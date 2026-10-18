import os
import hashlib
import json
import time
from datetime import datetime, timedelta, timezone
from flask import Blueprint, request, jsonify
import logging
import requests

logger = logging.getLogger('blaeu.intervals')

from db import (
    get_db, save_intervals_connection, get_intervals_connection, delete_intervals_connection,
    update_intervals_last_sync, get_user_by_id, add_route,
    upsert_activity, get_activity_by_source, get_activity_source_ids, get_latest_activity_start,
    set_activity_hidden, upsert_daily_wellness, upsert_athlete_thresholds, get_user_wellness_opt_in,
    find_duplicate_activities, merge_activities
)
from gpx_parser import parse_gpx
import app
from utils import (
    get_current_user_id, rate_limit
)

intervals_bp = Blueprint('intervals', __name__)

def parse_intervals_datetime(dt_str):
    if not dt_str:
        return datetime.now(timezone.utc)
    cleaned = dt_str.strip()
    if cleaned.endswith('Z'):
        cleaned = cleaned[:-1] + '+00:00'
    try:
        return datetime.fromisoformat(cleaned)
    except Exception:
        for fmt in ('%Y-%m-%dT%H:%M:%S%z', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S'):
            try:
                return datetime.strptime(cleaned, fmt)
            except ValueError:
                continue
    return datetime.now(timezone.utc)

def reconstruct_gpx_from_streams(activity_name, start_time_str, time_stream, lat_stream, lon_stream, altitude_stream):
    if not lat_stream or not lon_stream:
        return ""
        
    start_dt = parse_intervals_datetime(start_time_str)
    gpx_pts = []
    num_points = min(len(lat_stream), len(lon_stream))
    
    for i in range(num_points):
        lat = lat_stream[i]
        lon = lon_stream[i]
        offset_sec = time_stream[i] if (time_stream and i < len(time_stream)) else i
        pt_dt = start_dt + timedelta(seconds=offset_sec)
        pt_time_str = pt_dt.strftime('%Y-%m-%dT%H:%M:%SZ')
        
        ele_tag = ""
        if altitude_stream and i < len(altitude_stream) and altitude_stream[i] is not None:
            ele_tag = f"<ele>{altitude_stream[i]}</ele>"
            
        gpx_pts.append(
            f'      <trkpt lat="{lat}" lon="{lon}">\n'
            f'        {ele_tag}\n'
            f'        <time>{pt_time_str}</time>\n'
            f'      </trkpt>'
        )
        
    gpx_pts_str = "\n".join(gpx_pts)
    gpx_str = f"""<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" creator="Blaeu" xmlns="http://www.topografix.com/GPX/1/1">
  <metadata>
    <time>{start_dt.strftime('%Y-%m-%dT%H:%M:%SZ')}</time>
  </metadata>
  <trk>
    <name>{activity_name or "Intervals.icu Activity"}</name>
    <trkseg>
{gpx_pts_str}
    </trkseg>
  </trk>
</gpx>
"""
    return gpx_str

INTERVALS_API = "https://intervals.icu/api/v1"

# Pause between successive Intervals.icu requests in sync jobs. Reliability matters
# more than speed here, so we stay well below anything that could trigger throttling.
REQUEST_DELAY_SECONDS = float(os.getenv('BLAEU_INTERVALS_REQUEST_DELAY', '1.0'))
MAX_RETRIES = 3
SYNC_BATCH_SIZE = 15

STRAVA_FORBIDDEN_MESSAGE = (
    "Failed to fetch streams from Intervals.icu: 403 Forbidden. "
    "This typically happens for activities synced from Strava, as Strava's API terms restrict sharing "
    "activity data with external apps. Please try syncing directly from your device provider (e.g., Garmin) "
    "to Intervals.icu, or upload the GPX file manually."
)

class IntervalsTransientError(Exception):
    """A temporary failure (network, throttling, server error): retry later, never skip."""

def polite_pause(seconds=None):
    delay = REQUEST_DELAY_SECONDS if seconds is None else seconds
    if delay > 0:
        time.sleep(delay)

def intervals_get(path, api_key, params=None, timeout=30):
    """
    GET an Intervals.icu API path and return the decoded JSON.
    Retries on 429 and 5xx with backoff (honouring Retry-After); other HTTP errors raise.
    """
    url = f"{INTERVALS_API}{path}"
    auth = ('API_KEY', api_key)
    for attempt in range(MAX_RETRIES + 1):
        resp = requests.get(url, auth=auth, params=params, timeout=timeout)
        status = resp.status_code
        if isinstance(status, int) and (status == 429 or status >= 500) and attempt < MAX_RETRIES:
            retry_after = resp.headers.get('Retry-After') if isinstance(getattr(resp, 'headers', None), dict) else None
            try:
                wait = min(float(retry_after), 300) if retry_after else 10 * (2 ** attempt)
            except ValueError:
                wait = 10 * (2 ** attempt)
            logger.warning(f"Intervals.icu returned HTTP {status} for {path}; retrying in {wait:.0f}s")
            polite_pause(wait)
            continue
        resp.raise_for_status()
        return resp.json()

def _num(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

def _first(summary, *keys):
    for key in keys:
        value = _num(summary.get(key))
        if value is not None:
            return value
    return None

def to_utc_string(dt_str):
    if not dt_str:
        return None
    dt = parse_intervals_datetime(dt_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')

def is_strava_stub(summary):
    """Activities Intervals.icu received from Strava come back without any data."""
    if not isinstance(summary, dict):
        return False
    return bool(summary.get('_note')) or (
        str(summary.get('source', '')).upper() == 'STRAVA'
        and summary.get('moving_time') is None and summary.get('elapsed_time') is None
    )

def normalize_intervals_activity(summary):
    """
    Maps an Intervals.icu activity object to Blaeu activity fields. The full object is
    kept in raw_summary, so fields can be re-derived later without fetching again.
    """
    intensity = _first(summary, 'icu_intensity')
    if intensity is not None and intensity > 3:
        intensity = intensity / 100.0  # Intervals.icu reports intensity as a percentage

    start_utc = summary.get('start_date')
    start_local = summary.get('start_date_local')

    power_zones = summary.get('icu_zone_times')
    if isinstance(power_zones, list):
        power_zones = [z.get('secs') if isinstance(z, dict) else z for z in power_zones]

    return {
        'name': summary.get('name'),
        'sport_type': summary.get('type'),
        'start_time': to_utc_string(start_utc) if start_utc else None,
        'start_time_local': start_local.replace('T', ' ')[:19] if start_local else None,
        'timezone': summary.get('timezone'),
        'duration': _first(summary, 'elapsed_time'),
        'moving_time': _first(summary, 'moving_time', 'icu_recording_time'),
        'distance': _first(summary, 'distance', 'icu_distance'),
        'elevation_gain': _first(summary, 'total_elevation_gain'),
        'avg_hr': _first(summary, 'average_heartrate', 'icu_average_hr'),
        'max_hr': _first(summary, 'max_heartrate', 'icu_max_hr'),
        'avg_power': _first(summary, 'icu_average_watts', 'average_watts'),
        'normalized_power': _first(summary, 'icu_weighted_avg_watts', 'weighted_average_watts'),
        'avg_cadence': _first(summary, 'average_cadence'),
        'calories': _first(summary, 'calories'),
        'training_load': _first(summary, 'icu_training_load'),
        'intensity': intensity,
        'trimp': _first(summary, 'trimp'),
        'rpe': _first(summary, 'icu_rpe', 'perceived_exertion'),
        'feel': _first(summary, 'feel'),
        'hr_zone_times': summary.get('icu_hr_zone_times') or None,
        'power_zone_times': power_zones or None,
        'pace_zone_times': summary.get('pace_zone_times') or summary.get('gap_zone_times') or None,
        'is_indoor': summary.get('trainer') if isinstance(summary.get('trainer'), bool) else None,
        'raw_summary': summary,
    }

def may_have_gps(summary):
    """Uses stream_types when present to skip a streams request for indoor workouts."""
    stream_types = summary.get('stream_types') if isinstance(summary, dict) else None
    if isinstance(stream_types, list):
        return 'latlng' in stream_types
    return True

def fetch_intervals_streams(api_key, activity_id):
    try:
        streams_data = intervals_get(
            f"/activity/{activity_id}/streams.json", api_key, params={"types": "time,latlng,altitude"}
        )
    except requests.exceptions.HTTPError as e:
        status_code = e.response.status_code if e.response is not None else None
        logger.error(f"Failed to fetch streams for activity {activity_id} (HTTP {status_code}): {e}")
        if status_code == 403:
            raise ValueError(STRAVA_FORBIDDEN_MESSAGE)
        if isinstance(status_code, int) and 400 <= status_code < 500 and status_code != 429:
            raise ValueError(f"Failed to fetch streams from Intervals.icu (HTTP {status_code}): {e}")
        raise IntervalsTransientError(f"Failed to fetch streams from Intervals.icu (HTTP {status_code}): {e}")
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to fetch streams for activity {activity_id}: {e}")
        raise IntervalsTransientError(f"Failed to fetch streams from Intervals.icu: {e}")

    streams = {'time': None, 'lat': None, 'lon': None, 'altitude': None}
    for stream in streams_data if isinstance(streams_data, list) else []:
        t = stream.get("type")
        if t == "time":
            streams['time'] = stream.get("data")
        elif t == "latlng":
            streams['lat'] = stream.get("data")
            streams['lon'] = stream.get("data2")
        elif t == "altitude":
            streams['altitude'] = stream.get("data")
    return streams

def fetch_intervals_activity(api_key, activity_id):
    try:
        summary = intervals_get(f"/activity/{activity_id}", api_key)
    except requests.exceptions.HTTPError as e:
        status_code = e.response.status_code if e.response is not None else None
        logger.error(f"Failed to fetch activity {activity_id} (HTTP {status_code}): {e}")
        if status_code == 403:
            raise ValueError(STRAVA_FORBIDDEN_MESSAGE)
        if isinstance(status_code, int) and 400 <= status_code < 500 and status_code != 429:
            raise ValueError(f"Failed to fetch activity from Intervals.icu (HTTP {status_code}): {e}")
        raise IntervalsTransientError(f"Failed to fetch activity from Intervals.icu (HTTP {status_code}): {e}")
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to fetch activity {activity_id}: {e}")
        raise IntervalsTransientError(f"Failed to fetch activity from Intervals.icu: {e}")
    if not isinstance(summary, dict):
        raise ValueError("Unexpected response from Intervals.icu while fetching the activity.")
    return summary

def store_intervals_route(user_id, activity_id, activity_name, start_time_str, streams):
    """Rebuilds a GPX file from streams and registers it as a route. Returns the route id."""
    gpx_str = reconstruct_gpx_from_streams(
        activity_name, start_time_str, streams['time'], streams['lat'], streams['lon'], streams['altitude']
    )
    gpx_bytes = gpx_str.encode('utf-8')

    # Parse to verify validity and extract metadata
    parsed = parse_gpx(gpx_bytes)

    # Generate filename and unique hash
    import secrets
    file_hash = hashlib.sha256(gpx_bytes).hexdigest()
    filename = f"intervals_{activity_id}_{secrets.token_hex(8)}.gpx"
    file_path = os.path.join(app.GPX_STORE_DIR, filename)

    # Save to local GPX store
    os.makedirs(app.GPX_STORE_DIR, exist_ok=True)
    with open(file_path, 'wb') as f:
        f.write(gpx_bytes)

    track_name = None
    if parsed.get('tracks') and len(parsed['tracks']) > 0:
        track_name = parsed['tracks'][0].get('name')
    if not track_name:
        track_name = activity_name or f"Intervals Activity {activity_id}"

    # Register in SQLite database routes
    route_metadata = {
        'name': track_name,
        'description': f"Imported from Intervals.icu (Activity ID: {activity_id})",
        'filename': filename,
        'file_hash': file_hash,
        'file_path': file_path,
        'folder_id': None,
        'user_id': user_id,
        'is_public': False, # private by default
        'timezone': parsed.get('timezone'),
        'created_at': parsed.get('start_time'),
        'simplified_path': json.dumps(parsed.get('simplified_path', []))
    }

    try:
        route_id = add_route(route_metadata, parsed['statistics'])
    except Exception:
        # Don't leave an orphaned GPX file behind when the route can't be registered
        if os.path.exists(file_path):
            os.remove(file_path)
        raise

    # Trigger background poster generation if default style is not 'dark'
    user = get_user_by_id(user_id)
    default_style = user.get('default_map_style', 'dark') if user else 'dark'
    if default_style and default_style != 'dark':
        app.run_async_poster_generation(route_id, user_id, default_style)

    return route_id

def import_single_intervals_activity(user_id, athlete_id, api_key, activity_id, activity_name=None, start_time_str=None, summary=None):
    """
    Imports one Intervals.icu activity into the training log. Workouts with GPS also get
    a route; workouts without GPS (indoor, strength) are stored as activities only.
    Re-importing is safe: the summary is refreshed and an existing route is reused.
    The same workout already imported from another source (Garmin, a GPX upload) is
    merged into this one, reusing its route instead of creating a second map.
    Returns a dict with activity_id, route_id, name, created and merged.
    """
    activity_id = str(activity_id)
    if summary is None:
        summary = fetch_intervals_activity(api_key, activity_id)
    if is_strava_stub(summary):
        raise ValueError(STRAVA_FORBIDDEN_MESSAGE)

    fields = normalize_intervals_activity(summary)
    fields['name'] = fields.get('name') or activity_name or f"Intervals Activity {activity_id}"
    start_time_str = summary.get('start_date') or start_time_str
    if not fields.get('start_time') and start_time_str:
        fields['start_time'] = to_utc_string(start_time_str)

    existing = get_activity_by_source(user_id, 'intervals', activity_id)
    route_id = existing['route_id'] if existing else None

    duplicates = find_duplicate_activities(
        user_id, 'intervals', fields.get('start_time'), fields.get('duration') or fields.get('moving_time')
    )
    if route_id is None:
        route_id = next((d['route_id'] for d in duplicates if d['route_id']), None)

    if route_id is None and may_have_gps(summary):
        streams = fetch_intervals_streams(api_key, activity_id)
        if streams['lat'] and streams['lon']:
            route_id = store_intervals_route(user_id, activity_id, fields['name'], start_time_str, streams)

    db_activity_id = upsert_activity(user_id, 'intervals', activity_id, fields, route_id=route_id)
    if existing and existing.get('hidden'):
        # An explicit (re-)import brings back an activity the user deleted earlier
        set_activity_hidden(db_activity_id, False)
    merge_activities([d['id'] for d in duplicates], db_activity_id)

    return {
        'activity_id': db_activity_id, 'route_id': route_id, 'name': fields['name'],
        'created': existing is None, 'merged': len(duplicates),
    }

def sync_intervals_thresholds(user_id, athlete_id, api_key):
    """Stores the athlete's per-sport thresholds and zones from Intervals.icu."""
    profile = intervals_get(f"/athlete/{athlete_id}", api_key, timeout=20)
    if not isinstance(profile, dict):
        return 0
    resting_hr = _first(profile, 'icu_resting_hr')
    count = 0
    for setting in profile.get('sportSettings') or []:
        types = setting.get('types') or []
        if not types:
            continue
        upsert_athlete_thresholds(user_id, types[0], 'intervals', {
            'ftp': _first(setting, 'ftp'),
            'lthr': _first(setting, 'lthr'),
            'max_hr': _first(setting, 'max_hr'),
            'resting_hr': resting_hr,
            'threshold_pace': _first(setting, 'threshold_pace'),
            'hr_zones': setting.get('hr_zones'),
            'power_zones': setting.get('power_zones'),
            'pace_zones': setting.get('pace_zones'),
        }, raw=setting)
        count += 1
    return count

# Wellness fields that are derived from training rather than from the body; these are
# stored even without the wellness opt-in because they describe the training itself.
TRAINING_WELLNESS_FIELDS = ('ctl', 'atl', 'ramp_rate')

def normalize_intervals_wellness(record):
    return {
        'resting_hr': _first(record, 'restingHR'),
        'hrv': _first(record, 'hrv'),
        'hrv_sdnn': _first(record, 'hrvSDNN'),
        'sleep_secs': _first(record, 'sleepSecs'),
        'sleep_score': _first(record, 'sleepScore'),
        'sleep_quality': _first(record, 'sleepQuality'),
        'weight': _first(record, 'weight'),
        'body_fat': _first(record, 'bodyFat'),
        'readiness': _first(record, 'readiness'),
        'stress_avg': _first(record, 'stress'),
        'spo2': _first(record, 'spO2'),
        'respiration': _first(record, 'respiration'),
        'steps': _first(record, 'steps'),
        'vo2max': _first(record, 'vo2max'),
        'ctl': _first(record, 'ctl'),
        'atl': _first(record, 'atl'),
        'ramp_rate': _first(record, 'rampRate'),
        'soreness': _first(record, 'soreness'),
        'fatigue': _first(record, 'fatigue'),
        'mood': _first(record, 'mood'),
        'motivation': _first(record, 'motivation'),
    }

def sync_intervals_wellness(user_id, athlete_id, api_key, oldest, newest):
    """
    Stores daily wellness between two dates (YYYY-MM-DD). Without the user's opt-in only
    training-derived fitness/fatigue values are kept; body data and the raw record are not.
    """
    opted_in = get_user_wellness_opt_in(user_id) == 1
    records = intervals_get(
        f"/athlete/{athlete_id}/wellness", api_key, params={'oldest': oldest, 'newest': newest}, timeout=30
    )
    count = 0
    for record in records if isinstance(records, list) else []:
        date = record.get('id')
        if not date:
            continue
        fields = normalize_intervals_wellness(record)
        if not opted_in:
            fields = {k: v for k, v in fields.items() if k in TRAINING_WELLNESS_FIELDS}
        if all(v is None for v in fields.values()):
            continue
        upsert_daily_wellness(user_id, date, 'intervals', fields, raw=record if opted_in else None)
        count += 1
    return count

def get_interval_seconds(interval_str):
    mapping = {
        '1h': 3600,
        '3h': 10800,
        '6h': 21600,
        '12h': 43200,
        '24h': 86400
    }
    return mapping.get(interval_str)

def sync_user_intervals_activities_in_background(user_id):
    connection = get_intervals_connection(user_id)
    if not connection:
        return

    athlete_id = connection['athlete_id']
    api_key = connection['api_key']

    # Includes hidden (deleted) activities so they are not imported again
    imported_ids = get_activity_source_ids(user_id, 'intervals')
    latest_start = get_latest_activity_start(user_id, 'intervals')

    if latest_start:
        try:
            oldest_dt = parse_intervals_datetime(latest_start) - timedelta(days=1)
        except Exception:
            oldest_dt = datetime.now(timezone.utc) - timedelta(days=90)
    else:
        oldest_dt = datetime.now(timezone.utc) - timedelta(days=90)

    try:
        activities = intervals_get(
            f"/athlete/{athlete_id}/activities", api_key, params={'oldest': oldest_dt.strftime('%Y-%m-%d')}
        )
    except Exception as e:
        logger.error(f"Failed to fetch activity list for athlete {athlete_id}: {e}")
        return
    if not isinstance(activities, list):
        logger.error(f"Unexpected activity list response for athlete {athlete_id}")
        return

    # Sort activities by start time ascending (older first)
    activities.sort(key=lambda a: a.get('start_date') or a.get('start_date_local') or '')
    pending = [a for a in activities if a.get('id') is not None and str(a.get('id')) not in imported_ids]

    if latest_start is None:
        # First sync: import only the most recent activity. History comes from the backfill job.
        pending = pending[-1:]
    else:
        # Oldest first, in batches, so a long gap is caught up over several runs without gaps
        pending = [
            a for a in pending
            if (to_utc_string(a.get('start_date') or a.get('start_date_local')) or '') > latest_start
        ][:SYNC_BATCH_SIZE]

    imported_any = False
    for act in pending:
        act_id = str(act.get('id'))
        polite_pause()
        try:
            import_single_intervals_activity(
                user_id, athlete_id, api_key, act_id, act.get('name'),
                act.get('start_date') or act.get('start_date_local'), summary=act
            )
            imported_any = True
        except ValueError as e:
            # Permanent problem with this one activity (e.g. Strava-sourced data)
            logger.warning(f"Skipping Intervals.icu activity {act_id} during sync: {e}")
        except Exception as e:
            # Stop instead of skipping: an unexpected error must not leave a gap that the
            # "newer than latest" logic would never revisit.
            logger.error(f"Error importing Intervals.icu activity {act_id}; stopping this sync run: {e}", exc_info=True)
            break

    try:
        polite_pause()
        sync_intervals_thresholds(user_id, athlete_id, api_key)
    except Exception as e:
        logger.warning(f"Failed to sync Intervals.icu thresholds for user {user_id}: {e}")

    try:
        polite_pause()
        newest = datetime.now(timezone.utc).date()
        sync_intervals_wellness(
            user_id, athlete_id, api_key,
            (newest - timedelta(days=7)).isoformat(), newest.isoformat()
        )
    except Exception as e:
        logger.warning(f"Failed to sync Intervals.icu wellness for user {user_id}: {e}")

    if imported_any:
        update_intervals_last_sync(user_id, datetime.now().strftime('%Y-%m-%d %H:%M:%S'))


def run_auto_sync_for_all_users():
    from db import get_all_active_intervals_connections, attempt_intervals_sync_lock
    
    connections = get_all_active_intervals_connections()
    now = datetime.now()
    now_str = now.strftime('%Y-%m-%d %H:%M:%S')
    
    for conn in connections:
        user_id = conn['user_id']
        interval_str = conn['auto_sync_interval']
        last_sync_str = conn['last_sync']
        
        seconds = get_interval_seconds(interval_str)
        if not seconds:
            continue
            
        should_sync = False
        if not last_sync_str:
            should_sync = True
        else:
            try:
                last_sync_dt = datetime.strptime(last_sync_str, '%Y-%m-%d %H:%M:%S')
                if (now - last_sync_dt).total_seconds() >= seconds:
                    should_sync = True
            except ValueError:
                should_sync = True
                
        if should_sync:
            if attempt_intervals_sync_lock(user_id, now_str, last_sync_str, seconds):
                logger.info(f"[Auto-Sync] Locked and running Intervals.icu sync for user {user_id}...")
                try:
                    sync_user_intervals_activities_in_background(user_id)
                except Exception as e:
                    logger.error(f"[Auto-Sync] Error syncing activities for user {user_id}: {e}", exc_info=True)


@intervals_bp.route('/api/intervals/status', methods=['GET'])
def get_intervals_status():
    user_id = get_current_user_id()
    if not user_id:
        return jsonify({'error': 'Unauthorized. Please log in.'}), 401
        
    connection = get_intervals_connection(user_id)
    if connection:
        return jsonify({
            'status': 'connected',
            'athlete_id': connection['athlete_id'],
            'last_sync': connection['last_sync'],
            'auto_sync_interval': connection.get('auto_sync_interval', 'off')
        })
    else:
        return jsonify({'status': 'disconnected'})

@intervals_bp.route('/api/intervals/auto-sync', methods=['PUT'])
def update_intervals_auto_sync():
    user_id = get_current_user_id()
    if not user_id:
        return jsonify({'error': 'Unauthorized. Please log in.'}), 401
        
    connection = get_intervals_connection(user_id)
    if not connection:
        return jsonify({'error': 'Intervals.icu not connected'}), 400
        
    data = request.json or {}
    interval = data.get('auto_sync_interval', 'off')
    
    allowed = ['off', '1h', '3h', '6h', '12h', '24h']
    if interval not in allowed:
        return jsonify({'error': 'Invalid auto-sync interval value'}), 400
        
    from db import update_intervals_auto_sync_interval
    try:
        update_intervals_auto_sync_interval(user_id, interval)
        return jsonify({'status': 'success', 'auto_sync_interval': interval})
    except Exception as e:
        logger.exception("Failed to update auto-sync interval")
        return jsonify({'error': 'Failed to update auto-sync interval: An unexpected server error occurred.'}), 500

@intervals_bp.route('/api/intervals/connect', methods=['POST'])
@rate_limit(limit=5, period=60)
def connect_intervals():
    user_id = get_current_user_id()
    if not user_id:
        return jsonify({'error': 'Unauthorized. Please log in.'}), 401
        
    data = request.json or {}
    athlete_id = data.get('athlete_id')
    api_key = data.get('api_key')
    
    if not api_key:
        return jsonify({'error': 'API Key is required'}), 400
        
    # Default to "0" if athlete_id is empty/whitespace
    athlete_id_str = athlete_id.strip() if athlete_id else "0"
    if not athlete_id_str:
        athlete_id_str = "0"
        
    # Validate connection by checking a request to athlete profile
    auth = ('API_KEY', api_key)
    url = f"https://intervals.icu/api/v1/athlete/{athlete_id_str}"
    
    try:
        resp = requests.get(url, auth=auth, timeout=10)
        if resp.status_code == 401:
            return jsonify({'error': 'Intervals.icu connection failed: Invalid API Key.'}), 401
        elif resp.status_code == 404:
            return jsonify({'error': f'Intervals.icu connection failed: Athlete ID {athlete_id_str} not found.'}), 404
        resp.raise_for_status()
        
        # Get actual athlete ID if the user input was "0" or empty
        profile_data = resp.json()
        if (athlete_id_str == "0" or not athlete_id_str) and profile_data and 'id' in profile_data:
            athlete_id_str = str(profile_data['id'])
    except Exception as e:
        logger.warning(f"Connection test failed: {e}")
        return jsonify({'error': 'Intervals.icu connection failed: Check your API Key and network connection.'}), 400
        
    try:
        save_intervals_connection(user_id, athlete_id_str, api_key)
    except Exception as e:
        logger.exception("Unexpected error during Intervals.icu connection setup")
        return jsonify({'error': 'Intervals.icu connection failed: An unexpected database error occurred.'}), 500

    # Import the training history in the background, slowly and resumably
    try:
        from blueprints.intervals_backfill import start_backfill
        start_backfill(user_id)
    except Exception:
        logger.exception("Failed to schedule Intervals.icu history backfill")
    return jsonify({'status': 'connected', 'athlete_id': athlete_id_str})

@intervals_bp.route('/api/intervals/disconnect', methods=['POST'])
def disconnect_intervals():
    user_id = get_current_user_id()
    if not user_id:
        return jsonify({'error': 'Unauthorized. Please log in.'}), 401
        
    delete_intervals_connection(user_id)
    return jsonify({'status': 'disconnected'})

@intervals_bp.route('/api/intervals/activities', methods=['GET'])
def get_intervals_activities():
    user_id = get_current_user_id()
    if not user_id:
        return jsonify({'error': 'Unauthorized. Please log in.'}), 401
        
    connection = get_intervals_connection(user_id)
    if not connection:
        return jsonify({'error': 'Intervals.icu not connected'}), 400
        
    athlete_id = connection['athlete_id']
    api_key = connection['api_key']
    
    auth = ('API_KEY', api_key)
    url = f"https://intervals.icu/api/v1/athlete/{athlete_id}/activities"
    
    # Show last 90 days of activities
    oldest_dt = datetime.now(timezone.utc) - timedelta(days=90)
    oldest_str = oldest_dt.strftime('%Y-%m-%d')
    
    try:
        resp = requests.get(url, auth=auth, params={'oldest': oldest_str}, timeout=20)
        resp.raise_for_status()
        activities = resp.json()
        
        # Sort desc (newest first) and limit to 15
        activities.sort(key=lambda a: a.get('start_date_local') or a.get('start_date', ''), reverse=True)
        activities = activities[:15]
        
        formatted = []
        for act in activities:
            formatted.append({
                'activityId': act.get('id'),
                'activityName': act.get('name'),
                'activityType': act.get('type'),
                'startTimeLocal': act.get('start_date_local') or act.get('start_date'),
                'startTimeUTC': act.get('start_date') or act.get('start_date_local'),
                'distance': act.get('distance'),
                'duration': act.get('moving_time') or act.get('elapsed_time')
            })
        return jsonify({'status': 'success', 'activities': formatted})
    except Exception as e:
        logger.exception("Unexpected error fetching Intervals.icu activities")
        return jsonify({'error': 'Failed to fetch activities: intervals.icu session expired or was revoked. Please reconnect.'}), 401

@intervals_bp.route('/api/intervals/import', methods=['POST'])
def import_intervals_activity():
    user_id = get_current_user_id()
    if not user_id:
        return jsonify({'error': 'Unauthorized. Please log in.'}), 401
        
    connection = get_intervals_connection(user_id)
    if not connection:
        return jsonify({'error': 'Intervals.icu not connected'}), 400
        
    data = request.json or {}
    activity_id = data.get('activityId')
    activity_name = data.get('activityName')
    start_time_str = data.get('startTimeLocal')
    
    if not activity_id:
        return jsonify({'error': 'activityId is required'}), 400
        
    athlete_id = connection['athlete_id']
    api_key = connection['api_key']
    
    try:
        result = import_single_intervals_activity(user_id, athlete_id, api_key, activity_id, activity_name, start_time_str)
        update_intervals_last_sync(user_id, datetime.now().strftime('%Y-%m-%d %H:%M:%S'))

        return jsonify({
            'status': 'success',
            'activity_id': result['activity_id'],
            'route_id': result['route_id'],
            'name': result['name']
        })
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except IntervalsTransientError:
        logger.exception("Temporary failure during Intervals.icu activity import")
        return jsonify({'error': 'Intervals.icu is temporarily unavailable. Please try again later.'}), 502
    except Exception as e:
        logger.exception("Unexpected error during Intervals.icu activity import")
        return jsonify({'error': 'Intervals.icu import failed: An unexpected error occurred.'}), 500


@intervals_bp.route('/api/intervals/backfill', methods=['GET'])
def get_intervals_backfill():
    user_id = get_current_user_id()
    if not user_id:
        return jsonify({'error': 'Unauthorized. Please log in.'}), 401
    from blueprints.intervals_backfill import job_progress, SOURCE, KIND
    from db import get_latest_sync_job
    return jsonify({'job': job_progress(get_latest_sync_job(user_id, SOURCE, KIND))})

@intervals_bp.route('/api/intervals/backfill', methods=['POST'])
@rate_limit(limit=5, period=60)
def start_intervals_backfill():
    user_id = get_current_user_id()
    if not user_id:
        return jsonify({'error': 'Unauthorized. Please log in.'}), 401
    if not get_intervals_connection(user_id):
        return jsonify({'error': 'Intervals.icu not connected'}), 400

    from blueprints.intervals_backfill import start_backfill, job_progress, DEFAULT_MONTHS, MAX_MONTHS
    data = request.json or {}
    months = data.get('months', DEFAULT_MONTHS)
    if not isinstance(months, int) or isinstance(months, bool) or not 1 <= months <= MAX_MONTHS:
        return jsonify({'error': f'months must be a whole number between 1 and {MAX_MONTHS}'}), 400
    try:
        job = start_backfill(user_id, months)
    except Exception:
        logger.exception("Failed to start Intervals.icu backfill")
        return jsonify({'error': 'Failed to start the history import: An unexpected server error occurred.'}), 500
    return jsonify({'job': job_progress(job)})

@intervals_bp.route('/api/intervals/backfill/cancel', methods=['POST'])
def cancel_intervals_backfill():
    user_id = get_current_user_id()
    if not user_id:
        return jsonify({'error': 'Unauthorized. Please log in.'}), 401
    from blueprints.intervals_backfill import cancel_backfill, job_progress
    return jsonify({'job': job_progress(cancel_backfill(user_id))})

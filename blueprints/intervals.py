import os
import hashlib
import json
from datetime import datetime, timedelta, timezone
from flask import Blueprint, request, jsonify
import logging
import requests

logger = logging.getLogger('blaeu.intervals')

from db import (
    get_db, save_intervals_connection, get_intervals_connection, delete_intervals_connection,
    update_intervals_last_sync, get_routes, get_route, get_user_by_id, add_route
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

def import_single_intervals_activity(user_id, athlete_id, api_key, activity_id, activity_name=None, start_time_str=None):
    auth = ('API_KEY', api_key)
    streams_url = f"https://intervals.icu/api/v1/activity/{activity_id}/streams.json"
    
    try:
        resp = requests.get(streams_url, auth=auth, params={"types": "time,latlng,altitude"}, timeout=30)
        resp.raise_for_status()
        streams_data = resp.json()
    except requests.exceptions.HTTPError as e:
        status_code = e.response.status_code if e.response is not None else None
        logger.error(f"Failed to fetch streams for activity {activity_id} (HTTP {status_code}): {e}")
        if status_code == 403:
            raise ValueError(
                "Failed to fetch streams from Intervals.icu: 403 Forbidden. "
                "This typically happens for activities synced from Strava, as Strava's API terms restrict sharing "
                "activity data with external apps. Please try syncing directly from your device provider (e.g., Garmin) "
                "to Intervals.icu, or upload the GPX file manually."
            )
        raise ValueError(f"Failed to fetch streams from Intervals.icu (HTTP {status_code}): {e}")
    except Exception as e:
        logger.error(f"Failed to fetch streams for activity {activity_id}: {e}")
        raise ValueError(f"Failed to fetch streams from Intervals.icu: {e}")
        
    time_stream = None
    lat_stream = None
    lon_stream = None
    altitude_stream = None
    
    for stream in streams_data:
        t = stream.get("type")
        if t == "time":
            time_stream = stream.get("data")
        elif t == "latlng":
            lat_stream = stream.get("data")
            lon_stream = stream.get("data2")
        elif t == "altitude":
            altitude_stream = stream.get("data")
            
    if not lat_stream or not lon_stream or len(lat_stream) == 0:
        raise ValueError("Activity does not contain GPS location data.")
        
    # Reconstruct GPX file
    gpx_str = reconstruct_gpx_from_streams(activity_name, start_time_str, time_stream, lat_stream, lon_stream, altitude_stream)
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
    
    route_id = add_route(route_metadata, parsed['statistics'])
    
    # Trigger background poster generation if default style is not 'dark'
    user = get_user_by_id(user_id)
    default_style = user.get('default_map_style', 'dark') if user else 'dark'
    if default_style and default_style != 'dark':
        app.run_async_poster_generation(route_id, user_id, default_style)
        
    return route_id

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
    
    # Get all already imported intervals route filenames for this user
    routes = get_routes(user_id)
    imported_ids = set()
    latest_created_at = None
    
    for r in routes:
        fn = r.get('filename', '')
        if fn.startswith('intervals_') and fn.endswith('.gpx'):
            try:
                act_id = fn.split('_')[1]
                imported_ids.add(str(act_id))
            except Exception:
                pass
                
            created_at = r.get('created_at')
            if created_at:
                if latest_created_at is None or created_at > latest_created_at:
                    latest_created_at = created_at

    if latest_created_at:
        try:
            dt = parse_intervals_datetime(latest_created_at)
            oldest_dt = dt - timedelta(days=1)
        except Exception:
            oldest_dt = datetime.now(timezone.utc) - timedelta(days=90)
    else:
        oldest_dt = datetime.now(timezone.utc) - timedelta(days=90)
        
    oldest_str = oldest_dt.strftime('%Y-%m-%d')
    
    auth = ('API_KEY', api_key)
    url = f"https://intervals.icu/api/v1/athlete/{athlete_id}/activities"
    
    try:
        resp = requests.get(url, auth=auth, params={'oldest': oldest_str}, timeout=30)
        resp.raise_for_status()
        activities = resp.json()
    except Exception as e:
        logger.error(f"Failed to fetch activity list for athlete {athlete_id}: {e}")
        return
        
    # Sort activities by start time ascending (older first)
    activities.sort(key=lambda a: a.get('start_date_local') or a.get('start_date', ''))
    latest_activities = activities[-15:] # Slice latest 15 activities
    
    imported_any = False
    
    if latest_created_at is None:
        # Import the single most recent activity that has valid coordinates
        if latest_activities:
            for act in reversed(latest_activities):
                act_id = str(act.get('id'))
                if act_id not in imported_ids:
                    try:
                        start_time = act.get('start_date') or act.get('start_date_local')
                        import_single_intervals_activity(user_id, athlete_id, api_key, act_id, act.get('name'), start_time)
                        imported_any = True
                        break
                    except ValueError as e:
                        if "GPS" in str(e) or "location" in str(e):
                            continue
                        logger.error(f"Error importing activity {act_id} during init: {e}")
    else:
        # Import all activities newer than latest_created_at
        for act in latest_activities:
            act_id = str(act.get('id'))
            start_time_str = act.get('start_date') or act.get('start_date_local')
            if not start_time_str:
                continue
                
            act_dt = parse_intervals_datetime(start_time_str)
            if act_dt.tzinfo is not None:
                act_dt = act_dt.astimezone(timezone.utc)
            act_utc_str = act_dt.strftime('%Y-%m-%d %H:%M:%S')
            
            if act_id not in imported_ids:
                if latest_created_at is None or act_utc_str > latest_created_at:
                    try:
                        import_single_intervals_activity(user_id, athlete_id, api_key, act_id, act.get('name'), start_time_str)
                        imported_any = True
                    except ValueError as e:
                        if "GPS" in str(e) or "location" in str(e):
                            continue
                        logger.error(f"Error importing activity {act_id} during sync: {e}")
                    
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
        return jsonify({'status': 'connected', 'athlete_id': athlete_id_str})
    except Exception as e:
        logger.exception("Unexpected error during Intervals.icu connection setup")
        return jsonify({'error': 'Intervals.icu connection failed: An unexpected database error occurred.'}), 500

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
        route_id = import_single_intervals_activity(user_id, athlete_id, api_key, activity_id, activity_name, start_time_str)
        update_intervals_last_sync(user_id, datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        
        route = get_route(route_id)
        return jsonify({
            'status': 'success',
            'route_id': route_id,
            'name': route['name'] if route else f"Intervals Activity {activity_id}"
        })
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        logger.exception("Unexpected error during Intervals.icu activity import")
        return jsonify({'error': 'Intervals.icu import failed: An unexpected error occurred.'}), 500

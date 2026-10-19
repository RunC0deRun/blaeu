"""
The welcome screen: the latest workout with a comparison to similar ones, the key fitness and
recovery numbers, earlier workouts, and what the app did in the background.
"""
import json
import logging
from datetime import datetime, timedelta, timezone

from flask import Blueprint, jsonify, request

from blueprints.activities import serialize_activity
from coach.context import _merge_wellness
from coach.daily_note import parse_day
from db import (
    get_db, get_activities, get_daily_wellness, get_user_by_id, get_garmin_connection,
    get_intervals_connection,
)
from utils import get_current_user_id

logger = logging.getLogger('blaeu.welcome')
welcome_bp = Blueprint('welcome', __name__)

RECENT_COUNT = 5
COMPARE_COUNT = 4
# Workouts count as similar when their distance is within this factor of the latest one
COMPARE_DISTANCE_FACTOR = 1.33
FITNESS_DAYS = 42
WELLNESS_DAYS = 28
EVENT_COUNT = 6
BACKFILL_LABELS = {'pending': 'waiting to start', 'running': 'running', 'paused': 'paused',
                   'completed': 'completed', 'cancelled': 'cancelled'}


def _serialize(activity):
    data = serialize_activity(activity)
    data['route_id'] = activity.get('route_id')
    data['aerobic_te'] = activity.get('aerobic_te')
    try:
        zones = json.loads(activity['hr_zone_times']) if activity.get('hr_zone_times') else None
    except (TypeError, ValueError):
        zones = None
    data['hr_zone_times'] = [z or 0 for z in zones] if isinstance(zones, list) and any(zones) else None
    return data


def _is_ride(sport_type):
    t = (sport_type or '').lower()
    return any(word in t for word in ('ride', 'cycl', 'bike'))


def compare_with_similar(latest, earlier):
    """
    How the latest workout compares with up to four earlier ones of the same sport and a
    similar distance: pace (speed for rides) and average heart rate.
    """
    def moving(a):
        return a.get('moving_time') or a.get('duration')

    if not latest.get('distance') or not moving(latest):
        return None
    low, high = latest['distance'] / COMPARE_DISTANCE_FACTOR, latest['distance'] * COMPARE_DISTANCE_FACTOR
    similar = [
        a for a in earlier
        if a.get('sport_type') == latest.get('sport_type') and a.get('distance') and moving(a)
        and low <= a['distance'] <= high
    ][:COMPARE_COUNT]
    if len(similar) < 2:
        return None

    ride = _is_ride(latest.get('sport_type'))
    def measure(a):
        speed = a['distance'] / moving(a)
        return speed * 3.6 if ride else 1000 / speed  # km/h, or seconds per km

    average = sum(measure(a) for a in similar) / len(similar)
    result = {'count': len(similar), 'metric': 'speed' if ride else 'pace',
              'diff': round(measure(latest) - average, 1), 'hr_diff': None}
    hrs = [a['avg_hr'] for a in similar if a.get('avg_hr')]
    if latest.get('avg_hr') and hrs:
        result['hr_diff'] = round(latest['avg_hr'] - sum(hrs) / len(hrs))
    return result


def _fitness(wellness):
    rows = [w for w in wellness if w.get('ctl') is not None and w.get('atl') is not None]
    if not rows:
        return None
    latest = rows[0]
    return {
        'date': latest['date'],
        'ctl': latest['ctl'],
        'atl': latest['atl'],
        'form': latest['ctl'] - latest['atl'],
        'ramp_rate': latest.get('ramp_rate'),
        'ctl_series': [{'date': w['date'], 'value': w['ctl']} for w in reversed(rows[:FITNESS_DAYS])],
    }


def _wellness_metric(wellness, key):
    rows = [w for w in wellness if w.get(key) is not None]
    if not rows:
        return None
    week = [w[key] for w in rows[:7]]
    return {
        'value': rows[0][key],
        'date': rows[0]['date'],
        'avg7': sum(week) / len(week),
        'series': [{'date': w['date'], 'value': w[key]} for w in reversed(rows[:WELLNESS_DAYS])],
    }


def _last_seven_days(activities, day):
    since = (day - timedelta(days=6)).isoformat()
    acts = [a for a in activities if (a.get('start_time_local') or a.get('start_time') or '')[:10] >= since]
    return {
        'sessions': len(acts),
        'duration': sum(a.get('moving_time') or a.get('duration') or 0 for a in acts),
        'distance': sum(a.get('distance') or 0 for a in acts),
        'load': sum(a.get('training_load') or 0 for a in acts),
    }


def _local_to_utc(stamp):
    """Connection sync times are written in the server's local time; events use UTC."""
    try:
        return datetime.strptime(stamp, '%Y-%m-%d %H:%M:%S').astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError):
        return None


def _events(user_id):
    events = []
    for name, connection in (('Garmin Connect', get_garmin_connection(user_id)),
                             ('Intervals.icu', get_intervals_connection(user_id))):
        time = _local_to_utc(connection.get('last_sync')) if connection else None
        if time:
            events.append({'kind': 'sync', 'title': f'{name} sync', 'detail': 'Checked for new workouts', 'time': time})

    conn = get_db()
    try:
        job = conn.execute(
            "SELECT * FROM sync_jobs WHERE user_id = ? ORDER BY id DESC LIMIT 1", (user_id,)
        ).fetchone()
        documents = conn.execute("""
            SELECT title, status, chunk_count, error, updated_at FROM library_documents
            WHERE owner_user_id = ? OR owner_user_id IS NULL ORDER BY updated_at DESC LIMIT 3
        """, (user_id,)).fetchall()
        conversations = conn.execute("""
            SELECT c.title, c.updated_at, COUNT(m.id) AS messages FROM chat_conversations c
            LEFT JOIN chat_messages m ON m.conversation_id = c.id
            WHERE c.user_id = ? GROUP BY c.id ORDER BY c.updated_at DESC LIMIT 3
        """, (user_id,)).fetchall()
    finally:
        conn.close()

    if job:
        job = dict(job)
        detail = f"{BACKFILL_LABELS.get(job['status'], job['status'])}"
        if job['status'] in ('pending', 'running', 'paused') and job['windows_total']:
            detail += f", {job['windows_done']} of {job['windows_total']} steps"
        elif job['status'] == 'completed':
            detail += f", {job['activities_new']} new workouts, {job['wellness_days']} wellness days"
        if job['status'] == 'paused' and job.get('last_error'):
            detail += f" ({job['last_error'][:80]})"
        events.append({'kind': 'warning' if job['status'] == 'paused' else 'sync', 'title': 'Training history import',
                       'detail': detail, 'time': job['updated_at']})
    for d in documents:
        if d['status'] == 'ready':
            detail = f"Indexed, {d['chunk_count']} passages"
        elif d['status'] == 'failed':
            detail = 'Indexing failed'
        else:
            detail = 'Indexing'
        events.append({'kind': 'warning' if d['status'] == 'failed' else 'library', 'title': d['title'],
                       'detail': detail, 'time': d['updated_at']})
    for c in conversations:
        events.append({'kind': 'coach', 'title': c['title'] or 'Coach conversation',
                       'detail': f"Coach conversation, {c['messages']} messages", 'time': c['updated_at']})

    events = [e for e in events if e['time']]
    events.sort(key=lambda e: e['time'], reverse=True)
    return events[:EVENT_COUNT]


@welcome_bp.route('/api/welcome', methods=['GET'])
def welcome():
    user_id = get_current_user_id()
    if not user_id:
        return jsonify({'error': 'Unauthorized. Please log in.'}), 401

    day = parse_day(request.args.get('date'))
    user = get_user_by_id(user_id) or {}
    activities = get_activities(user_id)
    latest = activities[0] if activities else None

    wellness = _merge_wellness(get_daily_wellness(
        user_id, start=(day - timedelta(days=FITNESS_DAYS + 7)).isoformat(), end=day.isoformat()))
    recovery = None
    # Fitness comes from training load; HRV and resting HR are wellness data, shown only with opt-in
    if user.get('wellness_opt_in') == 1:
        recovery = {'hrv': _wellness_metric(wellness, 'hrv'), 'resting_hr': _wellness_metric(wellness, 'resting_hr')}

    return jsonify({
        'date': day.isoformat(),
        'latest': _serialize(latest) if latest else None,
        'comparison': compare_with_similar(latest, activities[1:]) if latest else None,
        'recent': [_serialize(a) for a in activities[1:1 + RECENT_COUNT]],
        'fitness': _fitness(wellness),
        'recovery': recovery,
        'last_seven_days': _last_seven_days(activities, day),
        'events': _events(user_id),
    })

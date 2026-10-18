import logging
from flask import Blueprint, jsonify
from db import get_activities, get_activity, delete_activity
from utils import get_current_user_id

logger = logging.getLogger('blaeu.activities')
activities_bp = Blueprint('activities', __name__)

# Fields the dashboard needs for a workout card; raw summaries and zone data stay server-side.
ACTIVITY_API_FIELDS = (
    'id', 'name', 'sport_type', 'source', 'start_time', 'start_time_local', 'timezone',
    'duration', 'moving_time', 'distance', 'elevation_gain', 'avg_hr', 'max_hr',
    'avg_power', 'normalized_power', 'avg_cadence', 'calories', 'training_load',
    'intensity', 'rpe', 'feel', 'is_indoor'
)


def serialize_activity(activity):
    data = {k: activity.get(k) for k in ACTIVITY_API_FIELDS}
    data['is_indoor'] = bool(data['is_indoor'])
    return data


@activities_bp.route('/api/activities', methods=['GET'])
def list_activities():
    """Lists the user's workouts without a map: indoor rides, treadmill runs, strength..."""
    user_id = get_current_user_id()
    if not user_id:
        return jsonify({'error': 'Unauthorized. Please log in.'}), 401

    # Workouts with GPS are listed through /api/routes; get_activities already skips
    # hidden (deleted) activities and merged duplicates.
    activities = [a for a in get_activities(user_id) if a['route_id'] is None]
    return jsonify([serialize_activity(a) for a in activities])


@activities_bp.route('/api/activities/<int:activity_id>', methods=['DELETE'])
def remove_activity(activity_id):
    user_id = get_current_user_id()
    if not user_id:
        return jsonify({'error': 'Unauthorized. Please log in.'}), 401

    activity = get_activity(activity_id)
    if not activity or activity['hidden'] or activity['merged_into'] is not None:
        return jsonify({'error': 'Activity not found'}), 404

    if activity['user_id'] != user_id:
        return jsonify({'error': 'Permission denied. You do not own this activity.'}), 403

    if activity['route_id'] is not None:
        return jsonify({'error': 'This activity has a route. Delete the route instead.'}), 409

    try:
        delete_activity(activity_id)
        return jsonify({'success': True})
    except Exception:
        logger.exception("Unexpected error deleting activity")
        return jsonify({'error': 'Could not delete activity: An unexpected error occurred.'}), 500

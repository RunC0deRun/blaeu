import os
import re
import logging
from flask import Blueprint, request, jsonify, session
from werkzeug.security import generate_password_hash, check_password_hash
from db import (
    count_users, get_user_by_username, get_user_by_id, add_user, delete_user,
    get_users, backfill_ownerless_data, update_user_default_map_style,
    update_user_default_aspect_ratio
)
from utils import rate_limit, get_csrf_token, get_current_user_id

logger = logging.getLogger('blaeu.auth')
auth_bp = Blueprint('auth', __name__)

@auth_bp.route('/api/auth/register', methods=['POST'])
@rate_limit(limit=3, period=60)
def register():
    data = request.json or {}
    username = data.get('username')
    password = data.get('password')
    
    if not username or not password:
        return jsonify({'error': 'Username and password are required'}), 400
        
    username = username.strip()
    if len(username) < 3 or len(password) < 8:
        return jsonify({'error': 'Username must be at least 3 characters, password at least 8 characters'}), 400
        
    if not re.search(r'[A-Z]', password) or not re.search(r'[a-z]', password) or not re.search(r'[0-9]', password):
        return jsonify({'error': 'Password must contain at least one uppercase letter, one lowercase letter, and one digit.'}), 400
        
    try:
        user_count = count_users()
        allow_registration = os.getenv('BLAEU_ALLOW_REGISTRATION', 'false').lower() == 'true'
        if user_count > 0 and not allow_registration:
            return jsonify({'error': 'Registration is closed on this instance.'}), 403

        is_first = (user_count == 0)
        is_admin = 1 if is_first else 0
        
        try:
            password_hash = generate_password_hash(password)
        except AttributeError:
            password_hash = generate_password_hash(password, method='pbkdf2:sha256')
        user_id = add_user(username, password_hash, is_admin)
        
        # Backfill existing ownerless data to the first admin user
        if is_first:
            backfill_ownerless_data(user_id)
            
        session['user_id'] = user_id
        csrf_token = get_csrf_token()
        return jsonify({
            'success': True,
            'user': {
                'id': user_id,
                'username': username,
                'is_admin': is_admin,
                'default_map_style': 'dark',
                'week_start_day': 'Monday',
                'default_aspect_ratio': '16:9'
            },
            'csrf_token': csrf_token
        }), 201
    except ValueError as e:
        err_msg = str(e)
        logger.warning("Registration failed: %s", err_msg)
        if "Username already exists" in err_msg:
            return jsonify({'error': 'Username already exists'}), 400
        return jsonify({'error': 'Registration failed. Invalid input.'}), 400
    except Exception as e:
        logger.exception("Unexpected error during user registration")
        return jsonify({'error': 'Registration failed: An unexpected server error occurred.'}), 500


@auth_bp.route('/api/auth/login', methods=['POST'])
@rate_limit(limit=5, period=60)
def login():
    data = request.json or {}
    username = data.get('username')
    password = data.get('password')
    
    if not username or not password:
        return jsonify({'error': 'Username and password are required'}), 400
        
    user = get_user_by_username(username)
    if not user or not check_password_hash(user['password_hash'], password):
        return jsonify({'error': 'Invalid username or password'}), 401
        
    session['user_id'] = user['id']
    csrf_token = get_csrf_token()
    return jsonify({
        'success': True,
        'user': {
            'id': user['id'],
            'username': user['username'],
            'is_admin': user['is_admin'],
            'default_map_style': user.get('default_map_style', 'dark'),
            'week_start_day': user.get('week_start_day', 'Monday'),
            'default_aspect_ratio': user.get('default_aspect_ratio', '16:9'),
            'wellness_opt_in': user.get('wellness_opt_in')
        },
        'csrf_token': csrf_token
    })


@auth_bp.route('/api/auth/logout', methods=['POST'])
def logout():
    session.pop('user_id', None)
    session.pop('csrf_token', None)
    return jsonify({'success': True})


@auth_bp.route('/api/auth/status', methods=['GET'])
def auth_status():
    user_id = session.get('user_id')
    user_count = count_users()
    allow_registration = os.getenv('BLAEU_ALLOW_REGISTRATION', 'false').lower() == 'true'
    registration_open = (user_count == 0) or allow_registration
    csrf_token = get_csrf_token()
    
    if not user_id:
        return jsonify({
            'logged_in': False,
            'no_users_exist': user_count == 0,
            'registration_open': registration_open,
            'csrf_token': csrf_token
        })
        
    user = get_user_by_id(user_id)
    if not user:
        session.pop('user_id', None)
        return jsonify({
            'logged_in': False,
            'no_users_exist': user_count == 0,
            'registration_open': registration_open,
            'csrf_token': csrf_token
        })
        
    return jsonify({
        'logged_in': True,
        'user': {
            'id': user['id'],
            'username': user['username'],
            'is_admin': user['is_admin'],
            'default_map_style': user.get('default_map_style', 'dark'),
            'week_start_day': user.get('week_start_day', 'Monday'),
            'default_aspect_ratio': user.get('default_aspect_ratio', '16:9'),
            'wellness_opt_in': user.get('wellness_opt_in')
        },
        'csrf_token': csrf_token
    })


@auth_bp.route('/api/auth/default-map-style', methods=['PUT'])
def update_default_style():
    user_id = get_current_user_id()
    if not user_id:
        return jsonify({'error': 'Unauthorized. Please log in.'}), 401
        
    data = request.json or {}
    default_style = data.get('default_map_style')
    if not default_style:
        return jsonify({'error': 'default_map_style is required'}), 400
        
    try:
        update_user_default_map_style(user_id, default_style)
        return jsonify({
            'success': True,
            'default_map_style': default_style
        })
    except Exception as e:
        logger.exception("Unexpected error updating map style")
        return jsonify({'error': 'Failed to update map style: An unexpected server error occurred.'}), 500


@auth_bp.route('/api/auth/week-start-day', methods=['PUT'])
def update_week_start_day():
    user_id = get_current_user_id()
    if not user_id:
        return jsonify({'error': 'Unauthorized. Please log in.'}), 401
        
    data = request.json or {}
    week_start_day = data.get('week_start_day')
    if not week_start_day:
        return jsonify({'error': 'week_start_day is required'}), 400
        
    # Validation
    VALID_DAYS = {'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'}
    if week_start_day not in VALID_DAYS:
        return jsonify({'error': f'Invalid week_start_day. Must be one of: {", ".join(VALID_DAYS)}'}), 400
        
    try:
        from db import update_user_week_start_day
        update_user_week_start_day(user_id, week_start_day)
        return jsonify({
            'success': True,
            'week_start_day': week_start_day
        })
    except Exception as e:
        logger.exception("Unexpected error updating week start day")
        return jsonify({'error': 'Failed to update week start day: An unexpected server error occurred.'}), 500


@auth_bp.route('/api/auth/default-aspect-ratio', methods=['PUT'])
def update_default_aspect_ratio():
    user_id = get_current_user_id()
    if not user_id:
        return jsonify({'error': 'Unauthorized. Please log in.'}), 401
        
    data = request.json or {}
    default_aspect_ratio = data.get('default_aspect_ratio')
    if not default_aspect_ratio:
        return jsonify({'error': 'default_aspect_ratio is required'}), 400
        
    VALID_RATIOS = {'16:9', '4:3', '2:3', '3:2', '3:4', '9:16'}
    if default_aspect_ratio not in VALID_RATIOS:
        return jsonify({'error': f'Invalid aspect ratio. Must be one of: {", ".join(VALID_RATIOS)}'}), 400
        
    try:
        update_user_default_aspect_ratio(user_id, default_aspect_ratio)
        return jsonify({
            'success': True,
            'default_aspect_ratio': default_aspect_ratio
        })
    except Exception as e:
        logger.exception("Unexpected error updating aspect ratio")
        return jsonify({'error': 'Failed to update aspect ratio: An unexpected server error occurred.'}), 500


@auth_bp.route('/api/auth/users', methods=['GET'])
def list_users():
    admin_id = session.get('user_id')
    if not admin_id:
        return jsonify({'error': 'Unauthorized'}), 401
        
    admin = get_user_by_id(admin_id)
    if not admin or not admin['is_admin']:
        return jsonify({'error': 'Forbidden. Admin privileges required.'}), 403
        
    return jsonify(get_users())


@auth_bp.route('/api/auth/users/<int:delete_user_id>', methods=['DELETE'])
def remove_user(delete_user_id):
    admin_id = session.get('user_id')
    if not admin_id:
        return jsonify({'error': 'Unauthorized'}), 401
        
    admin = get_user_by_id(admin_id)
    if not admin or not admin['is_admin']:
        return jsonify({'error': 'Forbidden. Admin privileges required.'}), 403
        
    if admin_id == delete_user_id:
        return jsonify({'error': 'Cannot delete your own administrator account.'}), 400
        
    try:
        delete_user(delete_user_id)
        return jsonify({'success': True})
    except Exception as e:
        logger.exception("Unexpected error deleting user")
        return jsonify({'error': 'Failed to delete user: An unexpected server error occurred.'}), 500



def _data_preferences(user_id):
    from db import get_intervals_connection, get_latest_sync_job
    from blueprints.intervals_backfill import job_progress, SOURCE, KIND, DEFAULT_MONTHS, MAX_MONTHS

    user = get_user_by_id(user_id)
    intervals_connected = get_intervals_connection(user_id) is not None
    job = get_latest_sync_job(user_id, SOURCE, KIND) if intervals_connected else None
    wellness_opt_in = user.get('wellness_opt_in')

    # Wellness only arrives through Intervals.icu for now, so only ask when it's connected.
    # The history offer is for connections made before the backfill existed: connecting
    # now starts one automatically.
    ask_wellness = intervals_connected and wellness_opt_in is None
    offer_history = intervals_connected and job is None and not user.get('data_prompt_answered_at')
    return {
        'wellness_opt_in': None if wellness_opt_in is None else bool(wellness_opt_in),
        'intervals_connected': intervals_connected,
        'history': job_progress(job),
        'history_default_months': DEFAULT_MONTHS,
        'history_max_months': MAX_MONTHS,
        'prompt': {
            'show': ask_wellness or offer_history,
            'ask_wellness': ask_wellness,
            'offer_history': offer_history,
        },
    }


@auth_bp.route('/api/auth/data-preferences', methods=['GET'])
def get_data_preferences():
    user_id = get_current_user_id()
    if not user_id:
        return jsonify({'error': 'Unauthorized. Please log in.'}), 401
    return jsonify(_data_preferences(user_id))


@auth_bp.route('/api/auth/data-preferences', methods=['PUT'])
def update_data_preferences():
    """
    Saves training-data choices: {wellness_opt_in?: bool, import_history?: bool,
    history_months?: int, prompt_answered?: bool}.
    """
    user_id = get_current_user_id()
    if not user_id:
        return jsonify({'error': 'Unauthorized. Please log in.'}), 401

    from db import update_user_wellness_opt_in, mark_user_data_prompt_answered, get_intervals_connection
    from blueprints.intervals_backfill import start_backfill, DEFAULT_MONTHS, MAX_MONTHS

    data = request.json or {}
    wellness = data.get('wellness_opt_in')
    import_history = data.get('import_history', False)
    months = data.get('history_months', DEFAULT_MONTHS)
    if wellness is not None and not isinstance(wellness, bool):
        return jsonify({'error': 'wellness_opt_in must be true or false'}), 400
    if not isinstance(import_history, bool):
        return jsonify({'error': 'import_history must be true or false'}), 400
    if not isinstance(months, int) or isinstance(months, bool) or not 1 <= months <= MAX_MONTHS:
        return jsonify({'error': f'history_months must be a whole number between 1 and {MAX_MONTHS}'}), 400

    try:
        user = get_user_by_id(user_id)
        newly_opted_in = wellness is True and user.get('wellness_opt_in') != 1
        if wellness is not None:
            update_user_wellness_opt_in(user_id, wellness)

        # Opting in later also fetches the wellness history, through the same slow backfill
        if get_intervals_connection(user_id) and (import_history or newly_opted_in):
            start_backfill(user_id, months)

        if data.get('prompt_answered'):
            mark_user_data_prompt_answered(user_id)
    except Exception:
        logger.exception("Unexpected error saving training data preferences")
        return jsonify({'error': 'Failed to save your choices: An unexpected server error occurred.'}), 500

    return jsonify(_data_preferences(user_id))

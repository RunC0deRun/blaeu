import os
import re
import logging
from flask import Blueprint, request, jsonify, session
from werkzeug.security import generate_password_hash, check_password_hash
from db import (
    count_users, get_user_by_username, get_user_by_id, add_user, delete_user,
    get_users, backfill_ownerless_data, update_user_default_map_style
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
        
        password_hash = generate_password_hash(password)
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
                'week_start_day': 'Monday'
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
            'week_start_day': user.get('week_start_day', 'Monday')
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
            'week_start_day': user.get('week_start_day', 'Monday')
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

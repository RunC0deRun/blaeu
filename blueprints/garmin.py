import os
import hashlib
import json
from datetime import datetime
from flask import Blueprint, request, jsonify
from garminconnect.exceptions import GarminConnectAuthenticationError, GarminConnectTooManyRequestsError
import logging

logger = logging.getLogger('blaeu.garmin')

from db import (
    get_db, save_garmin_connection, get_garmin_connection, delete_garmin_connection,
    update_garmin_last_sync, get_routes, get_route, get_user_by_id, add_route
)
from gpx_parser import parse_gpx
import app
from utils import (
    get_current_user_id, rate_limit, run_async_poster_generation
)

garmin_bp = Blueprint('garmin', __name__)

class MfaRequiredException(GarminConnectAuthenticationError):
    pass

def import_single_garmin_activity(user_id, client, activity_id, activity_name=None):
    from garminconnect import Garmin
    # Download activity GPX
    gpx_bytes = client.download_activity(activity_id, dl_fmt=Garmin.ActivityDownloadFormat.GPX)
    
    # Parse GPX to verify validity and extract metadata
    parsed = parse_gpx(gpx_bytes)
    
    # Generate filename and unique hash
    file_hash = hashlib.sha256(gpx_bytes).hexdigest()
    filename = f"garmin_{activity_id}.gpx"
    file_path = os.path.join(app.GPX_STORE_DIR, filename)
    
    # Save to local GPX store
    os.makedirs(app.GPX_STORE_DIR, exist_ok=True)
    with open(file_path, 'wb') as f:
        f.write(gpx_bytes)
        
    track_name = None
    if parsed.get('tracks') and len(parsed['tracks']) > 0:
        track_name = parsed['tracks'][0].get('name')
    if not track_name:
        track_name = activity_name or f"Garmin Activity {activity_id}"

    # Register in SQLite database routes
    route_metadata = {
        'name': track_name,
        'description': f"Imported from Garmin Connect (Activity ID: {activity_id})",
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

def sync_user_garmin_activities_in_background(user_id):
    from garminconnect import Garmin
    token_store = os.path.join(app.DATA_DIR, 'garmin_tokens', str(int(user_id)))
    if not os.path.exists(token_store):
        return
        
    connection = get_garmin_connection(user_id)
    if not connection:
        return
        
    client = Garmin()
    client.login(tokenstore=token_store)
    
    # Get all already imported garmin route filenames for this user
    routes = get_routes(user_id)
    imported_ids = set()
    latest_created_at = None
    
    for r in routes:
        fn = r.get('filename', '')
        if fn.startswith('garmin_') and fn.endswith('.gpx'):
            try:
                act_id = fn.split('_')[1].split('.')[0]
                imported_ids.add(str(act_id))
            except Exception:
                pass
            
            created_at = r.get('created_at')
            if created_at:
                if latest_created_at is None or created_at > latest_created_at:
                    latest_created_at = created_at
                    
    # Fetch latest 15 activities from Garmin Connect
    activities = client.get_activities(0, 15)
    
    # Sort activities by start time ascending so we import older ones first
    activities.sort(key=lambda a: a.get('startTimeLocal', ''))
    
    imported_any = False
    
    if latest_created_at is None:
        # No Garmin activities imported yet. Import the single most recent one to initialize.
        if activities:
            latest_act = activities[-1] # The last one in sorted is the most recent
            latest_id = str(latest_act.get('activityId'))
            if latest_id not in imported_ids:
                import_single_garmin_activity(user_id, client, latest_id, latest_act.get('activityName'))
                imported_any = True
    else:
        # Import all activities newer than latest_created_at
        for act in activities:
            act_id = str(act.get('activityId'))
            start_time = act.get('startTimeLocal')
            if act_id not in imported_ids and start_time > latest_created_at:
                import_single_garmin_activity(user_id, client, act_id, act.get('activityName'))
                imported_any = True
                
    if imported_any:
        update_garmin_last_sync(user_id, datetime.now().strftime('%Y-%m-%d %H:%M:%S'))

def run_auto_sync_for_all_users():
    from db import get_all_active_garmin_connections, attempt_garmin_sync_lock
    
    connections = get_all_active_garmin_connections()
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
            # Try to acquire Gunicorn-safe atomic SQLite write lock
            if attempt_garmin_sync_lock(user_id, now_str, last_sync_str, seconds):
                logger.info(f"[Auto-Sync] Locked and running Garmin Connect sync for user {user_id}...")
                try:
                    sync_user_garmin_activities_in_background(user_id)
                except Exception as e:
                    logger.error(f"[Auto-Sync] Error syncing activities for user {user_id}: {e}", exc_info=True)


@garmin_bp.route('/api/garmin/status', methods=['GET'])
def get_garmin_status():
    user_id = get_current_user_id()
    if not user_id:
        return jsonify({'error': 'Unauthorized. Please log in.'}), 401
        
    connection = get_garmin_connection(user_id)
    if connection:
        return jsonify({
            'status': 'connected',
            'email': connection['email'],
            'display_name': connection['display_name'],
            'last_sync': connection['last_sync'],
            'auto_sync_interval': connection.get('auto_sync_interval', 'off')
        })
    else:
        return jsonify({'status': 'disconnected'})

@garmin_bp.route('/api/garmin/auto-sync', methods=['PUT'])
def update_garmin_auto_sync():
    user_id = get_current_user_id()
    if not user_id:
        return jsonify({'error': 'Unauthorized. Please log in.'}), 401
        
    connection = get_garmin_connection(user_id)
    if not connection:
        return jsonify({'error': 'Garmin not connected'}), 400
        
    data = request.json or {}
    interval = data.get('auto_sync_interval', 'off')
    
    allowed = ['off', '1h', '3h', '6h', '12h', '24h']
    if interval not in allowed:
        return jsonify({'error': 'Invalid auto-sync interval value'}), 400
        
    from db import update_garmin_auto_sync_interval
    try:
        update_garmin_auto_sync_interval(user_id, interval)
        return jsonify({'status': 'success', 'auto_sync_interval': interval})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': 'Failed to update auto-sync interval: An unexpected server error occurred.'}), 500

@garmin_bp.route('/api/garmin/connect', methods=['POST'])
@rate_limit(limit=5, period=60)
def connect_garmin():
    user_id = get_current_user_id()
    if not user_id:
        return jsonify({'error': 'Unauthorized. Please log in.'}), 401
        
    data = request.json or {}
    email = data.get('email')
    password = data.get('password')
    mfa_code = data.get('mfa_code')
    
    if not email or not password:
        return jsonify({'error': 'Email and password are required'}), 400
        
    from garminconnect import Garmin
    tokens_parent = os.path.join(app.DATA_DIR, 'garmin_tokens')
    os.makedirs(tokens_parent, exist_ok=True)
    try:
        os.chmod(tokens_parent, 0o700)
    except Exception:
        pass
    token_store = os.path.join(tokens_parent, str(int(user_id)))
    os.makedirs(token_store, exist_ok=True)
    try:
        os.chmod(token_store, 0o700)
    except Exception:
        pass
    
    try:
        if mfa_code:
            # Step 2: MFA code is provided, complete login
            client = Garmin(email, password, prompt_mfa=lambda: mfa_code)
            client.login(tokenstore=token_store)
        else:
            # Step 1: Initial login attempt, raise exception if MFA requested
            def mfa_callback():
                raise MfaRequiredException()
            client = Garmin(email, password, prompt_mfa=mfa_callback)
            client.login(tokenstore=token_store)
            
        if not client.client.di_token:
            if os.path.exists(token_store):
                import shutil
                shutil.rmtree(token_store, ignore_errors=True)
            raise GarminConnectAuthenticationError(
                "Garmin connection established, but failed to acquire persistent DI OAuth tokens due to rate limiting. "
                "Please try again in a few hours."
            )

        display_name = client.display_name
        save_garmin_connection(user_id, email, display_name)
        return jsonify({'status': 'connected', 'display_name': display_name})
        
    except MfaRequiredException:
        return jsonify({'status': 'mfa_required'})
    except GarminConnectAuthenticationError as e:
        import shutil
        if os.path.exists(token_store):
            shutil.rmtree(token_store, ignore_errors=True)
        err_msg = str(e)
        if "failed to acquire persistent DI OAuth tokens" in err_msg:
            return jsonify({'error': f"Garmin connection failed: {err_msg}"}), 401
        return jsonify({'error': "Garmin connection failed: Invalid email or password."}), 401
    except GarminConnectTooManyRequestsError as e:
        import shutil
        if os.path.exists(token_store):
            shutil.rmtree(token_store, ignore_errors=True)
        return jsonify({'error': f"Garmin connection failed: {str(e)}"}), 429
    except Exception as e:
        import shutil
        if os.path.exists(token_store):
            shutil.rmtree(token_store, ignore_errors=True)
        import traceback
        traceback.print_exc()
        err_msg = str(e)
        if "429" in err_msg.lower() or "too many requests" in err_msg.lower():
            return jsonify({'error': "Garmin connection failed: Too many requests. Please try again later."}), 429
        return jsonify({'error': "Garmin connection failed: An unexpected server error occurred."}), 500

@garmin_bp.route('/api/garmin/disconnect', methods=['POST'])
def disconnect_garmin():
    user_id = get_current_user_id()
    if not user_id:
        return jsonify({'error': 'Unauthorized. Please log in.'}), 401
        
    delete_garmin_connection(user_id)
    token_store = os.path.join(app.DATA_DIR, 'garmin_tokens', str(int(user_id)))
    if os.path.exists(token_store):
        import shutil
        try:
            shutil.rmtree(token_store)
        except Exception:
            pass
    return jsonify({'status': 'disconnected'})

@garmin_bp.route('/api/garmin/activities', methods=['GET'])
def get_garmin_activities():
    user_id = get_current_user_id()
    if not user_id:
        return jsonify({'error': 'Unauthorized. Please log in.'}), 401
        
    connection = get_garmin_connection(user_id)
    if not connection:
        return jsonify({'error': 'Garmin not connected'}), 400
        
    from garminconnect import Garmin
    token_store = os.path.join(app.DATA_DIR, 'garmin_tokens', str(int(user_id)))
    
    try:
        client = Garmin()
        client.login(tokenstore=token_store)
        activities = client.get_activities(0, 15)
        
        # Format activities nicely for frontend UI
        formatted = []
        for act in activities:
            formatted.append({
                'activityId': act.get('activityId'),
                'activityName': act.get('activityName'),
                'activityType': act.get('activityType', {}).get('typeKey'),
                'startTimeLocal': act.get('startTimeLocal'),
                'distance': act.get('distance'), # meters
                'duration': act.get('duration') # seconds
            })
        return jsonify({'status': 'success', 'activities': formatted})
    except GarminConnectTooManyRequestsError:
        import traceback
        traceback.print_exc()
        return jsonify({'error': "Rate limit exceeded by Garmin: Too many requests. Please try again later."}), 429
    except Exception as e:
        import traceback
        traceback.print_exc()
        err_msg = str(e)
        if "429" in err_msg.lower() or "too many requests" in err_msg.lower():
            return jsonify({'error': "Rate limit exceeded by Garmin: Too many requests. Please try again later."}), 429
        return jsonify({'status': 'needs_reauthentication', 'error': 'Garmin session expired or was revoked. Please reconnect.'}), 401

@garmin_bp.route('/api/garmin/import', methods=['POST'])
def import_garmin_activity():
    user_id = get_current_user_id()
    if not user_id:
        return jsonify({'error': 'Unauthorized. Please log in.'}), 401
        
    connection = get_garmin_connection(user_id)
    if not connection:
        return jsonify({'error': 'Garmin not connected'}), 400
        
    data = request.json or {}
    activity_id = data.get('activityId')
    if not activity_id:
        return jsonify({'error': 'activityId is required'}), 400
        
    from garminconnect import Garmin
    token_store = os.path.join(app.DATA_DIR, 'garmin_tokens', str(int(user_id)))
    
    try:
        client = Garmin()
        client.login(tokenstore=token_store)
        
        route_id = import_single_garmin_activity(user_id, client, activity_id)
        
        # Update last sync timestamp
        update_garmin_last_sync(user_id, datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        
        route = get_route(route_id)
        return jsonify({
            'status': 'success',
            'route_id': route_id,
            'name': route['name'] if route else f"Garmin Activity {activity_id}"
        })
    except GarminConnectTooManyRequestsError:
        return jsonify({'error': "Garmin import failed: Too many requests. Please try again later."}), 429
    except Exception as e:
        import traceback
        traceback.print_exc()
        err_msg = str(e)
        if "429" in err_msg.lower() or "too many requests" in err_msg.lower():
            return jsonify({'error': "Garmin import failed: Too many requests. Please try again later."}), 429
        return jsonify({'error': "Garmin import failed: An unexpected error occurred during import."}), 500

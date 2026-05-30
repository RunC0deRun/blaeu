import os
import hashlib
import requests
import json
import threading
from flask import Flask, request, jsonify, render_template, send_file, send_from_directory, session
from werkzeug.security import generate_password_hash, check_password_hash
from db import (
    init_db, add_route, get_routes, get_route, update_route, delete_route,
    create_folder, get_folders, delete_folder, get_all_tags, DATA_DIR, get_db,
    save_garmin_connection, get_garmin_connection, delete_garmin_connection, update_garmin_last_sync,
    add_user, get_user_by_username, get_user_by_id, get_users, delete_user, backfill_ownerless_data, count_users,
    update_user_default_map_style, update_route_poster_status
)
from gpx_parser import parse_gpx

app = Flask(__name__, template_folder='templates', static_folder='static')

# Initialize session secret key securely
secret_key = os.getenv('SECRET_KEY')
if not secret_key:
    secret_key_path = os.path.join(DATA_DIR, 'secret_key')
    if os.path.exists(secret_key_path):
        try:
            with open(secret_key_path, 'r', encoding='utf-8') as f:
                secret_key = f.read().strip()
        except Exception:
            pass
    if not secret_key:
        import secrets
        secret_key = secrets.token_hex(32)
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(secret_key_path, 'w', encoding='utf-8') as f:
                f.write(secret_key)
        except Exception:
            pass

app.secret_key = secret_key

# Initialize DB on load
init_db()

# Create directory to store actual GPX files
GPX_STORE_DIR = os.path.join(DATA_DIR, 'gpx')
os.makedirs(GPX_STORE_DIR, exist_ok=True)

# Create directory to store cached map tiles
TILES_CACHE_DIR = os.path.join(DATA_DIR, 'tiles_cache')
os.makedirs(TILES_CACHE_DIR, exist_ok=True)

# Background poster generation tracking state
poster_generations = {}
poster_generations_lock = threading.Lock()

def get_current_user_id():
    return session.get('user_id')


def run_async_poster_generation(route_id, user_id, theme_name):
    initial_status = {
        'status': 'generating',
        'progress': 0,
        'error': None
    }
    update_route_poster_status(route_id, initial_status)

    def worker():
        try:
            with app.app_context():
                route = get_route(route_id)
                if not route:
                    update_route_poster_status(route_id, {'status': 'failed', 'progress': 0, 'error': 'Route not found'})
                    return
                
                pts = route.get('simplified_path', [])
                if not pts:
                    try:
                        with open(route['file_path'], 'rb') as f:
                            content = f.read()
                        parsed = parse_gpx(content)
                        pts = []
                        for trk in parsed.get('tracks', []):
                            for seg in trk.get('segments', []):
                                for pt in seg:
                                    pts.append([pt['lat'], pt['lon']])
                    except Exception as e:
                        update_route_poster_status(route_id, {'status': 'failed', 'progress': 0, 'error': str(e)})
                        return
                
                if not pts:
                    update_route_poster_status(route_id, {'status': 'failed', 'progress': 0, 'error': 'No coordinates'})
                    return
                
                latitudes = [p[0] for p in pts]
                longitudes = [p[1] for p in pts]
                lat_min = min(latitudes)
                lat_max = max(latitudes)
                lon_min = min(longitudes)
                lon_max = max(longitudes)
                
                update_route_poster_status(route_id, {
                    'status': 'generating',
                    'progress': 1,
                    'error': None
                })
                
                from poster_map import generate_poster_background
                data = generate_poster_background(
                    route_id, lat_min, lat_max, lon_min, lon_max, theme_name
                )
                
                update_route_poster_status(route_id, {
                    'status': 'completed',
                    'progress': 5,
                    'error': None,
                    'theme': theme_name,
                    'image_url': data['image_url'],
                    'bounds': data['bounds'],
                    'bg_color': data['bg_color'],
                    'text_color': data['text_color'],
                    'display_city': data['display_city'],
                    'display_country': data['display_country']
                })
        except Exception as e:
            import traceback
            traceback.print_exc()
            update_route_poster_status(route_id, {
                'status': 'failed',
                'progress': 0,
                'error': str(e)
            })
                
    thread = threading.Thread(target=worker)
    thread.daemon = True
    thread.start()


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/favicon.ico')
def favicon():
    return '', 204


@app.route('/api/upload', methods=['POST'])
def upload_gpx():
    user_id = get_current_user_id()
    if not user_id:
        return jsonify({'error': 'Unauthorized. Please log in.'}), 401
        
    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400
        
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'Empty filename'}), 400

    try:
        content = file.read()
        file_hash = hashlib.sha256(content).hexdigest()
        
        # Check for duplicates in DB (scoped per user)
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM routes WHERE file_hash = ? AND user_id = ?", (file_hash, user_id))
        existing = cursor.fetchone()
        conn.close()
        
        if existing:
            return jsonify({
                'error': 'Duplicate file. This GPX route has already been uploaded.',
                'route_id': existing['id']
            }), 409
            
        # Parse GPX content
        parsed = parse_gpx(content)
        
        # Save file to disk
        file_name = file.filename
        saved_filename = f"{file_hash}.gpx"
        file_path = os.path.join(GPX_STORE_DIR, saved_filename)
        
        os.makedirs(GPX_STORE_DIR, exist_ok=True)
        with open(file_path, 'wb') as f:
            f.write(content)
            
        # Add metadata to DB
        folder_id = request.form.get('folder_id')
        if folder_id is None or folder_id == 'null' or folder_id == '':
            folder_id = None
        else:
            try:
                folder_id = int(folder_id)
            except (ValueError, TypeError):
                folder_id = None
                
        gpx_name = parsed['tracks'][0]['name'] if (parsed['tracks'] and parsed['tracks'][0]['name']) else None
        gpx_desc = parsed['tracks'][0]['desc'] if (parsed['tracks'] and parsed['tracks'][0]['desc']) else ''
        route_metadata = {
            'name': request.form.get('name') or gpx_name or file_name.rsplit('.', 1)[0],
            'description': request.form.get('description') or gpx_desc or '',
            'filename': file_name,
            'file_hash': file_hash,
            'file_path': file_path,
            'folder_id': folder_id,
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
            run_async_poster_generation(route_id, user_id, default_style)
        
        # Add initial tags if provided
        tags_raw = request.form.get('tags', '')
        if tags_raw:
            tags = [t.strip() for t in tags_raw.split(',') if t.strip()]
            update_route(route_id, tags=tags)
            
        new_route = get_route(route_id)
        if new_route:
            new_route['is_owner'] = True
        return jsonify(new_route), 201
        
    except ValueError as e:
        return jsonify({'error': f"GPX Parsing Error: {str(e)}"}), 400
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': f"Server Error: {str(e)}"}), 500


@app.route('/api/routes', methods=['GET'])
def list_routes():
    user_id = get_current_user_id()
    if not user_id:
        return jsonify({'error': 'Unauthorized. Please log in.'}), 401
        
    folder_id = request.args.get('folder_id')
    if folder_id:
        try:
            folder_id = int(folder_id)
        except ValueError:
            folder_id = None
    routes = get_routes(user_id, folder_id)
    # Add ownership property for client UI
    for r in routes:
        r['is_owner'] = (r['user_id'] == user_id)
    return jsonify(routes)


@app.route('/api/routes/<int:route_id>', methods=['GET'])
def get_route_details(route_id):
    user_id = get_current_user_id()
    if not user_id:
        return jsonify({'error': 'Unauthorized. Please log in.'}), 401
        
    route = get_route(route_id)
    if not route:
        return jsonify({'error': 'Route not found'}), 404
        
    # Check access permission
    if route['user_id'] != user_id and not route['is_public']:
        return jsonify({'error': 'Access denied to this private route'}), 403
        
    # Read the GPX file from disk to get coordinate paths and waypoints
    try:
        with open(route['file_path'], 'rb') as f:
            content = f.read()
        parsed = parse_gpx(content)
        
        # Merge parsed tracks and waypoints into the response
        response_data = dict(route)
        response_data['tracks'] = parsed['tracks']
        response_data['waypoints'] = parsed['waypoints']
        response_data['is_owner'] = (route['user_id'] == user_id)
        return jsonify(response_data)
    except Exception as e:
        return jsonify({'error': f"Could not read route coordinates: {str(e)}"}), 500


@app.route('/api/routes/<int:route_id>', methods=['PUT'])
def edit_route(route_id):
    user_id = get_current_user_id()
    if not user_id:
        return jsonify({'error': 'Unauthorized. Please log in.'}), 401
        
    route = get_route(route_id)
    if not route:
        return jsonify({'error': 'Route not found'}), 404
        
    if route['user_id'] != user_id:
        return jsonify({'error': 'Permission denied. You do not own this route.'}), 403
        
    data = request.json or {}
    name = data.get('name')
    description = data.get('description')
    folder_id = data.get('folder_id')
    tags = data.get('tags')
    is_public = data.get('is_public')
    
    try:
        update_route(route_id, name=name, description=description, folder_id=folder_id, tags=tags, is_public=is_public)
        updated = get_route(route_id)
        if not updated:
            return jsonify({'error': 'Route not found'}), 404
        updated['is_owner'] = True
        return jsonify(updated)
    except Exception as e:
        return jsonify({'error': f"Could not update route: {str(e)}"}), 500


@app.route('/api/routes/<int:route_id>', methods=['DELETE'])
def remove_route(route_id):
    user_id = get_current_user_id()
    if not user_id:
        return jsonify({'error': 'Unauthorized. Please log in.'}), 401
        
    route = get_route(route_id)
    if not route:
        return jsonify({'error': 'Route not found'}), 404
        
    if route['user_id'] != user_id:
        return jsonify({'error': 'Permission denied. You do not own this route.'}), 403
        
    try:
        delete_route(route_id)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': f"Could not delete route: {str(e)}"}), 500


@app.route('/api/folders', methods=['GET', 'POST'])
def handle_folders():
    user_id = get_current_user_id()
    if not user_id:
        return jsonify({'error': 'Unauthorized. Please log in.'}), 401
        
    if request.method == 'POST':
        data = request.json or {}
        name = data.get('name')
        if not name or not name.strip():
            return jsonify({'error': 'Folder name required'}), 400
        try:
            folder_id = create_folder(name, user_id)
            return jsonify({'id': folder_id, 'name': name.strip()}), 201
        except ValueError as e:
            return jsonify({'error': str(e)}), 409
        except Exception as e:
            return jsonify({'error': str(e)}), 500
    else:
        return jsonify(get_folders(user_id))


@app.route('/api/folders/<int:folder_id>', methods=['DELETE'])
def remove_folder(folder_id):
    user_id = get_current_user_id()
    if not user_id:
        return jsonify({'error': 'Unauthorized. Please log in.'}), 401
        
    try:
        delete_folder(folder_id, user_id)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': f"Could not delete folder: {str(e)}"}), 500


@app.route('/api/tags', methods=['GET'])
def list_tags():
    user_id = get_current_user_id()
    if not user_id:
        return jsonify({'error': 'Unauthorized. Please log in.'}), 401
        
    return jsonify(get_all_tags())


# Tile Proxy endpoint with local filesystem cache
@app.route('/api/tiles/<int:z>/<int:x>/<int:y>.png', methods=['GET'])
def get_map_tile(z, x, y):
    user_id = get_current_user_id()
    if not user_id:
        return jsonify({'error': 'Unauthorized. Please log in.'}), 401
        
    tile_dir = os.path.join(TILES_CACHE_DIR, str(z), str(x))
    tile_path = os.path.join(tile_dir, f"{y}.png")
    
    # Check local cache first
    if os.path.exists(tile_path):
        return send_file(tile_path, mimetype='image/png')
        
    # Fetch from CartoDB Dark Matter
    url = f"https://a.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png"
    headers = {
        'User-Agent': 'BlaeuGPXCartographer/1.0 (https://github.com/jan/blaeu)',
        'Referer': request.referrer or 'https://github.com/jan/blaeu'
    }
    
    try:
        os.makedirs(tile_dir, exist_ok=True)
        response = requests.get(url, headers=headers, timeout=10)
        
        if response.status_code == 200:
            with open(tile_path, 'wb') as f:
                f.write(response.content)
            return send_file(tile_path, mimetype='image/png')
        else:
            return jsonify({'error': f"Failed to fetch tile from OSM: {response.status_code}"}), response.status_code
    except Exception as e:
        return jsonify({'error': f"Tile proxy error: {str(e)}"}), 500
@app.route('/api/convert-video', methods=['POST'])
def convert_video():
    user_id = get_current_user_id()
    if not user_id:
        return jsonify({'error': 'Unauthorized. Please log in.'}), 401
        
    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400
        
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'Empty filename'}), 400
        
    import subprocess
    import tempfile
    
    temp_in_path = None
    temp_out_path = None
    
    try:
        # Create unique temp file paths
        with tempfile.NamedTemporaryFile(suffix='.webm', delete=False) as temp_in:
            temp_in_path = temp_in.name
        # Get and validate target format ('mp4' or 'webm')
        out_format = request.form.get('format', 'mp4')
        if out_format not in ['mp4', 'webm']:
            out_format = 'mp4'

        with tempfile.NamedTemporaryFile(suffix=f'.{out_format}', delete=False) as temp_out:
            temp_out_path = temp_out.name
            
        # Save input webm to temp file
        file.save(temp_in_path)
        
        # Get and validate FPS from request
        fps = request.form.get('fps', '30')
        try:
            fps_val = int(fps)
            if fps_val <= 0 or fps_val > 120:
                fps_val = 30
        except ValueError:
            fps_val = 30

        # Get and validate Bitrate from request
        bitrate = request.form.get('bitrate', '12000000')
        try:
            bitrate_val = int(bitrate)
            if bitrate_val <= 0:
                bitrate_val = 12000000
        except ValueError:
            bitrate_val = 12000000

        # Transcode WebM to target format using ffmpeg.
        # Use setpts to rewrite timestamps based on the frame index and the configured FPS,
        # forcing a constant framerate and preventing slow-motion/timecode mismatch.
        if out_format == 'webm':
            cmd = [
                'ffmpeg', '-y',
                '-i', temp_in_path,
                '-filter:v', f"setpts=N/({fps_val}*TB)",
                '-r', str(fps_val),
                '-c:v', 'libvpx',
                '-cpu-used', '5',
                '-crf', '4',
                '-b:v', f"{bitrate_val}",
                temp_out_path
            ]
        else:
            cmd = [
                'ffmpeg', '-y',
                '-i', temp_in_path,
                '-filter:v', f"setpts=N/({fps_val}*TB)",
                '-r', str(fps_val),
                '-c:v', 'libx264',
                '-crf', '20',
                '-pix_fmt', 'yuv420p',
                temp_out_path
            ]
        
        # Run conversion
        subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        
        # Return converted video file
        mimetype = 'video/mp4' if out_format == 'mp4' else 'video/webm'
        return send_file(
            temp_out_path,
            mimetype=mimetype,
            as_attachment=True,
            download_name=f'animation.{out_format}'
        )
        
    except subprocess.CalledProcessError as e:
        stderr_msg = e.stderr.decode('utf-8', errors='ignore') if e.stderr else 'No stderr output'
        return jsonify({'error': f"ffmpeg conversion failed: {stderr_msg}"}), 500
    except Exception as e:
        return jsonify({'error': f"Conversion failed: {str(e)}"}), 500
    finally:
        # Clean up temp files
        if temp_in_path and os.path.exists(temp_in_path):
            try:
                os.unlink(temp_in_path)
            except Exception:
                pass
        if temp_out_path and os.path.exists(temp_out_path):
            try:
                os.unlink(temp_out_path)
            except Exception:
                pass# Garmin Connect API Integration Endpoints
from garminconnect.exceptions import GarminConnectAuthenticationError, GarminConnectTooManyRequestsError

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
    file_path = os.path.join(GPX_STORE_DIR, filename)
    
    # Save to local GPX store
    os.makedirs(GPX_STORE_DIR, exist_ok=True)
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
        run_async_poster_generation(route_id, user_id, default_style)
        
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
    token_store = os.path.join(DATA_DIR, 'garmin_tokens', str(user_id))
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
        from datetime import datetime
        update_garmin_last_sync(user_id, datetime.now().strftime('%Y-%m-%d %H:%M:%S'))

def run_auto_sync_for_all_users():
    from db import get_all_active_garmin_connections, attempt_garmin_sync_lock
    from datetime import datetime
    
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
                print(f"[Auto-Sync] Locked and running Garmin Connect sync for user {user_id}...")
                try:
                    sync_user_garmin_activities_in_background(user_id)
                except Exception as e:
                    print(f"[Auto-Sync] Error syncing activities for user {user_id}: {e}")

def auto_sync_worker_loop():
    import time
    while True:
        try:
            time.sleep(60)
            with app.app_context():
                run_auto_sync_for_all_users()
        except Exception as e:
            print(f"[Auto-Sync Worker] Error in loop: {e}")

# Start the background daemon thread for periodic auto-sync
auto_sync_thread = threading.Thread(target=auto_sync_worker_loop)
auto_sync_thread.daemon = True
auto_sync_thread.start()

@app.route('/api/garmin/status', methods=['GET'])
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

@app.route('/api/garmin/auto-sync', methods=['PUT'])
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
        return jsonify({'error': f'Failed to update auto-sync interval: {str(e)}'}), 500

@app.route('/api/garmin/connect', methods=['POST'])
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
    token_store = os.path.join(DATA_DIR, 'garmin_tokens', str(user_id))
    os.makedirs(token_store, exist_ok=True)
    
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
    except GarminConnectTooManyRequestsError as e:
        import shutil
        if os.path.exists(token_store):
            shutil.rmtree(token_store, ignore_errors=True)
        import traceback
        traceback.print_exc()
        return jsonify({'error': f"Garmin connection failed: {str(e)}"}), 429
    except Exception as e:
        import shutil
        if os.path.exists(token_store):
            shutil.rmtree(token_store, ignore_errors=True)
        import traceback
        traceback.print_exc()
        err_msg = str(e)
        if "429" in err_msg.lower() or "too many requests" in err_msg.lower():
            return jsonify({'error': f"Garmin connection failed: {err_msg}"}), 429
        return jsonify({'error': f"Garmin connection failed: {err_msg}"}), 401

@app.route('/api/garmin/disconnect', methods=['POST'])
def disconnect_garmin():
    user_id = get_current_user_id()
    if not user_id:
        return jsonify({'error': 'Unauthorized. Please log in.'}), 401
        
    delete_garmin_connection(user_id)
    token_store = os.path.join(DATA_DIR, 'garmin_tokens', str(user_id))
    if os.path.exists(token_store):
        import shutil
        try:
            shutil.rmtree(token_store)
        except Exception:
            pass
    return jsonify({'status': 'disconnected'})

@app.route('/api/garmin/activities', methods=['GET'])
def get_garmin_activities():
    user_id = get_current_user_id()
    if not user_id:
        return jsonify({'error': 'Unauthorized. Please log in.'}), 401
        
    connection = get_garmin_connection(user_id)
    if not connection:
        return jsonify({'error': 'Garmin not connected'}), 400
        
    from garminconnect import Garmin
    token_store = os.path.join(DATA_DIR, 'garmin_tokens', str(user_id))
    
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
    except GarminConnectTooManyRequestsError as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': f"Rate limit exceeded by Garmin: {str(e)}"}), 429
    except Exception as e:
        import traceback
        traceback.print_exc()
        err_msg = str(e)
        if "429" in err_msg.lower() or "too many requests" in err_msg.lower():
            return jsonify({'error': f"Rate limit exceeded by Garmin: {err_msg}"}), 429
        # If authentication session tokens expired/revoked, prompt reconnect
        return jsonify({'status': 'needs_reauthentication', 'error': err_msg}), 401

@app.route('/api/garmin/import', methods=['POST'])
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
    token_store = os.path.join(DATA_DIR, 'garmin_tokens', str(user_id))
    
    try:
        client = Garmin()
        client.login(tokenstore=token_store)
        
        route_id = import_single_garmin_activity(user_id, client, activity_id)
        
        # Update last sync timestamp
        from datetime import datetime
        update_garmin_last_sync(user_id, datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        
        route = get_route(route_id)
        return jsonify({
            'status': 'success',
            'route_id': route_id,
            'name': route['name'] if route else f"Garmin Activity {activity_id}"
        })
    except GarminConnectTooManyRequestsError as e:
        return jsonify({'error': f"Garmin import failed: {str(e)}"}), 429
    except Exception as e:
        err_msg = str(e)
        if "429" in err_msg.lower() or "too many requests" in err_msg.lower():
            return jsonify({'error': f"Garmin import failed: {err_msg}"}), 429
        return jsonify({'error': f"Garmin import failed: {err_msg}"}), 500

# Map Poster Generation Endpoints
@app.route('/api/map-themes', methods=['GET'])
def list_map_themes():
    themes_dir = os.path.join(app.root_path, 'static', 'themes')
    if not os.path.exists(themes_dir):
        return jsonify([])
    
    themes = []
    for file in sorted(os.listdir(themes_dir)):
        if file.endswith('.json'):
            theme_id = file[:-5]
            try:
                with open(os.path.join(themes_dir, file), 'r', encoding='utf-8') as f:
                    data = json.load(f)
                themes.append({
                    'id': theme_id,
                    'name': data.get('name', theme_id),
                    'description': data.get('description', ''),
                    'bg': data.get('bg', '#FFFFFF'),
                    'text': data.get('text', '#000000')
                })
            except Exception:
                continue
    return jsonify(themes)


@app.route('/api/poster-maps/<filename>', methods=['GET'])
def get_poster_map_image(filename):
    user_id = get_current_user_id()
    if not user_id:
        return jsonify({'error': 'Unauthorized. Please log in.'}), 401
        
    from poster_map import POSTER_MAPS_DIR
    filename = os.path.basename(filename)
    file_path = os.path.join(POSTER_MAPS_DIR, filename)
    if os.path.exists(file_path):
        return send_file(file_path, mimetype='image/png')
    else:
        return jsonify({'error': 'Image not found'}), 404


@app.route('/api/routes/<int:route_id>/poster-map', methods=['GET'])
def get_route_poster_map(route_id):
    user_id = get_current_user_id()
    if not user_id:
        return jsonify({'error': 'Unauthorized. Please log in.'}), 401
        
    route = get_route(route_id)
    if not route:
        return jsonify({'error': 'Route not found'}), 404
        
    # Check access permission
    if route['user_id'] != user_id and not route['is_public']:
        return jsonify({'error': 'Access denied to this private route'}), 403
        
    theme_name = request.args.get('theme', 'noir')
    display_city = request.args.get('displayCity')
    display_country = request.args.get('displayCountry')
    
    # Bounding box query parameters (optional)
    lat_min = request.args.get('latMin')
    lat_max = request.args.get('latMax')
    lon_min = request.args.get('lonMin')
    lon_max = request.args.get('lonMax')
    
    if not (lat_min and lat_max and lon_min and lon_max):
        pts = route.get('simplified_path', [])
        if not pts:
            try:
                with open(route['file_path'], 'rb') as f:
                    content = f.read()
                parsed = parse_gpx(content)
                pts = []
                for trk in parsed.get('tracks', []):
                    for seg in trk.get('segments', []):
                        for pt in seg:
                            pts.append([pt['lat'], pt['lon']])
            except Exception as e:
                return jsonify({'error': f'Could not read route coordinates to compute bounds: {str(e)}'}), 500
        
        if not pts:
            return jsonify({'error': 'No coordinate points in route'}), 400
            
        latitudes = [p[0] for p in pts]
        longitudes = [p[1] for p in pts]
        lat_min = min(latitudes)
        lat_max = max(latitudes)
        lon_min = min(longitudes)
        lon_max = max(longitudes)
    else:
        try:
            lat_min = float(lat_min)
            lat_max = float(lat_max)
            lon_min = float(lon_min)
            lon_max = float(lon_max)
        except ValueError:
            return jsonify({'error': 'Invalid bounding box parameters'}), 400

    # Check if bounds are passed. If so, don't apply padding.
    lat_min_arg = request.args.get('latMin')
    lat_max_arg = request.args.get('latMax')
    lon_min_arg = request.args.get('lonMin')
    lon_max_arg = request.args.get('lonMax')
    
    apply_padding = True
    if lat_min_arg and lat_max_arg and lon_min_arg and lon_max_arg:
        apply_padding = False

    from poster_map import generate_poster_background
    try:
        data = generate_poster_background(
            route_id, lat_min, lat_max, lon_min, lon_max, theme_name,
            display_city=display_city, display_country=display_country,
            apply_padding=apply_padding
        )
        
        # Save completed details in poster_status column of DB if this is a default padding map
        if apply_padding:
            update_route_poster_status(route_id, {
                'status': 'completed',
                'progress': 5,
                'error': None,
                'theme': theme_name,
                'image_url': data['image_url'],
                'bounds': data['bounds'],
                'bg_color': data['bg_color'],
                'text_color': data['text_color'],
                'display_city': data['display_city'],
                'display_country': data['display_country']
            })
            
        return jsonify(data)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': f'Poster map generation failed: {str(e)}'}), 500


# ==========================================
# User Authentication & Management Endpoints
# ==========================================

@app.route('/api/auth/register', methods=['POST'])
def register():
    data = request.json or {}
    username = data.get('username')
    password = data.get('password')
    
    if not username or not password:
        return jsonify({'error': 'Username and password are required'}), 400
        
    username = username.strip()
    if len(username) < 3 or len(password) < 4:
        return jsonify({'error': 'Username must be at least 3 chars, password at least 4 chars'}), 400
        
    try:
        is_first = (count_users() == 0)
        is_admin = 1 if is_first else 0
        
        password_hash = generate_password_hash(password)
        user_id = add_user(username, password_hash, is_admin)
        
        # Backfill existing ownerless data to the first admin user
        if is_first:
            backfill_ownerless_data(user_id)
            
        session['user_id'] = user_id
        return jsonify({
            'success': True,
            'user': {
                'id': user_id,
                'username': username,
                'is_admin': is_admin,
                'default_map_style': 'dark'
            }
        }), 201
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': f"Registration failed: {str(e)}"}), 500


@app.route('/api/auth/login', methods=['POST'])
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
    return jsonify({
        'success': True,
        'user': {
            'id': user['id'],
            'username': user['username'],
            'is_admin': user['is_admin'],
            'default_map_style': user.get('default_map_style', 'dark')
        }
    })


@app.route('/api/auth/logout', methods=['POST'])
def logout():
    session.pop('user_id', None)
    return jsonify({'success': True})


@app.route('/api/auth/status', methods=['GET'])
def auth_status():
    user_id = session.get('user_id')
    if not user_id:
        no_users_exist = (count_users() == 0)
        return jsonify({
            'logged_in': False,
            'no_users_exist': no_users_exist
        })
        
    user = get_user_by_id(user_id)
    if not user:
        session.pop('user_id', None)
        return jsonify({'logged_in': False, 'no_users_exist': count_users() == 0})
        
    return jsonify({
        'logged_in': True,
        'user': {
            'id': user['id'],
            'username': user['username'],
            'is_admin': user['is_admin'],
            'default_map_style': user.get('default_map_style', 'dark')
        }
    })


@app.route('/api/auth/default-map-style', methods=['PUT'])
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
        return jsonify({'error': str(e)}), 500


@app.route('/api/auth/users', methods=['GET'])
def list_users():
    admin_id = session.get('user_id')
    if not admin_id:
        return jsonify({'error': 'Unauthorized'}), 401
        
    admin = get_user_by_id(admin_id)
    if not admin or not admin['is_admin']:
        return jsonify({'error': 'Forbidden. Admin privileges required.'}), 403
        
    return jsonify(get_users())


@app.route('/api/auth/users/<int:delete_user_id>', methods=['DELETE'])
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
        return jsonify({'error': f"Failed to delete user: {str(e)}"}), 500


if __name__ == '__main__':
    # Run the server
    app.run(host='0.0.0.0', port=5000, debug=True)

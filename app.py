import os
import hashlib
import requests
import json
from flask import Flask, request, jsonify, render_template, send_file, send_from_directory
from db import (
    init_db, add_route, get_routes, get_route, update_route, delete_route,
    create_folder, get_folders, delete_folder, get_all_tags, DATA_DIR, get_db,
    save_garmin_connection, get_garmin_connection, delete_garmin_connection, update_garmin_last_sync
)
from gpx_parser import parse_gpx

app = Flask(__name__, template_folder='templates', static_folder='static')

# Initialize DB on load
init_db()

# Create directory to store actual GPX files
GPX_STORE_DIR = os.path.join(DATA_DIR, 'gpx')
os.makedirs(GPX_STORE_DIR, exist_ok=True)

# Create directory to store cached map tiles
TILES_CACHE_DIR = os.path.join(DATA_DIR, 'tiles_cache')
os.makedirs(TILES_CACHE_DIR, exist_ok=True)


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/favicon.ico')
def favicon():
    return '', 204


@app.route('/api/upload', methods=['POST'])
def upload_gpx():
    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400
        
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'Empty filename'}), 400

    try:
        content = file.read()
        file_hash = hashlib.sha256(content).hexdigest()
        
        # Check for duplicates in DB
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM routes WHERE file_hash = ?", (file_hash,))
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
            'timezone': parsed.get('timezone'),
            'created_at': parsed.get('start_time'),
            'simplified_path': json.dumps(parsed.get('simplified_path', []))
        }
        
        route_id = add_route(route_metadata, parsed['statistics'])
        
        # Add initial tags if provided
        tags_raw = request.form.get('tags', '')
        if tags_raw:
            tags = [t.strip() for t in tags_raw.split(',') if t.strip()]
            update_route(route_id, tags=tags)
            
        new_route = get_route(route_id)
        return jsonify(new_route), 201
        
    except ValueError as e:
        return jsonify({'error': f"GPX Parsing Error: {str(e)}"}), 400
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': f"Server Error: {str(e)}"}), 500


@app.route('/api/routes', methods=['GET'])
def list_routes():
    folder_id = request.args.get('folder_id')
    if folder_id:
        try:
            folder_id = int(folder_id)
        except ValueError:
            folder_id = None
    routes = get_routes(folder_id)
    return jsonify(routes)


@app.route('/api/routes/<int:route_id>', methods=['GET'])
def get_route_details(route_id):
    route = get_route(route_id)
    if not route:
        return jsonify({'error': 'Route not found'}), 404
        
    # Read the GPX file from disk to get coordinate paths and waypoints
    try:
        with open(route['file_path'], 'rb') as f:
            content = f.read()
        parsed = parse_gpx(content)
        
        # Merge parsed tracks and waypoints into the response
        response_data = dict(route)
        response_data['tracks'] = parsed['tracks']
        response_data['waypoints'] = parsed['waypoints']
        return jsonify(response_data)
    except Exception as e:
        return jsonify({'error': f"Could not read route coordinates: {str(e)}"}), 500


@app.route('/api/routes/<int:route_id>', methods=['PUT'])
def edit_route(route_id):
    data = request.json or {}
    name = data.get('name')
    description = data.get('description')
    folder_id = data.get('folder_id')
    tags = data.get('tags')
    
    try:
        update_route(route_id, name=name, description=description, folder_id=folder_id, tags=tags)
        updated = get_route(route_id)
        if not updated:
            return jsonify({'error': 'Route not found'}), 404
        return jsonify(updated)
    except Exception as e:
        return jsonify({'error': f"Could not update route: {str(e)}"}), 500


@app.route('/api/routes/<int:route_id>', methods=['DELETE'])
def remove_route(route_id):
    route = get_route(route_id)
    if not route:
        return jsonify({'error': 'Route not found'}), 404
    try:
        delete_route(route_id)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': f"Could not delete route: {str(e)}"}), 500


@app.route('/api/folders', methods=['GET', 'POST'])
def handle_folders():
    if request.method == 'POST':
        data = request.json or {}
        name = data.get('name')
        if not name or not name.strip():
            return jsonify({'error': 'Folder name required'}), 400
        try:
            folder_id = create_folder(name)
            return jsonify({'id': folder_id, 'name': name.strip()}), 201
        except ValueError as e:
            return jsonify({'error': str(e)}), 409
        except Exception as e:
            return jsonify({'error': str(e)}), 500
    else:
        return jsonify(get_folders())


@app.route('/api/folders/<int:folder_id>', methods=['DELETE'])
def remove_folder(folder_id):
    try:
        delete_folder(folder_id)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': f"Could not delete folder: {str(e)}"}), 500


@app.route('/api/tags', methods=['GET'])
def list_tags():
    return jsonify(get_all_tags())


# Tile Proxy endpoint with local filesystem cache
@app.route('/api/tiles/<int:z>/<int:x>/<int:y>.png', methods=['GET'])
def get_map_tile(z, x, y):
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

@app.route('/api/garmin/status', methods=['GET'])
def get_garmin_status():
    user_id = 1 # Default single-user
    connection = get_garmin_connection(user_id)
    if connection:
        return jsonify({
            'status': 'connected',
            'email': connection['email'],
            'display_name': connection['display_name'],
            'last_sync': connection['last_sync']
        })
    else:
        return jsonify({'status': 'disconnected'})

@app.route('/api/garmin/connect', methods=['POST'])
def connect_garmin():
    user_id = 1 # Default single-user
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
    user_id = 1 # Default single-user
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
    user_id = 1 # Default single-user
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
    user_id = 1 # Default single-user
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
            track_name = f"Garmin Activity {activity_id}"

        # Register in SQLite database routes
        route_metadata = {
            'name': track_name,
            'description': f"Imported from Garmin Connect (Activity ID: {activity_id})",
            'filename': filename,
            'file_hash': file_hash,
            'file_path': file_path,
            'folder_id': None,
            'timezone': parsed.get('timezone'),
            'simplified_path': json.dumps(parsed.get('simplified_path', []))
        }
        
        route_id = add_route(route_metadata, parsed['statistics'])
        
        # Update last sync timestamp
        from datetime import datetime
        update_garmin_last_sync(user_id, datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        
        return jsonify({
            'status': 'success',
            'route_id': route_id,
            'name': route_metadata['name']
        })
    except GarminConnectTooManyRequestsError as e:
        return jsonify({'error': f"Garmin import failed: {str(e)}"}), 429
    except Exception as e:
        err_msg = str(e)
        if "429" in err_msg.lower() or "too many requests" in err_msg.lower():
            return jsonify({'error': f"Garmin import failed: {err_msg}"}), 429
        return jsonify({'error': f"Garmin import failed: {err_msg}"}), 500


if __name__ == '__main__':
    # Run the server
    app.run(host='0.0.0.0', port=5000, debug=True)

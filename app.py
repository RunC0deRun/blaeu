import os
import hashlib
import requests
from flask import Flask, request, jsonify, render_template, send_file, send_from_directory
from db import (
    init_db, add_route, get_routes, get_route, update_route, delete_route,
    create_folder, get_folders, delete_folder, get_all_tags, DATA_DIR, get_db
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
            'created_at': parsed.get('start_time')
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


if __name__ == '__main__':
    # Run the server
    app.run(host='0.0.0.0', port=5000, debug=True)

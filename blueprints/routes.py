import os
import json
import hashlib
from flask import Blueprint, request, jsonify, send_file
from db import (
    get_db, add_route, get_routes, get_route, update_route, delete_route,
    get_all_tags, get_user_by_id, update_route_poster_status
)
from gpx_parser import parse_gpx
import app
from utils import (
    get_current_user_id, rate_limit, run_async_poster_generation
)

routes_bp = Blueprint('routes', __name__)

@routes_bp.route('/api/upload', methods=['POST'])
@rate_limit(limit=10, period=60)
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
        file_path = os.path.join(app.GPX_STORE_DIR, saved_filename)
        
        os.makedirs(app.GPX_STORE_DIR, exist_ok=True)
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
            app.run_async_poster_generation(route_id, user_id, default_style)
        
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
        return jsonify({'error': 'Server Error: An unexpected error occurred.'}), 500


@routes_bp.route('/api/routes', methods=['GET'])
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
    sort_by = request.args.get('sort_by')
    sort_order = request.args.get('sort_order')
    routes = get_routes(user_id, folder_id, sort_by=sort_by, sort_order=sort_order)
    # Add ownership property for client UI
    for r in routes:
        r['is_owner'] = (r['user_id'] == user_id)
    return jsonify(routes)


@routes_bp.route('/api/routes/<int:route_id>', methods=['GET'])
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
        import traceback
        traceback.print_exc()
        return jsonify({'error': 'Could not read route coordinates: Failed to read or parse file.'}), 500


@routes_bp.route('/api/routes/<int:route_id>', methods=['PUT'])
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
        import traceback
        traceback.print_exc()
        return jsonify({'error': 'Could not update route: An unexpected error occurred.'}), 500


@routes_bp.route('/api/routes/<int:route_id>', methods=['DELETE'])
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
        import traceback
        traceback.print_exc()
        return jsonify({'error': 'Could not delete route: An unexpected error occurred.'}), 500


@routes_bp.route('/api/tags', methods=['GET'])
def list_tags():
    user_id = get_current_user_id()
    if not user_id:
        return jsonify({'error': 'Unauthorized. Please log in.'}), 401
        
    return jsonify(get_all_tags(user_id))


# Map Poster Generation Endpoints
@routes_bp.route('/api/map-themes', methods=['GET'])
def list_map_themes():
    # Import current_app dynamically to avoid import-time app context errors
    from flask import current_app
    themes_dir = os.path.join(current_app.root_path, 'static', 'themes')
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


@routes_bp.route('/api/poster-maps/<filename>', methods=['GET'])
def get_poster_map_image(filename):
    user_id = get_current_user_id()
    if not user_id:
        return jsonify({'error': 'Unauthorized. Please log in.'}), 401
        
    from poster_map import POSTER_MAPS_DIR
    filename = os.path.basename(filename)
    
    # Enforce strict ownership / route publicity check
    parts = filename.split('_')
    if parts:
        try:
            route_id = int(parts[0])
            route = get_route(route_id)
            if route:
                if route['user_id'] != user_id and not route['is_public']:
                    return jsonify({'error': 'Access denied'}), 403
            else:
                return jsonify({'error': 'Image not found'}), 404
        except (ValueError, IndexError):
            pass
            
    file_path = os.path.join(POSTER_MAPS_DIR, filename)
    if os.path.exists(file_path):
        return send_file(file_path, mimetype='image/png')
    else:
        return jsonify({'error': 'Image not found'}), 404


@routes_bp.route('/api/routes/<int:route_id>/poster-map', methods=['GET'])
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
                import traceback
                traceback.print_exc()
                return jsonify({'error': 'Failed to read route coordinates: An unexpected server error occurred.'}), 500
        
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
        return jsonify({'error': 'Poster map generation failed: An unexpected error occurred.'}), 500

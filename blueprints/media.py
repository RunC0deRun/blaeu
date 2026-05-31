import os
import requests
from flask import Blueprint, request, jsonify, send_file
import app
from utils import (
    get_current_user_id, rate_limit
)

media_bp = Blueprint('media', __name__)

# Tile Proxy endpoint with local filesystem cache
@media_bp.route('/api/tiles/<int:z>/<int:x>/<int:y>.png', methods=['GET'])
def get_map_tile(z, x, y):
    user_id = get_current_user_id()
    if not user_id:
        return jsonify({'error': 'Unauthorized. Please log in.'}), 401
        
    if not (0 <= z <= 19):
        return jsonify({'error': 'Invalid zoom level. Zoom must be between 0 and 19.'}), 400
        
    max_val = 1 << z
    if not (0 <= x < max_val) or not (0 <= y < max_val):
        return jsonify({'error': 'Tile coordinates out of bounds.'}), 400
        
    tile_dir = os.path.join(app.TILES_CACHE_DIR, str(z), str(x))
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
        import traceback
        traceback.print_exc()
        return jsonify({'error': 'Tile proxy error: Failed to fetch or process tile.'}), 500


@media_bp.route('/api/convert-video', methods=['POST'])
@rate_limit(limit=3, period=60)
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
            if bitrate_val <= 0 or bitrate_val > 100000000:
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
        import traceback
        traceback.print_exc()
        return jsonify({'error': 'ffmpeg conversion failed: Video transcoding failed.'}), 500
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': 'Conversion failed: An unexpected error occurred.'}), 500
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
                pass

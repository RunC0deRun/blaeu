import os
import secrets
import threading
import logging

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
logger = logging.getLogger('blaeu')
from flask import Flask, jsonify, render_template, session, request

from db import init_db, DATA_DIR
from utils import get_csrf_token, rate_limit_records, run_async_poster_generation

GPX_STORE_DIR = os.path.join(DATA_DIR, 'gpx')
TILES_CACHE_DIR = os.path.join(DATA_DIR, 'tiles_cache')

# Initialize Flask app
app = Flask(__name__, template_folder='templates', static_folder='static')
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50 MB limit
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = os.getenv('SESSION_COOKIE_SECURE', 'true').lower() == 'true'

@app.errorhandler(413)
def request_entity_too_large(error):
    return jsonify({'error': 'File too large. Maximum size allowed is 50MB.'}), 413

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

# Create directories to store GPX files and map tiles
os.makedirs(GPX_STORE_DIR, exist_ok=True)
os.makedirs(TILES_CACHE_DIR, exist_ok=True)

# CSRF Protection
@app.before_request
def csrf_protect():
    # Allow bypassing CSRF check in tests
    if app.config.get('TESTING'):
        return
        
    if request.method in ['POST', 'PUT', 'DELETE']:
        exempt_paths = ['/api/auth/login', '/api/auth/register']
        if request.path in exempt_paths:
            return
            
        session_token = session.get('csrf_token')
        header_token = request.headers.get('X-CSRF-Token')
        
        if not session_token or not header_token or session_token != header_token:
            return jsonify({'error': 'CSRF token validation failed. Please refresh the page.'}), 400

# Import Blueprints
from blueprints.auth import auth_bp
from blueprints.routes import routes_bp
from blueprints.folders import folders_bp
from blueprints.garmin import garmin_bp, run_auto_sync_for_all_users, MfaRequiredException
from blueprints.media import media_bp

# Register Blueprints
app.register_blueprint(auth_bp)
app.register_blueprint(routes_bp)
app.register_blueprint(folders_bp)
app.register_blueprint(garmin_bp)
app.register_blueprint(media_bp)

# Garmin Auto-Sync Background Scheduler Thread
def auto_sync_worker_loop():
    import time
    while True:
        try:
            time.sleep(60)
            with app.app_context():
                run_auto_sync_for_all_users()
        except Exception as e:
            logger.error(f"[Auto-Sync Worker] Error in loop: {e}", exc_info=True)

# Start the background daemon thread for periodic auto-sync
auto_sync_thread = threading.Thread(target=auto_sync_worker_loop)
auto_sync_thread.daemon = True
auto_sync_thread.start()

# Main Frontend Routes
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/favicon.ico')
def favicon():
    return '', 204

if __name__ == '__main__':
    # Run the server
    debug_mode = os.getenv('FLASK_DEBUG', 'false').lower() == 'true'
    app.run(host='0.0.0.0', port=5000, debug=debug_mode)

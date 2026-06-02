import os
import time
import threading
import logging
from functools import wraps
from typing import Callable, Optional, Any, Dict, List
from flask import request, jsonify, session, current_app
from db import DATA_DIR, update_route_poster_status, get_route


# Rate limit state
from collections import defaultdict
rate_limit_records = defaultdict(list)
rate_limit_lock = threading.Lock()

logger = logging.getLogger('blaeu.utils')

def rate_limit(limit: int, period: int) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(f: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(f)
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            if current_app.config.get('TESTING'):
                return f(*args, **kwargs)
                
            ip = request.remote_addr
            key = f"{f.__name__}:{ip}"
            now = time.time()
            
            with rate_limit_lock:
                rate_limit_records[key] = [t for t in rate_limit_records[key] if now - t < period]
                if len(rate_limit_records[key]) >= limit:
                    return jsonify({'error': 'Rate limit exceeded. Please try again later.'}), 429
                rate_limit_records[key].append(now)
                
            return f(*args, **kwargs)
        return wrapped
    return decorator

def get_current_user_id() -> Optional[int]:
    return session.get('user_id')

def get_csrf_token() -> str:
    if 'csrf_token' not in session:
        import secrets
        session['csrf_token'] = secrets.token_hex(32)
    return session['csrf_token']

def run_async_poster_generation(route_id: int, user_id: int, theme_name: str) -> None:
    # Retrieve the actual application object to pass across threads safely
    app = current_app._get_current_object()
    
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
                        from gpx_parser import parse_gpx
                        with open(route['file_path'], 'rb') as f:
                            content = f.read()
                        parsed = parse_gpx(content)
                        pts = []
                        for trk in parsed.get('tracks', []):
                            for seg in trk.get('segments', []):
                                for pt in seg:
                                    pts.append([pt['lat'], pt['lon']])
                    except Exception as e:
                        logger.exception("GPX parsing failed for poster generation")
                        update_route_poster_status(route_id, {'status': 'failed', 'progress': 0, 'error': 'Failed to parse GPX data.'})
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
            logger.exception("Error during async poster generation")
            update_route_poster_status(route_id, {
                'status': 'failed',
                'progress': 0,
                'error': 'An unexpected error occurred during poster style generation.'
            })
                
    thread = threading.Thread(target=worker)
    thread.daemon = True
    thread.start()

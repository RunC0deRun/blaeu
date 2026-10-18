import os
import time
import queue
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

# Background poster generation runs through a single queue per process, so a bulk import
# doesn't fire one Overpass query per route at once. Jobs are spaced out, and when Overpass
# is unreachable the whole queue backs off exponentially before the failed job is retried.
OVERPASS_MIN_INTERVAL = float(os.getenv('BLAEU_OVERPASS_MIN_INTERVAL', '5'))
POSTER_MAX_ATTEMPTS = int(os.getenv('BLAEU_POSTER_MAX_ATTEMPTS', '5'))
POSTER_BACKOFF_BASE = 30
POSTER_BACKOFF_MAX = 900

_poster_queue: "queue.Queue[Dict[str, Any]]" = queue.Queue()
_poster_worker: Optional[threading.Thread] = None
_poster_worker_lock = threading.Lock()


def run_async_poster_generation(route_id: int, user_id: int, theme_name: str) -> None:
    update_route_poster_status(route_id, {
        'status': 'generating',
        'progress': 0,
        'error': None
    })
    _poster_queue.put({
        'app': current_app._get_current_object(),
        'route_id': route_id,
        'user_id': user_id,
        'theme_name': theme_name,
        'attempt': 1
    })
    _ensure_poster_worker()


def _ensure_poster_worker() -> None:
    global _poster_worker
    with _poster_worker_lock:
        if _poster_worker is None or not _poster_worker.is_alive():
            _poster_worker = threading.Thread(target=_poster_worker_loop, name='poster-queue', daemon=True)
            _poster_worker.start()


def _backoff_delay(consecutive_failures: int) -> float:
    return min(POSTER_BACKOFF_BASE * 2 ** (consecutive_failures - 1), POSTER_BACKOFF_MAX)


def _poster_worker_loop() -> None:
    from poster_map import OSMNetworkError

    consecutive_failures = 0
    next_allowed = 0.0
    while True:
        job = _poster_queue.get()
        try:
            wait = next_allowed - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            route_id = job['route_id']
            try:
                with job['app'].app_context():
                    _generate_route_poster(route_id, job['user_id'], job['theme_name'])
                consecutive_failures = 0
                next_allowed = time.monotonic() + OVERPASS_MIN_INTERVAL
            except OSMNetworkError as e:
                consecutive_failures += 1
                delay = _backoff_delay(consecutive_failures)
                next_allowed = time.monotonic() + delay
                if job['attempt'] < POSTER_MAX_ATTEMPTS:
                    logger.warning(
                        f"Async poster generation for route {route_id} failed "
                        f"(attempt {job['attempt']}/{POSTER_MAX_ATTEMPTS}): {e} Backing off {delay:.0f}s."
                    )
                    _poster_queue.put({**job, 'attempt': job['attempt'] + 1})
                else:
                    logger.warning(f"Async poster generation for route {route_id} failed after {job['attempt']} attempts: {e}")
                    update_route_poster_status(route_id, {
                        'status': 'failed',
                        'progress': 0,
                        'error': str(e)
                    })
            except Exception:
                logger.exception("Error during async poster generation")
                next_allowed = time.monotonic() + OVERPASS_MIN_INTERVAL
                update_route_poster_status(route_id, {
                    'status': 'failed',
                    'progress': 0,
                    'error': 'An unexpected error occurred during poster style generation.'
                })
        finally:
            _poster_queue.task_done()


def _generate_route_poster(route_id: int, user_id: int, theme_name: str) -> None:
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
    
    from db import get_user_by_id
    user = get_user_by_id(user_id)
    aspect_ratio = user.get('default_aspect_ratio', '16:9') if user else '16:9'

    from poster_map import generate_poster_background
    data = generate_poster_background(
        route_id, lat_min, lat_max, lon_min, lon_max, theme_name,
        aspect_ratio=aspect_ratio
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
        'display_country': data['display_country'],
        'aspect_ratio': aspect_ratio
    })

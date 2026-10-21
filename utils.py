import os
import json
import time
import threading
import logging
from functools import wraps
from typing import Callable, Optional, Any, Dict, List
from flask import request, jsonify, session, current_app
from datetime import datetime, timedelta, timezone
from db import (
    DATA_DIR, update_route_poster_status, get_route, get_user_by_id, enqueue_map_render,
    get_map_render_jobs, get_user_route_posters, clear_map_render_queue, poster_maps_dir, claim_next_map_render, update_map_render_job,
)


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

# Background poster generation runs through one queue in the database (map_render_jobs), so
# it survives restarts and all gunicorn workers share it. Every process runs a worker that
# claims one job at a time: only one map renders at once across the whole instance, jobs are
# spaced out so a bulk import doesn't fire one Overpass query per route at once, and when
# Overpass is unreachable the whole queue backs off exponentially before the failed job is retried.
OVERPASS_MIN_INTERVAL = float(os.getenv('BLAEU_OVERPASS_MIN_INTERVAL', '5'))
POSTER_MAX_ATTEMPTS = int(os.getenv('BLAEU_POSTER_MAX_ATTEMPTS', '5'))
POSTER_BACKOFF_BASE = 30
POSTER_BACKOFF_MAX = 900
MAP_RENDER_POLL_SECONDS = 3
MAP_RENDER_LEASE_MINUTES = 30
MAP_RENDER_START_DELAY = 30
# 'dark' uses live map tiles, so there is no poster to render
LIVE_TILE_STYLE = 'dark'

_poster_worker: Optional[threading.Thread] = None
_poster_worker_lock = threading.Lock()


class PosterRenderError(Exception):
    """A route's map can't be rendered, and retrying won't help."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _fmt(dt: datetime) -> str:
    return dt.strftime('%Y-%m-%d %H:%M:%S')


def render_theme(user: Optional[Dict[str, Any]]) -> Optional[str]:
    """The poster theme the user's maps are rendered in; None when they use live tiles."""
    style = (user or {}).get('default_map_style') or LIVE_TILE_STYLE
    return None if style == LIVE_TILE_STYLE else style


def poster_image_exists(poster_status: Optional[Dict[str, Any]]) -> bool:
    """Whether a completed poster's image is still on disk (the cache can be cleared or not copied)."""
    filename = os.path.basename((poster_status or {}).get('image_url') or '')
    return bool(filename) and os.path.exists(os.path.join(poster_maps_dir(), filename))


def drop_missing_poster(route: Dict[str, Any]) -> Dict[str, Any]:
    """Forgets a completed poster whose image is gone, so it's rendered again instead of shown blank."""
    poster = route.get('poster_status')
    if poster and poster.get('status') == 'completed' and not poster_image_exists(poster):
        route['poster_status'] = None
    return route


def is_map_rendered(poster_status: Optional[Dict[str, Any]], user: Optional[Dict[str, Any]]) -> bool:
    """Whether the route has a finished poster map in the user's default style and aspect ratio."""
    theme = render_theme(user)
    return bool(
        theme and poster_status and poster_status.get('status') == 'completed'
        and poster_status.get('theme') == theme
        and poster_status.get('aspect_ratio') == ((user or {}).get('default_aspect_ratio') or '16:9')
        and poster_image_exists(poster_status)
    )


def run_async_poster_generation(route_id: int, user_id: int, theme_name: str) -> None:
    enqueue_map_render(route_id, user_id, theme_name)
    update_route_poster_status(route_id, {'status': 'queued', 'progress': 0, 'error': None})


def queue_unrendered_maps(user_id: int, retry_failed: bool = False) -> int:
    """
    Queues every route of the user without a rendered map. Routes already waiting, and ones
    that failed in the same theme, are skipped unless retry_failed is set. Returns how many
    were queued.
    """
    user = get_user_by_id(user_id)
    theme = render_theme(user)
    if not theme:
        clear_map_render_queue(user_id)
        return 0
    jobs = {j['route_id']: j for j in get_map_render_jobs(user_id)}
    queued = 0
    for route in get_user_route_posters(user_id):
        if is_map_rendered(route['poster_status'], user):
            continue
        job = jobs.get(route['id'])
        if job and job['theme'] == theme and (
                job['status'] in ('queued', 'running') or (job['status'] == 'failed' and not retry_failed)):
            continue
        run_async_poster_generation(route['id'], user_id, theme)
        queued += 1
    if queued:
        logger.info(f"Queued {queued} map renders for user {user_id}")
    return queued


def map_render_queue_status(user_id: int) -> Dict[str, Any]:
    """How far rendering the user's maps has come, for the welcome screen."""
    user = get_user_by_id(user_id)
    theme = render_theme(user)
    routes = get_user_route_posters(user_id)
    jobs = [j for j in get_map_render_jobs(user_id) if j['theme'] == theme]
    counts = {status: sum(1 for j in jobs if j['status'] == status) for status in ('queued', 'running', 'done', 'failed')}

    current = None
    running = next((j for j in jobs if j['status'] == 'running'), None)
    if running:
        try:
            poster = json.loads(running['poster_status']) if running['poster_status'] else {}
        except (TypeError, ValueError):
            poster = {}
        current = {'route_id': running['route_id'], 'name': running['route_name'],
                   'progress': poster.get('progress') or 0 if poster.get('status') == 'generating' else 0}

    now = _fmt(_utcnow())
    waiting = [j for j in jobs if j['status'] == 'queued' and j['attempts'] and (j['next_attempt_at'] or '') > now]
    return {
        'theme': theme,
        'routes': len(routes),
        'rendered': sum(1 for r in routes if is_map_rendered(r['poster_status'], user)),
        'queued': counts['queued'] + counts['running'],
        'done': counts['done'],
        'failed': counts['failed'],
        'current': current,
        'retry_at': min(j['next_attempt_at'] for j in waiting) if waiting else None,
        'last_error': waiting[0]['last_error'] if waiting else None,
        'updated_at': max((j['updated_at'] for j in jobs if j['updated_at']), default=None),
    }


def _backoff_delay(attempts: int) -> float:
    return min(POSTER_BACKOFF_BASE * 2 ** (attempts - 1), POSTER_BACKOFF_MAX)


def process_next_map_render() -> bool:
    """Claims and renders the next queued map. Returns False when there was nothing to do."""
    from poster_map import OSMNetworkError

    now = _utcnow()
    job = claim_next_map_render(_fmt(now), _fmt(now + timedelta(minutes=MAP_RENDER_LEASE_MINUTES)))
    if not job:
        return False
    route_id = job['route_id']
    attempts = job['attempts'] + 1
    try:
        _generate_route_poster(route_id, job['user_id'], job['theme'])
    except OSMNetworkError as e:
        if attempts < POSTER_MAX_ATTEMPTS:
            delay = _backoff_delay(attempts)
            logger.warning(
                f"Poster generation for route {route_id} failed "
                f"(attempt {attempts}/{POSTER_MAX_ATTEMPTS}): {e} Backing off {delay:.0f}s."
            )
            update_map_render_job(route_id, status='queued', attempts=attempts, last_error=str(e), locked_until=None,
                                  next_attempt_at=_fmt(_utcnow() + timedelta(seconds=delay)))
            update_route_poster_status(route_id, {'status': 'queued', 'progress': 0, 'error': None})
        else:
            logger.warning(f"Poster generation for route {route_id} failed after {attempts} attempts: {e}")
            _fail_map_render(route_id, attempts, str(e))
        return True
    except PosterRenderError as e:
        _fail_map_render(route_id, attempts, str(e))
        return True
    except Exception:
        logger.exception("Error during async poster generation")
        _fail_map_render(route_id, attempts, 'An unexpected error occurred during poster style generation.')
        return True

    update_map_render_job(route_id, status='done', attempts=0, last_error=None, next_attempt_at=None,
                          locked_until=_fmt(_utcnow() + timedelta(seconds=OVERPASS_MIN_INTERVAL)))
    return True


def _fail_map_render(route_id: int, attempts: int, message: str) -> None:
    update_map_render_job(route_id, status='failed', attempts=attempts, last_error=message,
                          locked_until=_fmt(_utcnow() + timedelta(seconds=OVERPASS_MIN_INTERVAL)))
    update_route_poster_status(route_id, {'status': 'failed', 'progress': 0, 'error': message})


def start_map_render_worker(app: Any) -> None:
    """Starts this process's map render worker (once)."""
    global _poster_worker
    with _poster_worker_lock:
        if _poster_worker is None or not _poster_worker.is_alive():
            _poster_worker = threading.Thread(target=_poster_worker_loop, args=(app,), name='map-render-queue', daemon=True)
            _poster_worker.start()


def _poster_worker_loop(app: Any) -> None:
    # Like the auto-sync worker, start a little after boot (tests switch on TESTING by then)
    time.sleep(MAP_RENDER_START_DELAY)
    while True:
        worked = False
        try:
            # Tests drive the queue themselves
            if not app.config.get('TESTING'):
                with app.app_context():
                    worked = process_next_map_render()
        except Exception:
            logger.exception("Map render worker error")
        if not worked:
            time.sleep(MAP_RENDER_POLL_SECONDS)


def _generate_route_poster(route_id: int, user_id: int, theme_name: str) -> None:
    route = get_route(route_id)
    if not route:
        raise PosterRenderError('Route not found')

    update_route_poster_status(route_id, {'status': 'generating', 'progress': 0, 'error': None})

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
            raise PosterRenderError('Failed to parse GPX data.') from e
    
    if not pts:
        raise PosterRenderError('No coordinates')
    
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

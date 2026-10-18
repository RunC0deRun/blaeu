"""
Resumable Intervals.icu history backfill.

A backfill walks back through a user's history in 30-day windows, newest first. Each
window takes one activity-list request and one wellness request; only activities that
are new and have GPS need an extra request for their track. Existing activities are
enriched from the list entries, which carry the full summary.

The job advances at most one window per worker tick (every 60 seconds), and its cursor is
stored after each window, so a restart resumes where it stopped. Every step is idempotent:
re-running a window after a crash only refreshes what is already there.
"""
import logging
import math
from datetime import date, datetime, timedelta, timezone

import requests

from db import (
    get_intervals_connection, get_activity_by_source, create_sync_job, get_sync_job,
    get_latest_sync_job, get_runnable_sync_jobs, claim_sync_job, update_sync_job,
)
from blueprints import intervals as icu

logger = logging.getLogger('blaeu.intervals.backfill')

SOURCE = 'intervals'
KIND = 'backfill'
WINDOW_DAYS = 30
DEFAULT_MONTHS = 12
MAX_MONTHS = 24
LEASE_MINUTES = 30
MAX_ATTEMPTS = 8
RETRY_BASE_MINUTES = 5
RETRY_MAX_MINUTES = 360

COUNTERS = ('activities_new', 'activities_enriched', 'activities_skipped', 'duplicates_merged', 'wellness_days')


def _now():
    return datetime.now(timezone.utc)


def _fmt(dt):
    return dt.strftime('%Y-%m-%d %H:%M:%S')


def window_count(range_start, range_end):
    days = (range_end - range_start).days + 1
    return max(1, math.ceil(days / WINDOW_DAYS))


def start_backfill(user_id, months=DEFAULT_MONTHS, today=None):
    """
    Starts a backfill, or returns the one already in progress. A paused job (after
    repeated failures) is resumed from where it stopped instead of starting over.
    """
    latest = get_latest_sync_job(user_id, SOURCE, KIND)
    if latest and latest['status'] in ('pending', 'running'):
        return latest
    if latest and latest['status'] == 'paused':
        update_sync_job(latest['id'], status='pending', attempts=0, next_attempt_at=None, last_error=None)
        return get_sync_job(latest['id'])

    months = max(1, min(int(months), MAX_MONTHS))
    range_end = today or _now().date()
    range_start = range_end - timedelta(days=round(months * 365 / 12))
    job_id = create_sync_job(
        user_id, SOURCE, KIND, range_start.isoformat(), range_end.isoformat(),
        window_count(range_start, range_end)
    )
    logger.info(f"Created Intervals.icu backfill job {job_id} for user {user_id} ({range_start} to {range_end})")
    return get_sync_job(job_id)


def cancel_backfill(user_id):
    latest = get_latest_sync_job(user_id, SOURCE, KIND)
    if latest and latest['status'] in ('pending', 'running', 'paused'):
        update_sync_job(latest['id'], status='cancelled', locked_until=None)
        return get_sync_job(latest['id'])
    return latest


def job_progress(job):
    if not job:
        return None
    total = job['windows_total'] or 1
    return {
        'id': job['id'],
        'status': job['status'],
        'range_start': job['range_start'],
        'range_end': job['range_end'],
        'processed_until': job['cursor'],
        'percent': 100 if job['status'] == 'completed' else round(100 * job['windows_done'] / total),
        'windows_done': job['windows_done'],
        'windows_total': job['windows_total'],
        'last_error': job['last_error'],
        'next_attempt_at': job['next_attempt_at'],
        'completed_at': job['completed_at'],
        **{k: job[k] for k in COUNTERS},
    }


def process_window(user_id, athlete_id, api_key, oldest, newest, first_window=False):
    """Imports and enriches one window of history. Returns counter increments."""
    counts = dict.fromkeys(COUNTERS, 0)

    if first_window:
        try:
            icu.sync_intervals_thresholds(user_id, athlete_id, api_key)
            icu.polite_pause()
        except (requests.exceptions.RequestException, ValueError) as e:
            logger.warning(f"Backfill: could not fetch thresholds for user {user_id}: {e}")

    # Ask for one extra day on both sides in case the API's date filter doesn't match the
    # local start date exactly; below, each activity is only handled by the window that
    # contains its local start date, so nothing is processed twice or missed
    activities = icu.intervals_get(
        f"/athlete/{athlete_id}/activities", api_key,
        params={'oldest': (oldest - timedelta(days=1)).isoformat(), 'newest': (newest + timedelta(days=1)).isoformat()}
    )
    if not isinstance(activities, list):
        raise icu.IntervalsTransientError("Unexpected activity list response from Intervals.icu")
    activities.sort(key=lambda a: a.get('start_date') or a.get('start_date_local') or '')

    for act in activities:
        act_id = act.get('id')
        if act_id is None:
            continue
        act_id = str(act_id)
        local_day = (act.get('start_date_local') or act.get('start_date') or '')[:10]
        if not oldest.isoformat() <= local_day <= newest.isoformat():
            continue  # belongs to a neighbouring window
        if icu.is_strava_stub(act):
            counts['activities_skipped'] += 1
            continue
        existing = get_activity_by_source(user_id, SOURCE, act_id)
        if existing and (existing['hidden'] or existing['merged_into']):
            continue  # deleted by the user, or shown through another source
        if existing is None:
            icu.polite_pause()  # a new activity may need a streams request
        try:
            result = icu.import_single_intervals_activity(
                user_id, athlete_id, api_key, act_id, act.get('name'),
                act.get('start_date') or act.get('start_date_local'), summary=act
            )
        except ValueError as e:
            # A permanent problem with this one activity; transient errors propagate so the
            # whole window is retried later
            logger.warning(f"Backfill: skipping Intervals.icu activity {act_id} for user {user_id}: {e}")
            counts['activities_skipped'] += 1
            continue
        counts['activities_new' if result['created'] else 'activities_enriched'] += 1
        counts['duplicates_merged'] += result['merged']

    icu.polite_pause()
    counts['wellness_days'] = icu.sync_intervals_wellness(
        user_id, athlete_id, api_key, oldest.isoformat(), newest.isoformat()
    )
    return counts


def _retry_later(job, message):
    attempts = job['attempts'] + 1
    if attempts >= MAX_ATTEMPTS:
        logger.error(f"Backfill job {job['id']} paused after {attempts} failed attempts: {message}")
        update_sync_job(job['id'], status='paused', attempts=attempts, last_error=message, locked_until=None)
        return
    delay = min(RETRY_BASE_MINUTES * (2 ** (attempts - 1)), RETRY_MAX_MINUTES)
    logger.warning(f"Backfill job {job['id']} will retry in {delay} min (attempt {attempts}): {message}")
    update_sync_job(
        job['id'], attempts=attempts, last_error=message, locked_until=None,
        next_attempt_at=_fmt(_now() + timedelta(minutes=delay))
    )


def run_backfill_step(job_id):
    """Processes the next window of a (claimed) job and records the outcome."""
    job = get_sync_job(job_id)
    if not job or job['status'] not in ('pending', 'running'):
        return

    connection = get_intervals_connection(job['user_id'])
    if not connection:
        update_sync_job(job_id, status='paused', last_error='Intervals.icu is not connected.', locked_until=None)
        return

    range_start = date.fromisoformat(job['range_start'])
    newest = date.fromisoformat(job['cursor'])
    if newest < range_start:
        update_sync_job(job_id, status='completed', completed_at=_fmt(_now()), locked_until=None)
        return
    oldest = max(range_start, newest - timedelta(days=WINDOW_DAYS - 1))

    try:
        counts = process_window(
            job['user_id'], connection['athlete_id'], connection['api_key'], oldest, newest,
            first_window=job['windows_done'] == 0
        )
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else None
        if status in (401, 403):
            update_sync_job(
                job_id, status='paused', locked_until=None,
                last_error='Intervals.icu refused access. Please reconnect Intervals.icu and resume.'
            )
        else:
            _retry_later(job, f"Intervals.icu returned HTTP {status}")
        return
    except (icu.IntervalsTransientError, requests.exceptions.RequestException) as e:
        _retry_later(job, f"Temporary error: {e}")
        return
    except Exception as e:
        # Unexpected: stop rather than move past data that wasn't processed
        logger.exception(f"Backfill job {job_id} failed at window {oldest} to {newest}")
        update_sync_job(job_id, status='failed', last_error=f"Unexpected error: {e}", locked_until=None)
        return

    # Re-read so counters added here are never based on stale values
    job = get_sync_job(job_id)
    if job['status'] == 'cancelled':
        return
    next_cursor = oldest - timedelta(days=1)
    done = next_cursor < range_start
    update_sync_job(
        job_id,
        cursor=next_cursor.isoformat(),
        windows_done=job['windows_done'] + 1,
        attempts=0, last_error=None, next_attempt_at=None, locked_until=None,
        status='completed' if done else 'running',
        completed_at=_fmt(_now()) if done else None,
        **{k: job[k] + counts[k] for k in COUNTERS},
    )
    logger.info(f"Backfill job {job_id}: processed {oldest} to {newest} {counts}")


def run_pending_backfills():
    """Called from the background worker: advances each runnable job by one window."""
    now = _now()
    for job in get_runnable_sync_jobs(_fmt(now)):
        if job['source'] != SOURCE or job['kind'] != KIND:
            continue
        if claim_sync_job(job['id'], _fmt(now), _fmt(now + timedelta(minutes=LEASE_MINUTES))):
            run_backfill_step(job['id'])

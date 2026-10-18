import json
from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
import requests

import db
from blueprints import intervals
from blueprints import intervals_backfill as backfill
from tests.test_activities import SAMPLE_GPX, STREAMS, _add_route

TODAY = date(2026, 10, 18)


def _resp(payload, status=200):
    resp = MagicMock()
    resp.status_code = status
    resp.headers = {}
    resp.json.return_value = payload
    if status >= 400:
        err = requests.exceptions.HTTPError(f"{status} error")
        err.response = resp
        resp.raise_for_status.side_effect = err
    return resp


def _activity(act_id, day, gps=True, type_='Run', start_hour=7):
    start = datetime(day.year, day.month, day.day, start_hour, 0, 0, tzinfo=timezone.utc)
    return {
        'id': act_id, 'name': f'Workout {act_id}', 'type': type_,
        'start_date': start.strftime('%Y-%m-%dT%H:%M:%SZ'),
        'start_date_local': start.strftime('%Y-%m-%dT%H:%M:%S'),
        'elapsed_time': 10, 'moving_time': 10, 'average_heartrate': 140, 'icu_training_load': 50,
        'stream_types': ['time', 'heartrate'] + (['latlng'] if gps else []),
    }


class FakeHistory:
    """An Intervals.icu API with dated history; list and wellness honour oldest/newest."""

    def __init__(self, activities, wellness_days=()):
        self.activities = activities
        self.wellness = [{'id': d.isoformat(), 'ctl': 40.0, 'atl': 45.0, 'restingHR': 45} for d in wellness_days]
        self.calls = []
        self.fail_with = None

    def __call__(self, url, params=None, **kwargs):
        self.calls.append((url, dict(params or {})))
        if self.fail_with is not None:
            if isinstance(self.fail_with, Exception):
                raise self.fail_with
            return _resp({}, self.fail_with)
        params = params or {}
        oldest, newest = params.get('oldest', '0000'), params.get('newest', '9999')
        if url.endswith('/activities'):
            return _resp([a for a in self.activities if oldest <= a['start_date'][:10] <= newest])
        if url.endswith('/wellness'):
            return _resp([w for w in self.wellness if oldest <= w['id'] <= newest])
        if url.endswith('/streams.json'):
            return _resp(STREAMS)
        if '/activity/' in url:
            return _resp({}, 404)
        return _resp({'id': 'a1', 'sportSettings': [{'types': ['Run'], 'lthr': 170}]})

    def list_calls(self):
        return [p for url, p in self.calls if url.endswith('/activities')]


@pytest.fixture
def user_id(authed_client):
    uid = db.get_user_by_username('test_user')['id']
    db.save_intervals_connection(uid, 'a1', 'key')
    return uid


@pytest.fixture(autouse=True)
def no_pause(monkeypatch):
    monkeypatch.setattr(intervals, 'REQUEST_DELAY_SECONDS', 0)
    monkeypatch.setattr(intervals.time, 'sleep', lambda s: None)


def _run_to_completion(job_id, fake, max_steps=50):
    with patch('requests.get', side_effect=fake):
        for _ in range(max_steps):
            if db.get_sync_job(job_id)['status'] != 'running' and db.get_sync_job(job_id)['status'] != 'pending':
                break
            backfill.run_backfill_step(job_id)
    return db.get_sync_job(job_id)


# --- Job lifecycle ---

def test_start_backfill_creates_twelve_month_job(user_id):
    job = backfill.start_backfill(user_id, today=TODAY)
    assert job['status'] == 'pending'
    assert job['range_end'] == '2026-10-18'
    assert job['range_start'] == '2025-10-18'
    assert job['cursor'] == '2026-10-18'
    assert job['windows_total'] == 13


def test_start_backfill_returns_active_job_instead_of_duplicate(user_id):
    first = backfill.start_backfill(user_id, today=TODAY)
    second = backfill.start_backfill(user_id, today=TODAY)
    assert first['id'] == second['id']


def test_backfill_imports_history_window_by_window(user_id):
    activities = [
        _activity('i1', TODAY - timedelta(days=2)),
        _activity('i2', TODAY - timedelta(days=45), gps=False, type_='WeightTraining'),
        _activity('i3', TODAY - timedelta(days=200)),
        _activity('i4', TODAY - timedelta(days=364)),
        _activity('i5', TODAY - timedelta(days=400)),  # outside the 12 months
    ]
    fake = FakeHistory(activities, wellness_days=[TODAY - timedelta(days=d) for d in (1, 100, 300)])
    job = backfill.start_backfill(user_id, today=TODAY)
    job = _run_to_completion(job['id'], fake)

    assert job['status'] == 'completed'
    assert job['windows_done'] == job['windows_total'] == 13
    assert job['activities_new'] == 4
    assert job['activities_enriched'] == 0
    assert job['wellness_days'] == 3
    ids = {a['source_id'] for a in db.get_activities(user_id)}
    assert ids == {'i1', 'i2', 'i3', 'i4'}
    assert len(db.get_routes(user_id)) == 3  # i2 has no GPS
    # One list request per window, newest first
    lists = fake.list_calls()
    assert len(lists) == 13
    # Requests reach one day beyond each window on both sides
    assert lists[0]['newest'] == '2026-10-19'
    assert lists[-1]['oldest'] == '2025-10-17'
    assert db.get_athlete_thresholds(user_id)[0]['lthr'] == 170


def test_backfill_resumes_after_restart_without_redoing_windows(user_id):
    fake = FakeHistory([_activity('i1', TODAY - timedelta(days=100))])
    job = backfill.start_backfill(user_id, today=TODAY)
    with patch('requests.get', side_effect=fake):
        backfill.run_backfill_step(job['id'])
        backfill.run_backfill_step(job['id'])
    stopped = db.get_sync_job(job['id'])
    assert stopped['windows_done'] == 2
    assert stopped['cursor'] == (TODAY - timedelta(days=60)).isoformat()

    # "Restart": a fresh run continues from the stored cursor
    job = _run_to_completion(job['id'], fake)
    assert job['status'] == 'completed'
    assert len(fake.list_calls()) == 13
    assert {a['source_id'] for a in db.get_activities(user_id)} == {'i1'}


def test_backfill_enriches_existing_activities_without_new_routes(user_id):
    route_id = _add_route(user_id, 'intervals_i9_abcd.gpx')  # imported earlier, no summary yet
    day = date(2026, 6, 1)  # the sample GPX starts 2026-06-01 08:00:00
    act = _activity('i9', day, start_hour=8)
    fake = FakeHistory([act])
    job = backfill.start_backfill(user_id, today=TODAY)
    job = _run_to_completion(job['id'], fake)

    assert job['activities_enriched'] == 1 and job['activities_new'] == 0
    activity = db.get_activity_by_source(user_id, 'intervals', 'i9')
    assert activity['route_id'] == route_id
    assert activity['sport_type'] == 'Run'
    assert activity['avg_hr'] == 140
    assert json.loads(activity['raw_summary'])['id'] == 'i9'
    assert len(db.get_routes(user_id)) == 1
    assert not any(url.endswith('/streams.json') for url, _ in fake.calls)


def test_backfill_merges_same_workout_from_another_source(user_id):
    upload_route = _add_route(user_id, 'upload.gpx', file_hash='h-upload')  # starts 2026-06-01 08:00:00
    fake = FakeHistory([_activity('i7', date(2026, 6, 1), start_hour=8)])
    job = backfill.start_backfill(user_id, today=TODAY)
    job = _run_to_completion(job['id'], fake)

    assert job['duplicates_merged'] == 1
    visible = db.get_activities(user_id)
    assert len(visible) == 1
    assert visible[0]['source'] == 'intervals'
    assert visible[0]['route_id'] == upload_route  # reuses the existing map
    assert len(db.get_routes(user_id)) == 1
    upload = db.get_activity_by_source(user_id, 'upload', 'h-upload')
    assert upload['merged_into'] == visible[0]['id']


def test_backfill_does_not_merge_different_workouts(user_id):
    _add_route(user_id, 'upload.gpx', file_hash='h-upload')  # 2026-06-01 08:00:00
    fake = FakeHistory([_activity('i7', date(2026, 6, 1), start_hour=18)])
    job = _run_to_completion(backfill.start_backfill(user_id, today=TODAY)['id'], fake)
    assert job['duplicates_merged'] == 0
    assert len(db.get_activities(user_id)) == 2


def test_backfill_skips_deleted_activities(user_id):
    hidden_id = db.upsert_activity(user_id, 'intervals', 'i1', {'start_time': '2026-10-16 07:00:00'})
    db.set_activity_hidden(hidden_id, True)
    fake = FakeHistory([_activity('i1', TODAY - timedelta(days=2))])
    _run_to_completion(backfill.start_backfill(user_id, today=TODAY)['id'], fake)
    assert db.get_activity_by_source(user_id, 'intervals', 'i1')['hidden'] == 1
    assert db.get_activities(user_id) == []


def test_backfill_skips_strava_stubs(user_id):
    stub = {'id': 's1', 'source': 'STRAVA', '_note': 'n/a', 'start_date': '2026-10-10T07:00:00Z'}
    job = _run_to_completion(backfill.start_backfill(user_id, today=TODAY)['id'], FakeHistory([stub]))
    assert job['status'] == 'completed'
    assert job['activities_skipped'] == 1


# --- Failures ---

def test_transient_error_retries_later_without_advancing(user_id):
    fake = FakeHistory([])
    fake.fail_with = requests.exceptions.ConnectionError('network down')
    job = backfill.start_backfill(user_id, today=TODAY)
    with patch('requests.get', side_effect=fake):
        backfill.run_backfill_step(job['id'])
    job = db.get_sync_job(job['id'])
    assert job['cursor'] == TODAY.isoformat()
    assert job['windows_done'] == 0
    assert job['attempts'] == 1
    assert job['next_attempt_at'] is not None
    assert 'network down' in job['last_error']
    # Not runnable until the retry time
    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    assert job['id'] not in [j['id'] for j in db.get_runnable_sync_jobs(now)]


def test_repeated_failures_pause_and_start_resumes(user_id):
    fake = FakeHistory([_activity('i1', TODAY - timedelta(days=2))])
    fake.fail_with = 503
    job = backfill.start_backfill(user_id, today=TODAY)
    with patch('requests.get', side_effect=fake):
        for _ in range(backfill.MAX_ATTEMPTS):
            backfill.run_backfill_step(job['id'])
    assert db.get_sync_job(job['id'])['status'] == 'paused'

    resumed = backfill.start_backfill(user_id, today=TODAY)
    assert resumed['id'] == job['id']
    assert resumed['status'] == 'pending'
    assert resumed['attempts'] == 0
    fake.fail_with = None
    assert _run_to_completion(job['id'], fake)['status'] == 'completed'


def test_revoked_access_pauses_with_reconnect_message(user_id):
    fake = FakeHistory([])
    fake.fail_with = 401
    job = backfill.start_backfill(user_id, today=TODAY)
    with patch('requests.get', side_effect=fake):
        backfill.run_backfill_step(job['id'])
    job = db.get_sync_job(job['id'])
    assert job['status'] == 'paused'
    assert 'reconnect' in job['last_error']


def test_unexpected_error_fails_job_instead_of_skipping(user_id, monkeypatch):
    fake = FakeHistory([_activity('i1', TODAY - timedelta(days=2))])
    monkeypatch.setattr(intervals, 'upsert_activity', MagicMock(side_effect=RuntimeError('bug')))
    job = backfill.start_backfill(user_id, today=TODAY)
    with patch('requests.get', side_effect=fake):
        backfill.run_backfill_step(job['id'])
    job = db.get_sync_job(job['id'])
    assert job['status'] == 'failed'
    assert job['cursor'] == TODAY.isoformat()


def test_disconnected_user_pauses_job(user_id):
    job = backfill.start_backfill(user_id, today=TODAY)
    db.delete_intervals_connection(user_id)
    backfill.run_backfill_step(job['id'])
    assert db.get_sync_job(job['id'])['status'] == 'paused'


def test_cancelled_job_does_not_run(user_id):
    fake = FakeHistory([_activity('i1', TODAY - timedelta(days=2))])
    job = backfill.start_backfill(user_id, today=TODAY)
    backfill.cancel_backfill(user_id)
    with patch('requests.get', side_effect=fake):
        backfill.run_backfill_step(job['id'])
    assert fake.calls == []
    assert db.get_sync_job(job['id'])['status'] == 'cancelled'


# --- Scheduling ---

def test_claim_is_exclusive(user_id):
    job = backfill.start_backfill(user_id, today=TODAY)
    now = '2026-10-18 12:00:00'
    assert db.claim_sync_job(job['id'], now, '2026-10-18 12:30:00')
    assert not db.claim_sync_job(job['id'], now, '2026-10-18 12:30:00')
    # An expired lease (crashed worker) can be taken over
    assert db.claim_sync_job(job['id'], '2026-10-18 12:31:00', '2026-10-18 13:01:00')


def test_run_pending_backfills_advances_one_window_per_tick(user_id):
    fake = FakeHistory([])
    job = backfill.start_backfill(user_id, today=TODAY)
    with patch('requests.get', side_effect=fake):
        backfill.run_pending_backfills()
    job = db.get_sync_job(job['id'])
    assert job['windows_done'] == 1
    assert job['locked_until'] is None


# --- API ---

def _connect(client):
    with patch('requests.get', return_value=_resp({'id': 'a1'})):
        return client.post('/api/intervals/connect', json={'athlete_id': 'a1', 'api_key': 'key'})


def test_connect_schedules_backfill(authed_client):
    assert _connect(authed_client).status_code == 200
    job = authed_client.get('/api/intervals/backfill').get_json()['job']
    assert job['status'] == 'pending'
    assert job['windows_total'] == 13


def test_backfill_api(authed_client):
    assert authed_client.get('/api/intervals/backfill').get_json()['job'] is None
    assert authed_client.post('/api/intervals/backfill', json={}).status_code == 400  # not connected
    _connect(authed_client)
    authed_client.post('/api/intervals/backfill/cancel')
    assert authed_client.post('/api/intervals/backfill', json={'months': 0}).status_code == 400
    assert authed_client.post('/api/intervals/backfill', json={'months': '12'}).status_code == 400
    resp = authed_client.post('/api/intervals/backfill', json={'months': 6})
    assert resp.status_code == 200
    job = resp.get_json()['job']
    assert job['status'] == 'pending'
    assert job['percent'] == 0
    cancelled = authed_client.post('/api/intervals/backfill/cancel').get_json()['job']
    assert cancelled['status'] == 'cancelled'


def test_backfill_api_requires_login(client):
    assert client.get('/api/intervals/backfill').status_code == 401
    assert client.post('/api/intervals/backfill', json={}).status_code == 401


def test_overlap_day_counts_activity_once(user_id):
    # Exactly on a window boundary, so it is listed by two windows
    boundary = TODAY - timedelta(days=backfill.WINDOW_DAYS)
    fake = FakeHistory([_activity('i1', boundary)])
    job = _run_to_completion(backfill.start_backfill(user_id, today=TODAY)['id'], fake)
    assert job['activities_new'] == 1
    assert job['activities_enriched'] == 0
    assert len([url for url, _ in fake.calls if url.endswith('/streams.json')]) == 1

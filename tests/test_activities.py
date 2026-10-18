import json
import pytest
import requests
from unittest.mock import patch, MagicMock

import db
from blueprints import intervals


SAMPLE_GPX = b"""<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" creator="test" xmlns="http://www.topografix.com/GPX/1/1">
  <trk><name>Morning Ride</name><trkseg>
    <trkpt lat="52.5200" lon="13.4050"><ele>34</ele><time>2026-06-01T08:00:00Z</time></trkpt>
    <trkpt lat="52.5210" lon="13.4060"><ele>36</ele><time>2026-06-01T08:00:10Z</time></trkpt>
  </trkseg></trk>
</gpx>"""

RICH_SUMMARY = {
    'id': 'i100',
    'name': 'Tempo Run',
    'type': 'Run',
    'start_date': '2026-06-02T06:30:00Z',
    'start_date_local': '2026-06-02T08:30:00',
    'timezone': 'Europe/Amsterdam',
    'elapsed_time': 3700,
    'moving_time': 3600,
    'distance': 12000.0,
    'total_elevation_gain': 85.0,
    'average_heartrate': 152,
    'max_heartrate': 176,
    'average_cadence': 86,
    'calories': 820,
    'icu_training_load': 74,
    'icu_intensity': 88.5,
    'trimp': 110.2,
    'icu_rpe': 6,
    'feel': 2,
    'icu_hr_zone_times': [300, 900, 1800, 500, 100],
    'icu_zone_times': [{'id': 'Z1', 'secs': 100}, {'id': 'Z2', 'secs': 200}],
    'trainer': False,
    'stream_types': ['time', 'heartrate', 'latlng', 'altitude'],
}

INDOOR_SUMMARY = {
    'id': 'i200',
    'name': 'Zwift Workout',
    'type': 'VirtualRide',
    'start_date': '2026-06-03T18:00:00Z',
    'elapsed_time': 3600,
    'moving_time': 3600,
    'icu_average_watts': 210,
    'icu_weighted_avg_watts': 228,
    'icu_training_load': 68,
    'trainer': True,
    'stream_types': ['time', 'watts', 'heartrate', 'cadence'],
}

STREAMS = [
    {'type': 'time', 'data': [0, 10]},
    {'type': 'latlng', 'data': [52.5200, 52.5210], 'data2': [13.4050, 13.4060]},
    {'type': 'altitude', 'data': [34.0, 36.0]},
]


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


class FakeIntervals:
    """Routes mocked requests.get calls by URL and records them."""

    def __init__(self, activities=None, summaries=None, streams=None, profile=None, wellness=None):
        self.activities = activities or []
        self.summaries = summaries or {}
        self.streams = streams or {}
        self.profile = profile or {'id': 'a1', 'sportSettings': []}
        self.wellness = wellness or []
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append(url)
        if url.endswith('/streams.json'):
            act_id = url.split('/activity/')[1].split('/')[0]
            return _resp(self.streams.get(act_id, []))
        if '/activity/' in url:
            act_id = url.rsplit('/', 1)[1]
            return _resp(self.summaries[act_id]) if act_id in self.summaries else _resp({}, 404)
        if url.endswith('/activities'):
            return _resp(self.activities)
        if url.endswith('/wellness'):
            return _resp(self.wellness)
        return _resp(self.profile)


@pytest.fixture
def user_id(authed_client):
    return db.get_user_by_username('test_user')['id']


@pytest.fixture(autouse=True)
def no_pause(monkeypatch):
    monkeypatch.setattr(intervals, 'REQUEST_DELAY_SECONDS', 0)
    monkeypatch.setattr(intervals.time, 'sleep', lambda s: None)


def _add_route(user_id, filename, file_hash='hash1'):
    from gpx_parser import parse_gpx
    parsed = parse_gpx(SAMPLE_GPX)
    return db.add_route({
        'name': 'Morning Ride', 'filename': filename, 'file_hash': file_hash,
        'file_path': f'/nonexistent/{filename}', 'user_id': user_id,
        'created_at': parsed['start_time'], 'timezone': 'UTC',
    }, parsed['statistics'])


# --- Schema and route-linked activities ---

@pytest.mark.parametrize('filename,source,source_id', [
    ('garmin_123456_abcd.gpx', 'garmin', '123456'),
    ('garmin_123456.gpx', 'garmin', '123456'),
    ('intervals_i789_abcd.gpx', 'intervals', 'i789'),
    ('deadbeef.gpx', 'upload', 'hash1'),
])
def test_route_activity_identity(filename, source, source_id):
    assert db.route_activity_identity(filename, 'hash1') == (source, source_id)


def test_add_route_creates_linked_activity(user_id):
    route_id = _add_route(user_id, 'garmin_42_abcd.gpx')
    activity = db.get_activity_by_source(user_id, 'garmin', '42')
    assert activity['route_id'] == route_id
    assert activity['has_gps'] == 1
    assert activity['start_time'] == '2026-06-01 08:00:00'
    assert activity['raw_summary'] is None  # not enriched yet


def test_init_db_migrates_existing_routes(user_id):
    route_id = _add_route(user_id, 'upload.gpx')
    conn = db.get_db()
    conn.execute("DELETE FROM activities")
    conn.commit()
    conn.close()

    db.init_db()
    db.init_db()  # idempotent

    activities = db.get_activities(user_id)
    assert len(activities) == 1
    assert activities[0]['route_id'] == route_id
    assert activities[0]['source'] == 'upload'


def test_upsert_activity_never_erases_known_values(user_id):
    db.upsert_activity(user_id, 'intervals', 'x1', {'avg_hr': 150, 'hr_zone_times': [1, 2, 3]})
    db.upsert_activity(user_id, 'intervals', 'x1', {'avg_hr': None, 'training_load': 50})
    activity = db.get_activity_by_source(user_id, 'intervals', 'x1')
    assert activity['avg_hr'] == 150
    assert activity['training_load'] == 50
    assert json.loads(activity['hr_zone_times']) == [1, 2, 3]
    assert len(db.get_activities(user_id)) == 1


def test_delete_route_removes_upload_but_tombstones_synced(user_id):
    upload_route = _add_route(user_id, 'upload.gpx', file_hash='h-upload')
    synced_route = _add_route(user_id, 'intervals_i5_abcd.gpx', file_hash='h-synced')

    db.delete_route(upload_route)
    db.delete_route(synced_route)

    assert db.get_activity_by_source(user_id, 'upload', 'h-upload') is None
    tombstone = db.get_activity_by_source(user_id, 'intervals', 'i5')
    assert tombstone['hidden'] == 1
    assert tombstone['route_id'] is None
    assert db.get_activities(user_id) == []
    assert 'i5' in db.get_activity_source_ids(user_id, 'intervals')


# --- Intervals.icu normalisation ---

def test_normalize_intervals_activity():
    fields = intervals.normalize_intervals_activity(RICH_SUMMARY)
    assert fields['sport_type'] == 'Run'
    assert fields['start_time'] == '2026-06-02 06:30:00'
    assert fields['start_time_local'] == '2026-06-02 08:30:00'
    assert fields['avg_hr'] == 152
    assert fields['training_load'] == 74
    assert fields['intensity'] == pytest.approx(0.885)
    assert fields['rpe'] == 6
    assert fields['power_zone_times'] == [100, 200]
    assert fields['is_indoor'] is False
    assert fields['raw_summary'] is RICH_SUMMARY


def test_strava_stub_detection():
    assert intervals.is_strava_stub({'id': '1', 'source': 'STRAVA', '_note': 'not available'})
    assert not intervals.is_strava_stub(RICH_SUMMARY)


# --- Intervals.icu import ---

def test_import_gps_activity_creates_route_and_rich_activity(user_id):
    fake = FakeIntervals(summaries={'i100': RICH_SUMMARY}, streams={'i100': STREAMS})
    with patch('requests.get', side_effect=fake):
        result = intervals.import_single_intervals_activity(user_id, 'a1', 'key', 'i100')

    assert result['route_id'] is not None
    activities = db.get_activities(user_id)
    assert len(activities) == 1
    activity = activities[0]
    assert activity['route_id'] == result['route_id']
    assert activity['has_gps'] == 1
    assert activity['sport_type'] == 'Run'
    assert activity['avg_hr'] == 152
    assert activity['enriched_at'] is not None
    assert json.loads(activity['raw_summary'])['id'] == 'i100'


def test_import_indoor_activity_without_gps(user_id):
    fake = FakeIntervals(summaries={'i200': INDOOR_SUMMARY})
    with patch('requests.get', side_effect=fake):
        result = intervals.import_single_intervals_activity(user_id, 'a1', 'key', 'i200')

    assert result['route_id'] is None
    assert not any('streams' in url for url in fake.calls)  # stream_types said: no GPS
    activity = db.get_activity_by_source(user_id, 'intervals', 'i200')
    assert activity['has_gps'] == 0
    assert activity['is_indoor'] == 1
    assert activity['normalized_power'] == 228
    assert db.get_routes(user_id) == []


def test_import_without_stream_types_checks_streams(user_id):
    summary = {k: v for k, v in INDOOR_SUMMARY.items() if k != 'stream_types'}
    fake = FakeIntervals(summaries={'i200': summary}, streams={'i200': [{'type': 'time', 'data': [0, 1]}]})
    with patch('requests.get', side_effect=fake):
        result = intervals.import_single_intervals_activity(user_id, 'a1', 'key', 'i200')
    assert result['route_id'] is None
    assert any('streams' in url for url in fake.calls)


def test_reimport_is_idempotent(user_id):
    fake = FakeIntervals(summaries={'i100': RICH_SUMMARY}, streams={'i100': STREAMS})
    with patch('requests.get', side_effect=fake):
        first = intervals.import_single_intervals_activity(user_id, 'a1', 'key', 'i100')
        second = intervals.import_single_intervals_activity(user_id, 'a1', 'key', 'i100')

    assert first['route_id'] == second['route_id']
    assert len(db.get_routes(user_id)) == 1
    assert len(db.get_activities(user_id)) == 1


def test_explicit_reimport_unhides_deleted_activity(user_id):
    fake = FakeIntervals(summaries={'i100': RICH_SUMMARY}, streams={'i100': STREAMS})
    with patch('requests.get', side_effect=fake):
        first = intervals.import_single_intervals_activity(user_id, 'a1', 'key', 'i100')
        db.delete_route(first['route_id'])
        assert db.get_activities(user_id) == []
        second = intervals.import_single_intervals_activity(user_id, 'a1', 'key', 'i100')

    assert second['route_id'] is not None
    assert len(db.get_activities(user_id)) == 1


def test_import_strava_stub_raises(user_id):
    fake = FakeIntervals(summaries={'s1': {'id': 's1', 'source': 'STRAVA', '_note': 'n/a'}})
    with patch('requests.get', side_effect=fake):
        with pytest.raises(ValueError, match='Strava'):
            intervals.import_single_intervals_activity(user_id, 'a1', 'key', 's1')
    assert db.get_activities(user_id) == []


def test_import_endpoint_handles_non_gps(authed_client, user_id):
    with patch('requests.get', side_effect=FakeIntervals(profile={'id': 'a1'})):
        authed_client.post('/api/intervals/connect', json={'athlete_id': 'a1', 'api_key': 'key'})
    with patch('requests.get', side_effect=FakeIntervals(summaries={'i200': INDOOR_SUMMARY})):
        resp = authed_client.post('/api/intervals/import', json={'activityId': 'i200'})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data['route_id'] is None
    assert data['name'] == 'Zwift Workout'


def test_import_endpoint_reports_transient_errors(authed_client, user_id):
    with patch('requests.get', side_effect=FakeIntervals(profile={'id': 'a1'})):
        authed_client.post('/api/intervals/connect', json={'athlete_id': 'a1', 'api_key': 'key'})
    with patch('requests.get', return_value=_resp({}, 503)):
        resp = authed_client.post('/api/intervals/import', json={'activityId': 'i200'})
    assert resp.status_code == 502


# --- HTTP politeness ---

def test_intervals_get_retries_on_429():
    responses = [_resp({}, 429), _resp({}, 429), _resp({'ok': True})]
    responses[0].headers = {'Retry-After': '2'}
    with patch('requests.get', side_effect=responses) as mock_get:
        assert intervals.intervals_get('/athlete/a1', 'key') == {'ok': True}
    assert mock_get.call_count == 3


def test_intervals_get_gives_up_after_max_retries():
    with patch('requests.get', return_value=_resp({}, 500)) as mock_get:
        with pytest.raises(requests.exceptions.HTTPError):
            intervals.intervals_get('/athlete/a1', 'key')
    assert mock_get.call_count == intervals.MAX_RETRIES + 1


# --- Background sync ---

def _connect(user_id):
    db.save_intervals_connection(user_id, 'a1', 'key')


def test_sync_imports_new_gps_and_indoor_activities(user_id):
    _connect(user_id)
    # An earlier activity makes this an incremental sync rather than a first sync
    db.upsert_activity(user_id, 'intervals', 'i001', {'start_time': '2026-06-01 00:00:00'})
    fake = FakeIntervals(
        activities=[RICH_SUMMARY, INDOOR_SUMMARY],
        streams={'i100': STREAMS},
        profile={'id': 'a1', 'icu_resting_hr': 48, 'sportSettings': [
            {'types': ['Run', 'TrailRun'], 'lthr': 168, 'max_hr': 188, 'threshold_pace': 3.9,
             'hr_zones': [140, 155, 168, 178, 188]},
        ]},
    )
    with patch('requests.get', side_effect=fake):
        intervals.sync_user_intervals_activities_in_background(user_id)

    ids = {a['source_id'] for a in db.get_activities(user_id)}
    assert {'i100', 'i200'} <= ids
    # Summaries from the list are used directly: no per-activity summary request
    assert not any(url.endswith('/activity/i100') or url.endswith('/activity/i200') for url in fake.calls)

    thresholds = db.get_athlete_thresholds(user_id)
    assert thresholds[0]['sport'] == 'Run'
    assert thresholds[0]['lthr'] == 168
    assert thresholds[0]['resting_hr'] == 48
    assert db.get_intervals_connection(user_id)['last_sync'] is not None


def test_sync_skips_hidden_activities(user_id):
    _connect(user_id)
    db.upsert_activity(user_id, 'intervals', 'i001', {'start_time': '2026-06-01 00:00:00'})
    hidden_id = db.upsert_activity(user_id, 'intervals', 'i200', {'start_time': '2026-06-03 18:00:00'})
    db.set_activity_hidden(hidden_id, True)
    fake = FakeIntervals(activities=[INDOOR_SUMMARY])
    with patch('requests.get', side_effect=fake):
        intervals.sync_user_intervals_activities_in_background(user_id)
    assert db.get_activity_by_source(user_id, 'intervals', 'i200')['hidden'] == 1


def test_sync_stops_on_transient_error_without_skipping(user_id):
    _connect(user_id)
    db.upsert_activity(user_id, 'intervals', 'i001', {'start_time': '2026-06-01 00:00:00'})
    fake = FakeIntervals(activities=[RICH_SUMMARY, INDOOR_SUMMARY])

    def flaky(url, **kwargs):
        if url.endswith('/streams.json'):
            raise requests.exceptions.ConnectionError('network down')
        return fake(url, **kwargs)

    with patch('requests.get', side_effect=flaky):
        intervals.sync_user_intervals_activities_in_background(user_id)

    # i100 failed transiently, so the later i200 must not be imported past it
    ids = {a['source_id'] for a in db.get_activities(user_id)}
    assert 'i100' not in ids
    assert 'i200' not in ids


WELLNESS = [{
    'id': '2026-06-03', 'ctl': 55.2, 'atl': 61.0, 'rampRate': 3.1,
    'restingHR': 47, 'hrv': 72, 'sleepSecs': 27000, 'weight': 71.5, 'vo2max': 56.0,
}]


def test_wellness_without_opt_in_keeps_only_training_load(user_id):
    with patch('requests.get', side_effect=FakeIntervals(wellness=WELLNESS)):
        intervals.sync_intervals_wellness(user_id, 'a1', 'key', '2026-06-01', '2026-06-03')
    row = db.get_daily_wellness(user_id)[0]
    assert row['ctl'] == 55.2
    assert row['resting_hr'] is None
    assert row['hrv'] is None
    assert row['raw'] is None


def test_wellness_with_opt_in_stores_body_data(user_id):
    db.update_user_wellness_opt_in(user_id, True)
    with patch('requests.get', side_effect=FakeIntervals(wellness=WELLNESS)):
        intervals.sync_intervals_wellness(user_id, 'a1', 'key', '2026-06-01', '2026-06-03')
    row = db.get_daily_wellness(user_id)[0]
    assert row['resting_hr'] == 47
    assert row['hrv'] == 72
    assert row['sleep_secs'] == 27000
    assert row['vo2max'] == 56.0
    assert json.loads(row['raw'])['weight'] == 71.5


def test_deleting_user_removes_training_data(authed_client, user_id):
    db.upsert_activity(user_id, 'intervals', 'i1', {'avg_hr': 140})
    db.upsert_daily_wellness(user_id, '2026-06-01', 'intervals', {'ctl': 40})
    db.upsert_athlete_thresholds(user_id, 'Run', 'intervals', {'lthr': 165})
    db.delete_user(user_id)
    conn = db.get_db()
    for table in ('activities', 'daily_wellness', 'athlete_thresholds'):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    conn.close()

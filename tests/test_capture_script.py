"""Tests for scripts/capture_intervals_fixtures.py, using a synthetic Intervals.icu API."""
import importlib.util
import json
import os
import random

import pytest

import db
from blueprints import intervals
from tests import intervals_fixture_checks as checks

_spec = importlib.util.spec_from_file_location(
    'capture_intervals_fixtures',
    os.path.join(os.path.dirname(__file__), '..', 'scripts', 'capture_intervals_fixtures.py'),
)
capture_script = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(capture_script)

ATHLETE_ID = 'i98765'

PROFILE = {
    'id': ATHLETE_ID, 'name': 'Anneke Jansen', 'firstname': 'Anneke', 'lastname': 'Jansen',
    'email': 'anneke@example.org', 'city': 'Amersfoort', 'country': 'Netherlands',
    'icu_resting_hr': 48, 'timezone': 'Europe/Amsterdam', 'icu_weight': 64.0,
    'sportSettings': [
        {'id': 1, 'athlete_id': ATHLETE_ID, 'types': ['Run', 'TrailRun'], 'lthr': 170, 'max_hr': 190,
         'threshold_pace': 3.8, 'hr_zones': [140, 155, 170, 180, 190]},
        {'id': 2, 'athlete_id': ATHLETE_ID, 'types': ['Ride', 'VirtualRide'], 'ftp': 250, 'lthr': 165},
    ],
}

ACTIVITIES = [
    {'id': 'i5550001', 'name': 'Rondje met Piet', 'description': 'Langs het huis van Piet',
     'type': 'Run', 'start_date': '2026-06-02T06:30:00Z', 'start_date_local': '2026-06-02T08:30:00',
     'timezone': 'Europe/Amsterdam', 'elapsed_time': 3700, 'moving_time': 3600, 'distance': 12000.0,
     'average_heartrate': 152, 'max_heartrate': 176, 'icu_training_load': 74, 'icu_athlete_id': ATHLETE_ID,
     'device_name': 'Garmin Forerunner 965', 'external_id': 'garmin_123456789.fit',
     'start_latlng': [52.15612, 5.38763], 'stream_types': ['time', 'heartrate', 'latlng', 'altitude']},
    {'id': 'i5550002', 'name': 'Zwift', 'type': 'VirtualRide', 'trainer': True,
     'start_date': '2026-06-03T18:00:00Z', 'elapsed_time': 3600, 'moving_time': 3600,
     'icu_average_watts': 210, 'average_heartrate': 140, 'max_heartrate': 165, 'icu_training_load': 68,
     'stream_types': ['time', 'watts', 'heartrate']},
    {'id': 'i5550003', 'name': 'Gym', 'type': 'WeightTraining',
     'start_date': '2026-06-04T17:00:00Z', 'elapsed_time': 2700, 'moving_time': 2700,
     'stream_types': ['time']},
    {'id': '7777777', 'type': 'Ride', 'start_date_local': '2026-06-05T09:00:00',
     'source': 'STRAVA', '_note': 'STRAVA activities are not available via the API'},
]

STREAMS = [
    {'type': 'time', 'data': list(range(0, 600, 10))},
    {'type': 'latlng', 'data': [52.15612 + i * 1e-4 for i in range(60)],
     'data2': [5.38763 + i * 1e-4 for i in range(60)]},
    {'type': 'altitude', 'data': [5.0] * 60},
]

WELLNESS = [
    {'id': '2026-06-03', 'ctl': 55.2, 'atl': 61.0, 'rampRate': 3.1, 'restingHR': 47, 'hrv': 72.0,
     'sleepSecs': 27000, 'weight': 64.2, 'comments': 'Slecht geslapen'},
]


def synthetic_get(path, params=None):
    if path == f'/athlete/{ATHLETE_ID}':
        return json.loads(json.dumps(PROFILE))
    if path.endswith('/activities'):
        return json.loads(json.dumps(ACTIVITIES))
    if path.endswith('/wellness'):
        return json.loads(json.dumps(WELLNESS))
    if path.endswith('/streams.json'):
        return json.loads(json.dumps(STREAMS))
    act_id = path.rsplit('/', 1)[1]
    return json.loads(json.dumps(next(a for a in ACTIVITIES if a['id'] == act_id)))


@pytest.fixture
def captured():
    return capture_script.capture(synthetic_get, ATHLETE_ID, rng=random.Random(1), log=lambda *a: None)


def test_capture_selects_every_kind(captured):
    kinds = sorted(info['kind'] for info in captured['manifest.json']['activities'].values())
    assert kinds == ['indoor', 'no_gps', 'outdoor_gps', 'strava']
    assert captured['manifest.json']['missing_kinds'] == []


def test_capture_removes_personal_data(captured):
    text = json.dumps(captured)
    for secret in ('Anneke', 'Jansen', 'anneke@example.org', 'Amersfoort', 'Netherlands', 'Piet',
                   'Forerunner', 'garmin_123456789', 'Europe/Amsterdam', '98765', 'i5550001',
                   '7777777', '52.156', '5.387', 'Slecht geslapen', 'icu_weight'):
        assert secret not in text, secret


def test_capture_keeps_training_data_and_shifts_gps(captured):
    manifest = captured['manifest.json']['activities']
    outdoor = next(fid for fid, info in manifest.items() if info['kind'] == 'outdoor_gps')
    detail = captured[f'activity_{outdoor}.json']
    assert detail['average_heartrate'] == 152
    assert detail['icu_training_load'] == 74
    assert detail['start_latlng'] == list(capture_script.FAKE_ORIGIN)

    latlng = next(s for s in captured[f'streams_{outdoor}.json'] if s['type'] == 'latlng')
    assert len(latlng['data']) == capture_script.STREAM_POINTS
    assert latlng['data'][0] == capture_script.FAKE_ORIGIN[0]
    assert latlng['data'][1] - latlng['data'][0] == pytest.approx(1e-4)  # shape kept

    assert captured['athlete.json']['id'] == 'a0'
    assert captured['athlete.json']['sportSettings'][1]['ftp'] == 250
    assert captured['athlete.json']['sportSettings'][0]['athlete_id'] == 'a0'


def test_capture_perturbs_body_wellness(captured):
    record = captured['wellness.json'][0]
    assert record['ctl'] == 55.2 and record['atl'] == 61.0  # training values kept
    assert record['weight'] != 64.2
    assert 64.2 * 0.85 <= record['weight'] <= 64.2 * 1.15
    assert record['comments'] == capture_script.REDACTED


def test_capture_refuses_to_write_leaks():
    def leaky_get(path, params=None):
        data = synthetic_get(path, params)
        if path.endswith('/activities'):
            data[0]['interval_summary'] = ['Tempo with Anneke Jansen']  # an unredacted free-text field
        return data

    with pytest.raises(capture_script.RedactionError):
        capture_script.capture(leaky_get, ATHLETE_ID, rng=random.Random(1), log=lambda *a: None)


def test_captured_fixtures_pass_the_fixture_checks(captured, tmp_path, authed_client, monkeypatch):
    monkeypatch.setattr(intervals, 'REQUEST_DELAY_SECONDS', 0)
    capture_script.write_fixtures(captured, str(tmp_path))
    user_id = db.get_user_by_username('test_user')['id']

    for fake_id in checks.manifest(str(tmp_path))['activities']:
        checks.check_activity(str(tmp_path), fake_id, user_id)
    checks.check_wellness(str(tmp_path))
    checks.check_athlete(str(tmp_path), user_id)


def test_main_requires_api_key_in_environment(monkeypatch):
    monkeypatch.delenv('INTERVALS_API_KEY', raising=False)
    with pytest.raises(SystemExit):
        capture_script.main(['--athlete-id', ATHLETE_ID])


def test_getter_backs_off_on_429(monkeypatch):
    from unittest.mock import MagicMock
    throttled = MagicMock(status_code=429, headers={'Retry-After': '3'})
    ok = MagicMock(status_code=200, headers={})
    ok.json.return_value = {'ok': True}
    monkeypatch.setattr(capture_script.requests.Session, 'get', MagicMock(side_effect=[throttled, ok]))
    waits = []
    get = capture_script.make_getter('key', delay=1.0, sleep=waits.append)
    assert get('/athlete/a0') == {'ok': True}
    assert waits == [3.0, 1.0]  # Retry-After honoured, then the normal pause before the retry

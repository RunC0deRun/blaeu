"""
Checks run against recorded (redacted) Intervals.icu responses, as written by
scripts/capture_intervals_fixtures.py. Shared by test_intervals_fixtures.py and the
script's own tests.
"""
import json
import os
from unittest.mock import MagicMock, patch

import pytest

import db
from blueprints import intervals

# Raw wellness keys and the column they must end up in
WELLNESS_MAPPING = {
    'restingHR': 'resting_hr', 'hrv': 'hrv', 'hrvSDNN': 'hrv_sdnn', 'sleepSecs': 'sleep_secs',
    'sleepScore': 'sleep_score', 'sleepQuality': 'sleep_quality', 'weight': 'weight',
    'bodyFat': 'body_fat', 'readiness': 'readiness', 'stress': 'stress_avg', 'spO2': 'spo2',
    'respiration': 'respiration', 'steps': 'steps', 'vo2max': 'vo2max', 'ctl': 'ctl', 'atl': 'atl',
    'rampRate': 'ramp_rate', 'soreness': 'soreness', 'fatigue': 'fatigue', 'mood': 'mood',
    'motivation': 'motivation',
}

# Fields the coach relies on: if the full activity has them, the list entry used by the
# background sync must have them too, or the sync has to fetch each activity separately.
SYNC_CRITICAL_FIELDS = ('sport_type', 'start_time', 'moving_time', 'avg_hr', 'training_load', 'avg_power')


def load(fixture_dir, name):
    with open(os.path.join(fixture_dir, name), encoding='utf-8') as f:
        return json.load(f)


def manifest(fixture_dir):
    return load(fixture_dir, 'manifest.json')


def _resp(payload):
    resp = MagicMock()
    resp.status_code = 200
    resp.headers = {}
    resp.json.return_value = payload
    return resp


def fake_api(fixture_dir):
    """A requests.get replacement that serves the recorded fixtures."""
    def get(url, **kwargs):
        if url.endswith('/streams.json'):
            fake_id = url.split('/activity/')[1].split('/')[0]
            path = os.path.join(fixture_dir, f'streams_{fake_id}.json')
            return _resp(load(fixture_dir, f'streams_{fake_id}.json') if os.path.exists(path) else [])
        if '/activity/' in url:
            return _resp(load(fixture_dir, f"activity_{url.rsplit('/', 1)[1]}.json"))
        if url.endswith('/activities'):
            return _resp(load(fixture_dir, 'activities_list.json'))
        if url.endswith('/wellness'):
            return _resp(load(fixture_dir, 'wellness.json'))
        return _resp(load(fixture_dir, 'athlete.json'))
    return get


def check_activity(fixture_dir, fake_id, user_id):
    info = manifest(fixture_dir)['activities'][fake_id]
    detail = load(fixture_dir, f'activity_{fake_id}.json')

    if info['kind'] == 'strava':
        assert intervals.is_strava_stub(detail)
        with patch('requests.get', side_effect=fake_api(fixture_dir)):
            with pytest.raises(ValueError, match='Strava'):
                intervals.import_single_intervals_activity(user_id, 'a0', 'key', fake_id, summary=detail)
        return

    fields = intervals.normalize_intervals_activity(detail)
    assert fields['sport_type'], "sport type not found"
    assert fields['start_time'], "start time not found"
    assert fields['duration'] is not None or fields['moving_time'] is not None, "duration not found"

    stream_types = detail.get('stream_types') or []
    if 'heartrate' in stream_types:
        assert fields['avg_hr'] is not None, "activity has HR but avg_hr was not mapped"
        assert fields['max_hr'] is not None, "activity has HR but max_hr was not mapped"
    if 'watts' in stream_types:
        assert fields['avg_power'] is not None, "activity has power but avg_power was not mapped"
    if 'latlng' in stream_types:
        assert fields['distance'] is not None, "GPS activity without distance"
    if detail.get('icu_training_load') is not None:
        assert fields['training_load'] == detail['icu_training_load']

    # The list entry (used by the background sync) must carry the same key data
    listed = next(a for a in load(fixture_dir, 'activities_list.json') if a.get('id') == fake_id)
    listed_fields = intervals.normalize_intervals_activity(listed)
    missing = [k for k in SYNC_CRITICAL_FIELDS if fields[k] is not None and listed_fields[k] is None]
    assert not missing, f"activity list entries lack {missing}; the sync must fetch full activities"

    # Import twice: route exactly when there is GPS, never duplicates
    streams_path = os.path.join(fixture_dir, f'streams_{fake_id}.json')
    streams = load(fixture_dir, f'streams_{fake_id}.json') if os.path.exists(streams_path) else []
    has_latlng = any(s.get('type') == 'latlng' and s.get('data') for s in streams)
    with patch('requests.get', side_effect=fake_api(fixture_dir)):
        first = intervals.import_single_intervals_activity(user_id, 'a0', 'key', fake_id)
        second = intervals.import_single_intervals_activity(user_id, 'a0', 'key', fake_id)
    assert (first['route_id'] is not None) == has_latlng
    assert first['route_id'] == second['route_id']
    activity = db.get_activity_by_source(user_id, 'intervals', fake_id)
    assert activity['has_gps'] == (1 if has_latlng else 0)
    assert len([a for a in db.get_activities(user_id) if a['source_id'] == fake_id]) == 1


def check_wellness(fixture_dir):
    records = load(fixture_dir, 'wellness.json')
    for record in records:
        fields = intervals.normalize_intervals_wellness(record)
        for raw_key, column in WELLNESS_MAPPING.items():
            value = record.get(raw_key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                assert fields[column] == pytest.approx(value), f"wellness {raw_key} not mapped to {column}"


def check_athlete(fixture_dir, user_id):
    profile = load(fixture_dir, 'athlete.json')
    settings = [s for s in profile.get('sportSettings') or [] if s.get('types')]
    with patch('requests.get', side_effect=fake_api(fixture_dir)):
        count = intervals.sync_intervals_thresholds(user_id, 'a0', 'key')
    assert count == len(settings)
    stored = {t['sport']: t for t in db.get_athlete_thresholds(user_id)}
    for setting in settings:
        row = stored[setting['types'][0]]
        for key in ('ftp', 'lthr', 'max_hr', 'threshold_pace'):
            if isinstance(setting.get(key), (int, float)):
                assert row[key] == pytest.approx(setting[key]), f"threshold {key} not mapped"

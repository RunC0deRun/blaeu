import pytest

import db


SAMPLE_GPX = b"""<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" creator="test" xmlns="http://www.topografix.com/GPX/1/1">
  <trk><name>Morning Ride</name><trkseg>
    <trkpt lat="52.5200" lon="13.4050"><ele>34</ele><time>2026-06-01T08:00:00Z</time></trkpt>
    <trkpt lat="52.5210" lon="13.4060"><ele>36</ele><time>2026-06-01T08:00:10Z</time></trkpt>
  </trkseg></trk>
</gpx>"""

STRENGTH = {
    'name': 'Gym Session', 'sport_type': 'WeightTraining',
    'start_time': '2026-06-04 17:00:00', 'start_time_local': '2026-06-04 19:00:00',
    'timezone': 'Europe/Amsterdam', 'duration': 3000, 'moving_time': 2700,
    'avg_hr': 118, 'max_hr': 151, 'training_load': 35, 'is_indoor': True,
    'raw_summary': {'id': 'i300', 'secret': 'stays server-side'},
    'hr_zone_times': [600, 1200, 900],
}

TREADMILL = {
    'name': 'Treadmill Run', 'sport_type': 'VirtualRun',
    'start_time': '2026-06-05 06:00:00', 'duration': 1800, 'moving_time': 1790,
    'distance': 5000.0, 'avg_hr': 150, 'max_hr': 172, 'avg_power': 260,
    'training_load': 42, 'is_indoor': True,
}


@pytest.fixture
def user_id(authed_client):
    return db.get_user_by_username('test_user')['id']


def _add_route(user_id, filename, file_hash):
    from gpx_parser import parse_gpx
    parsed = parse_gpx(SAMPLE_GPX)
    return db.add_route({
        'name': 'Morning Ride', 'filename': filename, 'file_hash': file_hash,
        'file_path': f'/nonexistent/{filename}', 'user_id': user_id,
        'created_at': parsed['start_time'], 'timezone': 'UTC',
    }, parsed['statistics'])


def _switch_to_second_user(client):
    client.post('/api/auth/logout')
    client.post('/api/auth/register', json={'username': 'other_user', 'password': 'Password123'})
    return db.get_user_by_username('other_user')['id']


def test_list_requires_login(client):
    assert client.get('/api/activities').status_code == 401
    assert client.delete('/api/activities/1').status_code == 401


def test_list_returns_only_mapless_activities(authed_client, user_id):
    _add_route(user_id, 'intervals_i100_abcd.gpx', 'h-gps')
    db.upsert_activity(user_id, 'intervals', 'i300', STRENGTH)
    db.upsert_activity(user_id, 'intervals', 'i400', TREADMILL)

    res = authed_client.get('/api/activities')
    assert res.status_code == 200
    data = res.get_json()
    assert [a['name'] for a in data] == ['Treadmill Run', 'Gym Session']  # newest first

    gym = data[1]
    assert gym['sport_type'] == 'WeightTraining'
    assert gym['start_time'] == '2026-06-04 17:00:00'
    assert gym['start_time_local'] == '2026-06-04 19:00:00'
    assert gym['duration'] == 3000
    assert gym['moving_time'] == 2700
    assert gym['distance'] is None
    assert gym['avg_hr'] == 118
    assert gym['max_hr'] == 151
    assert gym['training_load'] == 35
    assert gym['is_indoor'] is True
    # Bulky and raw data is not sent to the dashboard
    assert 'raw_summary' not in gym
    assert 'hr_zone_times' not in gym
    assert 'user_id' not in gym

    assert data[0]['distance'] == 5000.0
    assert data[0]['avg_power'] == 260


def test_list_skips_hidden_and_merged(authed_client, user_id):
    visible = db.upsert_activity(user_id, 'intervals', 'i300', STRENGTH)
    hidden = db.upsert_activity(user_id, 'intervals', 'i400', TREADMILL)
    db.set_activity_hidden(hidden, True)
    duplicate = db.upsert_activity(user_id, 'garmin', 'g1', STRENGTH)
    db.merge_activities([duplicate], visible)

    data = authed_client.get('/api/activities').get_json()
    assert [a['id'] for a in data] == [visible]


def test_list_is_isolated_per_user(authed_client, user_id):
    db.upsert_activity(user_id, 'intervals', 'i300', STRENGTH)

    other_id = _switch_to_second_user(authed_client)
    db.upsert_activity(other_id, 'intervals', 'i999', TREADMILL)

    data = authed_client.get('/api/activities').get_json()
    assert [a['name'] for a in data] == ['Treadmill Run']


def test_delete_tombstones_synced_activity(authed_client, user_id):
    activity_id = db.upsert_activity(user_id, 'intervals', 'i300', STRENGTH)

    res = authed_client.delete(f'/api/activities/{activity_id}')
    assert res.status_code == 200
    assert res.get_json() == {'success': True}

    assert authed_client.get('/api/activities').get_json() == []
    stored = db.get_activity(activity_id)
    assert stored['hidden'] == 1
    # The source id stays known, so the next sync does not import it again
    assert 'i300' in db.get_activity_source_ids(user_id, 'intervals')

    # Deleting twice reports not found
    assert authed_client.delete(f'/api/activities/{activity_id}').status_code == 404


def test_delete_removes_uploaded_mapless_activity(authed_client, user_id):
    activity_id = db.upsert_activity(user_id, 'upload', 'hash-x', STRENGTH)
    assert authed_client.delete(f'/api/activities/{activity_id}').status_code == 200
    assert db.get_activity(activity_id) is None


def test_delete_other_users_activity_is_forbidden(authed_client, user_id):
    activity_id = db.upsert_activity(user_id, 'intervals', 'i300', STRENGTH)

    _switch_to_second_user(authed_client)
    res = authed_client.delete(f'/api/activities/{activity_id}')
    assert res.status_code == 403
    assert db.get_activity(activity_id)['hidden'] == 0


def test_delete_unknown_activity(authed_client, user_id):
    assert authed_client.delete('/api/activities/12345').status_code == 404


def test_delete_activity_with_route_is_refused(authed_client, user_id):
    route_id = _add_route(user_id, 'intervals_i100_abcd.gpx', 'h-gps')
    activity = db.get_activity_by_source(user_id, 'intervals', 'i100')
    assert activity['route_id'] == route_id

    res = authed_client.delete(f"/api/activities/{activity['id']}")
    assert res.status_code == 409
    assert db.get_activity(activity['id'])['hidden'] == 0
    assert db.get_route(route_id) is not None

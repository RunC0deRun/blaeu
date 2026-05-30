import os
import tempfile
import pytest
import io
from flask import json
from app import app
import db

# Setup temporary database for testing
@pytest.fixture
def client(monkeypatch):
    db_fd, temp_db_path = tempfile.mkstemp()
    temp_gpx_dir = tempfile.mkdtemp()
    
    # Configure app env vars for testing
    monkeypatch.setenv("DATA_DIR", temp_gpx_dir)
    monkeypatch.setenv("BLAEU_ALLOW_REGISTRATION", "true")
    monkeypatch.setattr("db.DB_PATH", temp_db_path)
    monkeypatch.setattr("db.DATA_DIR", temp_gpx_dir)
    monkeypatch.setattr("app.DATA_DIR", temp_gpx_dir)
    monkeypatch.setattr("app.GPX_STORE_DIR", os.path.join(temp_gpx_dir, 'gpx'))
    monkeypatch.setattr("app.TILES_CACHE_DIR", os.path.join(temp_gpx_dir, 'tiles_cache'))
    
    # Re-initialize the test database
    db.init_db()
    
    app.config.update({
        'TESTING': True,
        'PROPAGATE_EXCEPTIONS': True,
        'SECRET_KEY': 'test-secret-key'
    })
    
    with app.test_client() as client:
        client.post('/api/auth/register', json={
            'username': 'test_user',
            'password': 'password123'
        })
        yield client
        
    os.close(db_fd)
    os.unlink(temp_db_path)
    import shutil
    shutil.rmtree(temp_gpx_dir, ignore_errors=True)

# Mock GPX data
GPX_DATA_1 = """<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" creator="Mock" xmlns="http://www.topografix.com/GPX/1/1">
  <trk>
    <name>Route A</name>
    <trkseg>
      <trkpt lat="52.5200" lon="13.4050"><ele>34.0</ele><time>2026-05-25T13:00:00Z</time></trkpt>
      <trkpt lat="52.5210" lon="13.4060"><ele>36.0</ele><time>2026-05-25T13:01:00Z</time></trkpt>
    </trkseg>
  </trk>
</gpx>
"""

GPX_DATA_2 = """<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" creator="Mock" xmlns="http://www.topografix.com/GPX/1/1">
  <trk>
    <name>Route B</name>
    <trkseg>
      <trkpt lat="51.5200" lon="12.4050"><ele>10.0</ele><time>2026-05-25T14:00:00Z</time></trkpt>
      <trkpt lat="51.5210" lon="12.4060"><ele>12.0</ele><time>2026-05-25T14:01:00Z</time></trkpt>
    </trkseg>
  </trk>
</gpx>
"""

def test_upload_route(client):
    data = {
        'file': (io.BytesIO(GPX_DATA_1.encode('utf-8')), 'test_route.gpx'),
        'name': 'Custom Name',
        'description': 'My custom description',
        'tags': 'run, forest'
    }
    response = client.post('/api/upload', data=data, content_type='multipart/form-data')
    assert response.status_code == 201
    
    json_data = json.loads(response.data)
    assert json_data['name'] == 'Custom Name'
    assert json_data['description'] == 'My custom description'
    assert 'run' in json_data['tags']
    assert 'forest' in json_data['tags']
    assert json_data['total_distance'] > 0
    assert json_data['avg_speed'] > 0
    assert json_data['avg_moving_speed'] > 0
    assert json_data['timezone'] == 'Europe/Berlin'
    assert json_data['created_at'] == '2026-05-25 13:00:00'
    assert json_data['timezone_abbr'] == 'CEST'

def test_upload_duplicate(client):
    data1 = {
        'file': (io.BytesIO(GPX_DATA_1.encode('utf-8')), 'test_route1.gpx')
    }
    response1 = client.post('/api/upload', data=data1, content_type='multipart/form-data')
    assert response1.status_code == 201
    
    # Upload same file content again
    data2 = {
        'file': (io.BytesIO(GPX_DATA_1.encode('utf-8')), 'test_route2.gpx')
    }
    response2 = client.post('/api/upload', data=data2, content_type='multipart/form-data')
    assert response2.status_code == 409
    
    json_data = json.loads(response2.data)
    assert 'Duplicate file' in json_data['error']

def test_get_routes_and_details(client):
    # Upload route 1
    client.post('/api/upload', data={
        'file': (io.BytesIO(GPX_DATA_1.encode('utf-8')), 'route1.gpx')
    }, content_type='multipart/form-data')
    
    # Upload route 2
    client.post('/api/upload', data={
        'file': (io.BytesIO(GPX_DATA_2.encode('utf-8')), 'route2.gpx')
    }, content_type='multipart/form-data')
    
    # Get routes list
    response = client.get('/api/routes')
    assert response.status_code == 200
    routes = json.loads(response.data)
    assert len(routes) == 2
    
    # First route in list is Route 2 (timeline from newest to oldest)
    assert routes[0]['name'] == 'Route B'
    assert routes[1]['name'] == 'Route A'
    
    # Get details for Route A
    route_id = routes[1]['id']
    details_response = client.get(f'/api/routes/{route_id}')
    assert details_response.status_code == 200
    details = json.loads(details_response.data)
    assert details['name'] == 'Route A'
    assert 'tracks' in details
    assert len(details['tracks']) == 1
    assert len(details['tracks'][0]['segments'][0]) == 2

def test_update_and_delete_route(client):
    # Upload
    res = client.post('/api/upload', data={
        'file': (io.BytesIO(GPX_DATA_1.encode('utf-8')), 'route.gpx')
    }, content_type='multipart/form-data')
    route_id = json.loads(res.data)['id']
    
    # Edit route details
    edit_data = {
        'name': 'Updated Title',
        'description': 'Updated Description',
        'tags': ['hike', 'mountains']
    }
    edit_response = client.put(f'/api/routes/{route_id}', json=edit_data)
    assert edit_response.status_code == 200
    updated = json.loads(edit_response.data)
    assert updated['name'] == 'Updated Title'
    assert updated['description'] == 'Updated Description'
    assert 'hike' in updated['tags']
    
    # Delete route
    delete_response = client.delete(f'/api/routes/{route_id}')
    assert delete_response.status_code == 200
    assert json.loads(delete_response.data)['success'] is True
    
    # Confirm it's gone
    get_response = client.get(f'/api/routes/{route_id}')
    assert get_response.status_code == 404

def test_folder_crud(client):
    # Create folder
    res = client.post('/api/folders', json={'name': 'Summer Holidays'})
    assert res.status_code == 201
    folder = json.loads(res.data)
    folder_id = folder['id']
    assert folder['name'] == 'Summer Holidays'
    
    # Try duplicate folder name
    res_dup = client.post('/api/folders', json={'name': 'Summer Holidays'})
    assert res_dup.status_code == 409
    
    # List folders
    res_list = client.get('/api/folders')
    assert res_list.status_code == 200
    folders = json.loads(res_list.data)
    assert len(folders) == 1
    assert folders[0]['name'] == 'Summer Holidays'
    
    # Associate route with folder during upload
    client.post('/api/upload', data={
        'file': (io.BytesIO(GPX_DATA_1.encode('utf-8')), 'route.gpx'),
        'folder_id': folder_id
    }, content_type='multipart/form-data')
    
    # Check that route is in folder
    routes = json.loads(client.get(f'/api/routes?folder_id={folder_id}').data)
    assert len(routes) == 1
    assert routes[0]['folder_name'] == 'Summer Holidays'
    
    # Delete folder
    res_del = client.delete(f'/api/folders/{folder_id}')
    assert res_del.status_code == 200
    
    # Verify folder list is empty
    folders_empty = json.loads(client.get('/api/folders').data)
    assert len(folders_empty) == 0

def test_convert_video_endpoint(client, monkeypatch):
    import subprocess
    from unittest.mock import MagicMock
    
    # Mock subprocess.run
    mock_run = MagicMock()
    monkeypatch.setattr(subprocess, "run", mock_run)
    
    # Simulates ffmpeg writing to the output file path
    def side_effect(cmd, *args, **kwargs):
        out_path = cmd[-1]
        with open(out_path, 'wb') as f:
            f.write(b"mock_mp4_content")
        return MagicMock()
    mock_run.side_effect = side_effect

    data = {
        'file': (io.BytesIO(b"mock_webm_content"), 'test.webm'),
        'fps': '30'
    }
    response = client.post('/api/convert-video', data=data, content_type='multipart/form-data')
    assert response.status_code == 200
    assert response.data == b"mock_mp4_content"
    assert response.mimetype == 'video/mp4'
    assert mock_run.called
    
    # Verify the command arguments
    call_args = mock_run.call_args[0][0]
    assert 'ffmpeg' in call_args
    assert '-filter:v' in call_args
    assert 'setpts=N/(30*TB)' in call_args
    assert '-r' in call_args
    assert '30' in call_args
    assert '-crf' in call_args
    assert '20' in call_args

def test_convert_video_webm_endpoint(client, monkeypatch):
    import subprocess
    from unittest.mock import MagicMock
    
    # Mock subprocess.run
    mock_run = MagicMock()
    monkeypatch.setattr(subprocess, "run", mock_run)
    
    # Simulates ffmpeg writing to the output file path
    def side_effect(cmd, *args, **kwargs):
        out_path = cmd[-1]
        with open(out_path, 'wb') as f:
            f.write(b"mock_webm_content")
        return MagicMock()
    mock_run.side_effect = side_effect

    data = {
        'file': (io.BytesIO(b"mock_webm_content"), 'test.webm'),
        'fps': '60',
        'format': 'webm',
        'bitrate': '12000000'
    }
    response = client.post('/api/convert-video', data=data, content_type='multipart/form-data')
    assert response.status_code == 200
    assert response.data == b"mock_webm_content"
    assert response.mimetype == 'video/webm'
    assert mock_run.called
    
    # Verify the command arguments for WebM
    call_args = mock_run.call_args[0][0]
    assert 'ffmpeg' in call_args
    assert '-filter:v' in call_args
    assert 'setpts=N/(60*TB)' in call_args
    assert '-r' in call_args
    assert '60' in call_args
    assert '-c:v' in call_args
    assert 'libvpx' in call_args
    assert '-crf' in call_args
    assert '4' in call_args
    assert '-b:v' in call_args
    assert '12000000' in call_args


def test_garmin_status_initially_disconnected(client):
    res = client.get('/api/garmin/status')
    assert res.status_code == 200
    data = json.loads(res.data)
    assert data['status'] == 'disconnected'


def test_garmin_connect_mfa_required(client, monkeypatch):
    from unittest.mock import MagicMock
    from app import MfaRequiredException
    
    mock_garmin = MagicMock()
    # First call throws MFA exception
    def mock_login(*args, **kwargs):
        raise MfaRequiredException()
        
    mock_garmin.return_value.login = mock_login
    monkeypatch.setattr('garminconnect.Garmin', mock_garmin)
    
    res = client.post('/api/garmin/connect', json={'email': 'test@example.com', 'password': 'password123'})
    assert res.status_code == 200
    data = json.loads(res.data)
    assert data['status'] == 'mfa_required'


def test_garmin_connect_success(client, monkeypatch):
    from unittest.mock import MagicMock
    
    mock_garmin = MagicMock()
    mock_garmin.return_value.display_name = 'Garmin Champ'
    
    monkeypatch.setattr('garminconnect.Garmin', mock_garmin)
    
    res = client.post('/api/garmin/connect', json={
        'email': 'test@example.com', 
        'password': 'password123',
        'mfa_code': '123456'
    })
    assert res.status_code == 200
    data = json.loads(res.data)
    assert data['status'] == 'connected'
    assert data['display_name'] == 'Garmin Champ'
    
    # Check status endpoint now returns connected
    status_res = client.get('/api/garmin/status')
    assert status_res.status_code == 200
    status_data = json.loads(status_res.data)
    assert status_data['status'] == 'connected'
    assert status_data['email'] == 'test@example.com'
    assert status_data['display_name'] == 'Garmin Champ'

    # Verify token directory permissions
    import stat
    parent_path = os.path.join(os.environ["DATA_DIR"], 'garmin_tokens')
    assert os.path.exists(parent_path)
    parent_mode = os.stat(parent_path).st_mode
    assert stat.S_IMODE(parent_mode) == 0o700
    
    user_dir_path = os.path.join(parent_path, '1')
    assert os.path.exists(user_dir_path)
    user_dir_mode = os.stat(user_dir_path).st_mode
    assert stat.S_IMODE(user_dir_mode) == 0o700



def test_garmin_disconnect(client, monkeypatch):
    import db
    db.save_garmin_connection(1, 'test@example.com', 'Garmin Champ')
    
    res = client.post('/api/garmin/disconnect')
    assert res.status_code == 200
    data = json.loads(res.data)
    assert data['status'] == 'disconnected'
    
    # Verify connection is deleted from DB
    connection = db.get_garmin_connection(1)
    assert connection is None


def test_garmin_activities_not_connected(client):
    res = client.get('/api/garmin/activities')
    assert res.status_code == 400
    data = json.loads(res.data)
    assert 'Garmin not connected' in data['error']


def test_garmin_activities_success(client, monkeypatch):
    from unittest.mock import MagicMock
    import db
    
    db.save_garmin_connection(1, 'test@example.com', 'Garmin Champ')
    
    mock_garmin = MagicMock()
    mock_garmin.return_value.get_activities.return_value = [
        {
            'activityId': '98765',
            'activityName': 'Morning Run',
            'activityType': {'typeKey': 'running'},
            'startTimeLocal': '2026-05-25 08:00:00',
            'distance': 10000.0,
            'duration': 3600.0
        }
    ]
    monkeypatch.setattr('garminconnect.Garmin', mock_garmin)
    
    res = client.get('/api/garmin/activities')
    assert res.status_code == 200
    data = json.loads(res.data)
    assert data['status'] == 'success'
    assert len(data['activities']) == 1
    assert data['activities'][0]['activityId'] == '98765'
    assert data['activities'][0]['activityName'] == 'Morning Run'
    assert data['activities'][0]['activityType'] == 'running'


def test_garmin_activities_expired(client, monkeypatch):
    from unittest.mock import MagicMock
    import db
    
    db.save_garmin_connection(1, 'test@example.com', 'Garmin Champ')
    
    mock_garmin = MagicMock()
    # login raises exception simulating expired token
    mock_garmin.return_value.login.side_effect = Exception('Token expired')
    monkeypatch.setattr('garminconnect.Garmin', mock_garmin)
    
    res = client.get('/api/garmin/activities')
    assert res.status_code == 401
    data = json.loads(res.data)
    assert data['status'] == 'needs_reauthentication'


def test_garmin_import_success(client, monkeypatch):
    from unittest.mock import MagicMock
    import db
    
    # Set up active connection in DB
    db.save_garmin_connection(1, 'test@example.com', 'Garmin Champ')
    
    mock_garmin = MagicMock()
    mock_garmin.return_value.download_activity.return_value = GPX_DATA_1.encode('utf-8')
    monkeypatch.setattr('garminconnect.Garmin', mock_garmin)
    
    res = client.post('/api/garmin/import', json={'activityId': '123456789'})
    assert res.status_code == 200
    data = json.loads(res.data)
    assert data['status'] == 'success'
    assert data['route_id'] is not None
    assert 'Route A' in data['name']
    
    # Verify the route actually exists in routes list
    routes_res = client.get('/api/routes')
    routes = json.loads(routes_res.data)
    assert len(routes) == 1
    assert routes[0]['name'] == 'Route A'
    assert routes[0]['created_at'] == '2026-05-25 13:00:00'


def test_garmin_connect_rate_limit(client, monkeypatch):
    from unittest.mock import MagicMock
    from garminconnect.exceptions import GarminConnectTooManyRequestsError
    
    mock_garmin = MagicMock()
    mock_garmin.return_value.login.side_effect = GarminConnectTooManyRequestsError("Too many login attempts.")
    monkeypatch.setattr('garminconnect.Garmin', mock_garmin)
    
    res = client.post('/api/garmin/connect', json={
        'email': 'test@example.com',
        'password': 'password123'
    })
    assert res.status_code == 429
    data = json.loads(res.data)
    assert "Too many login attempts" in data['error']


def test_garmin_activities_rate_limit(client, monkeypatch):
    from unittest.mock import MagicMock
    from garminconnect.exceptions import GarminConnectTooManyRequestsError
    import db
    
    db.save_garmin_connection(1, 'test@example.com', 'Garmin Champ')
    
    mock_garmin = MagicMock()
    mock_garmin.return_value.login.side_effect = GarminConnectTooManyRequestsError("Rate limited during login.")
    monkeypatch.setattr('garminconnect.Garmin', mock_garmin)
    
    res = client.get('/api/garmin/activities')
    assert res.status_code == 429
    data = json.loads(res.data)
    assert "Rate limit exceeded by Garmin" in data['error']
    
    # Verify we did NOT clear the connection
    assert db.get_garmin_connection(1) is not None


def test_garmin_import_rate_limit(client, monkeypatch):
    from unittest.mock import MagicMock
    from garminconnect.exceptions import GarminConnectTooManyRequestsError
    import db
    
    db.save_garmin_connection(1, 'test@example.com', 'Garmin Champ')
    
    mock_garmin = MagicMock()
    mock_garmin.return_value.login.side_effect = GarminConnectTooManyRequestsError("Rate limited during login.")
    monkeypatch.setattr('garminconnect.Garmin', mock_garmin)
    
    res = client.post('/api/garmin/import', json={'activityId': '123456789'})
    assert res.status_code == 429
    data = json.loads(res.data)
    assert "Garmin import failed" in data['error']


def test_garmin_connect_fallback_rate_limit(client, monkeypatch):
    from unittest.mock import MagicMock
    from garminconnect.exceptions import GarminConnectAuthenticationError
    
    mock_garmin = MagicMock()
    # Mock login to succeed, but di_token is None (fallback case)
    mock_garmin.return_value.client.di_token = None
    monkeypatch.setattr('garminconnect.Garmin', mock_garmin)
    
    res = client.post('/api/garmin/connect', json={
        'email': 'test@example.com', 
        'password': 'password123',
        'mfa_code': '123456'
    })
    # Should fail with 401 and explain DI OAuth token failure
    assert res.status_code == 401
    data = json.loads(res.data)
    assert "failed to acquire persistent DI OAuth tokens" in data['error']


def test_route_sorting_and_sql_injection_fallback(client):
    # Upload Route A (created at 13:00)
    client.post('/api/upload', data={
        'file': (io.BytesIO(GPX_DATA_1.encode('utf-8')), 'route1.gpx'),
        'name': 'Route A'
    }, content_type='multipart/form-data')
    
    # Upload Route B (created at 14:00)
    client.post('/api/upload', data={
        'file': (io.BytesIO(GPX_DATA_2.encode('utf-8')), 'route2.gpx'),
        'name': 'Route B'
    }, content_type='multipart/form-data')
    
    # 1. Sort by name ASC
    res_name_asc = client.get('/api/routes?sort_by=name&sort_order=asc')
    assert res_name_asc.status_code == 200
    routes_name_asc = json.loads(res_name_asc.data)
    assert len(routes_name_asc) == 2
    assert routes_name_asc[0]['name'] == 'Route A'
    assert routes_name_asc[1]['name'] == 'Route B'

    # 2. Sort by name DESC
    res_name_desc = client.get('/api/routes?sort_by=name&sort_order=desc')
    assert res_name_desc.status_code == 200
    routes_name_desc = json.loads(res_name_desc.data)
    assert routes_name_desc[0]['name'] == 'Route B'
    assert routes_name_desc[1]['name'] == 'Route A'

    # 3. Sort by date ASC
    res_date_asc = client.get('/api/routes?sort_by=date&sort_order=asc')
    assert res_date_asc.status_code == 200
    routes_date_asc = json.loads(res_date_asc.data)
    assert routes_date_asc[0]['name'] == 'Route A'
    assert routes_date_asc[1]['name'] == 'Route B'

    # 4. Fallback on invalid sort_by
    res_invalid_by = client.get('/api/routes?sort_by=invalid_column')
    assert res_invalid_by.status_code == 200
    # Should fallback to default (date DESC)
    routes_invalid_by = json.loads(res_invalid_by.data)
    assert routes_invalid_by[0]['name'] == 'Route B'
    assert routes_invalid_by[1]['name'] == 'Route A'

    # 5. Fallback on SQL injection in sort_by
    res_sqli_by = client.get('/api/routes?sort_by=name;+DROP+TABLE+routes;--')
    assert res_sqli_by.status_code == 200
    routes_sqli_by = json.loads(res_sqli_by.data)
    # Should fallback to default (date DESC) and not raise/execute the SQL injection
    assert len(routes_sqli_by) == 2
    assert routes_sqli_by[0]['name'] == 'Route B'
    assert routes_sqli_by[1]['name'] == 'Route A'

    # 6. Fallback on SQL injection in sort_order
    res_sqli_order = client.get('/api/routes?sort_by=name&sort_order=asc;+DROP+TABLE+routes;--')
    assert res_sqli_order.status_code == 200
    routes_sqli_order = json.loads(res_sqli_order.data)
    # Should fallback to default direction (DESC) for the whitelisted column (name)
    assert len(routes_sqli_order) == 2
    assert routes_sqli_order[0]['name'] == 'Route B'
    assert routes_sqli_order[1]['name'] == 'Route A'

def test_upload_file_too_large(client):
    # Try uploading a 51MB file
    large_data = b"X" * (51 * 1024 * 1024)
    data = {
        'file': (io.BytesIO(large_data), 'large_route.gpx')
    }
    response = client.post('/api/upload', data=data, content_type='multipart/form-data')
    assert response.status_code == 413
    json_data = json.loads(response.data)
    assert "File too large" in json_data['error']

def test_convert_video_invalid_params(client, monkeypatch):
    import subprocess
    from unittest.mock import MagicMock
    
    mock_run = MagicMock()
    monkeypatch.setattr(subprocess, "run", mock_run)
    
    def side_effect(cmd, *args, **kwargs):
        out_path = cmd[-1]
        with open(out_path, 'wb') as f:
            f.write(b"mock_content")
        return MagicMock()
    mock_run.side_effect = side_effect

    # 1. Post with invalid FPS and format
    data = {
        'file': (io.BytesIO(b"mock_webm_content"), 'test.webm'),
        'fps': 'invalid_fps',
        'format': 'invalid_format'
    }
    response = client.post('/api/convert-video', data=data, content_type='multipart/form-data')
    assert response.status_code == 200
    assert response.mimetype == 'video/mp4' # fallback to default mp4
    
    call_args = mock_run.call_args[0][0]
    assert 'setpts=N/(30*TB)' in call_args
    assert '30' in call_args

    # 2. Post with format webm and excessive bitrate
    data_webm = {
        'file': (io.BytesIO(b"mock_webm_content"), 'test.webm'),
        'format': 'webm',
        'bitrate': '999999999999'  # Above 100Mbps
    }
    mock_run.reset_mock()
    response_webm = client.post('/api/convert-video', data=data_webm, content_type='multipart/form-data')
    assert response_webm.status_code == 200
    assert response_webm.mimetype == 'video/webm'
    
    call_args_webm = mock_run.call_args[0][0]
    # Bitrate should fall back to 12000000
    assert '-b:v' in call_args_webm
    idx = call_args_webm.index('-b:v')
    assert call_args_webm[idx + 1] == '12000000'

def test_tile_proxy_boundaries(client, monkeypatch):
    import requests
    from unittest.mock import MagicMock
    
    mock_get = MagicMock()
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.content = b"mock_tile_image_content"
    mock_get.return_value = mock_response
    monkeypatch.setattr(requests, "get", mock_get)
    
    # 1. Valid tile request
    res_valid = client.get('/api/tiles/10/500/500.png')
    assert res_valid.status_code == 200
    assert res_valid.data == b"mock_tile_image_content"
    assert mock_get.called
    args, kwargs = mock_get.call_args
    assert kwargs.get('timeout') == 10
    
    # 2. Invalid zoom level (too high)
    res_invalid_z = client.get('/api/tiles/20/500/500.png')
    assert res_invalid_z.status_code == 400
    assert b"Invalid zoom level" in res_invalid_z.data
    
    # 3. Out of bounds x coordinate
    res_invalid_x = client.get('/api/tiles/3/8/5.png')
    assert res_invalid_x.status_code == 400
    assert b"Tile coordinates out of bounds" in res_invalid_x.data

    # 4. Out of bounds y coordinate
    res_invalid_y = client.get('/api/tiles/3/5/8.png')
    assert res_invalid_y.status_code == 400
    assert b"Tile coordinates out of bounds" in res_invalid_y.data

def test_session_cookie_flags():
    assert app.config['SESSION_COOKIE_HTTPONLY'] is True
    assert app.config['SESSION_COOKIE_SAMESITE'] == 'Lax'
    assert app.config['SESSION_COOKIE_SECURE'] is True





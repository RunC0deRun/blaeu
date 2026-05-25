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
    monkeypatch.setattr("db.DB_PATH", temp_db_path)
    monkeypatch.setattr("db.DATA_DIR", temp_gpx_dir)
    monkeypatch.setattr("app.GPX_STORE_DIR", os.path.join(temp_gpx_dir, 'gpx'))
    monkeypatch.setattr("app.TILES_CACHE_DIR", os.path.join(temp_gpx_dir, 'tiles_cache'))
    
    # Re-initialize the test database
    db.init_db()
    
    app.config.update({
        'TESTING': True,
        'PROPAGATE_EXCEPTIONS': True
    })
    
    with app.test_client() as client:
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

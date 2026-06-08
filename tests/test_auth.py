import os
import tempfile
import pytest
import io
from flask import json
from app import app
import db



# Mock GPX data
GPX_DATA = """<?xml version="1.0" encoding="UTF-8"?>
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

def test_auth_status_empty_db(client):
    response = client.get('/api/auth/status')
    assert response.status_code == 200
    res_data = json.loads(response.data)
    assert res_data['logged_in'] is False
    assert res_data['no_users_exist'] is True

def test_auth_register_and_login(client):
    # 1. Register first user (should be admin)
    response = client.post('/api/auth/register', json={
        'username': 'admin_user',
        'password': 'Password123'
    })
    assert response.status_code == 201
    res_data = json.loads(response.data)
    assert res_data['success'] is True
    assert res_data['user']['username'] == 'admin_user'
    assert res_data['user']['is_admin'] == 1
    
    # Status check should show logged in
    response = client.get('/api/auth/status')
    assert response.status_code == 200
    res_data = json.loads(response.data)
    assert res_data['logged_in'] is True
    assert res_data['user']['username'] == 'admin_user'
    assert res_data['user']['is_admin'] == 1
    
    # Logout
    response = client.post('/api/auth/logout')
    assert response.status_code == 200
    
    # Status check should show logged out
    response = client.get('/api/auth/status')
    res_data = json.loads(response.data)
    assert res_data['logged_in'] is False
    assert res_data['no_users_exist'] is False  # A user exists now!
    
    # Login again
    response = client.post('/api/auth/login', json={
        'username': 'admin_user',
        'password': 'Password123'
    })
    assert response.status_code == 200
    res_data = json.loads(response.data)
    assert res_data['success'] is True
    assert res_data['user']['username'] == 'admin_user'

def test_auth_invalid_credentials(client):
    # Register first user
    client.post('/api/auth/register', json={
        'username': 'test_user',
        'password': 'Password123'
    })
    client.post('/api/auth/logout')
    
    # Wrong password
    response = client.post('/api/auth/login', json={
        'username': 'test_user',
        'password': 'wrong_password'
    })
    assert response.status_code == 401
    
    # Non-existent user
    response = client.post('/api/auth/login', json={
        'username': 'other_user',
        'password': 'Password123'
    })
    assert response.status_code == 401

def test_registration_validation(client):
    # Short username
    response = client.post('/api/auth/register', json={
        'username': 'ab',
        'password': 'Password123'
    })
    assert response.status_code == 400
    
    # Short password
    response = client.post('/api/auth/register', json={
        'username': 'valid_user',
        'password': '1234567'
    })
    assert response.status_code == 400
    assert 'password at least 8 characters' in response.json['error']

def test_route_scoping_and_privacy(client):
    # Register Admin user
    client.post('/api/auth/register', json={
        'username': 'admin_user',
        'password': 'Password123'
    })
    
    # Upload Route A under Admin
    data_upload = {
        'file': (io.BytesIO(GPX_DATA.encode('utf-8')), 'route_a.gpx'),
        'name': 'Route A',
        'description': 'Admin Route'
    }
    res_upload = client.post('/api/upload', data=data_upload, content_type='multipart/form-data')
    assert res_upload.status_code == 201
    route_a_id = json.loads(res_upload.data)['id']
    
    # Log out Admin
    client.post('/api/auth/logout')
    
    # Register Normal User B
    client.post('/api/auth/register', json={
        'username': 'user_b',
        'password': 'Password123'
    })
    
    # List routes as User B. Route A should NOT be visible because it is private by default
    res_list = client.get('/api/routes')
    routes = json.loads(res_list.data)
    assert len(routes) == 0
    
    # Try to access details of Route A as User B (Private). Should be Forbidden.
    res_details = client.get(f'/api/routes/{route_a_id}')
    assert res_details.status_code == 403
    
    # Try to edit Route A as User B. Should be Forbidden.
    res_edit = client.put(f'/api/routes/{route_a_id}', json={'name': 'Hacked Route'})
    assert res_edit.status_code == 403
    
    # Try to delete Route A as User B. Should be Forbidden.
    res_delete = client.delete(f'/api/routes/{route_a_id}')
    assert res_delete.status_code == 403
    
    # Log out User B
    client.post('/api/auth/logout')
    
    # Log back in as Admin User
    client.post('/api/auth/login', json={
        'username': 'admin_user',
        'password': 'Password123'
    })
    
    # Make Route A public
    res_make_public = client.put(f'/api/routes/{route_a_id}', json={'is_public': True})
    assert res_make_public.status_code == 200
    
    # Log out Admin
    client.post('/api/auth/logout')
    
    # Log back in as User B
    client.post('/api/auth/login', json={
        'username': 'user_b',
        'password': 'Password123'
    })
    
    # List routes as User B. Route A should now be visible since it is public.
    res_list2 = client.get('/api/routes')
    routes2 = json.loads(res_list2.data)
    assert len(routes2) == 1
    assert routes2[0]['id'] == route_a_id
    assert routes2[0]['is_owner'] is False
    assert routes2[0]['owner_username'] == 'admin_user'
    
    # Details of Route A should now be visible to User B since it is public.
    res_details2 = client.get(f'/api/routes/{route_a_id}')
    assert res_details2.status_code == 200
    route_details = json.loads(res_details2.data)
    assert route_details['name'] == 'Route A'
    assert route_details['is_owner'] is False
    
    # Try to edit public Route A as User B. Still should be Forbidden.
    res_edit2 = client.put(f'/api/routes/{route_a_id}', json={'name': 'Hacked Route'})
    assert res_edit2.status_code == 403

def test_admin_user_management(client):
    # 1. Register Admin
    client.post('/api/auth/register', json={
        'username': 'admin_user',
        'password': 'Password123'
    })
    
    # 2. Register Normal User B
    client.post('/api/auth/register', json={
        'username': 'user_b',
        'password': 'Password123'
    })
    user_b_id = json.loads(client.get('/api/auth/status').data)['user']['id']
    
    # Upload a route under User B
    data_upload = {
        'file': (io.BytesIO(GPX_DATA.encode('utf-8')), 'route_b.gpx')
    }
    client.post('/api/upload', data=data_upload, content_type='multipart/form-data')
    
    # Non-admin try to list users. Should be Forbidden.
    res_list_fail = client.get('/api/auth/users')
    assert res_list_fail.status_code == 403
    
    # Non-admin try to delete another user. Should be Forbidden.
    res_delete_fail = client.delete(f'/api/auth/users/{user_b_id}')
    assert res_delete_fail.status_code == 403
    
    # Logout User B
    client.post('/api/auth/logout')
    
    # Log in as Admin
    client.post('/api/auth/login', json={
        'username': 'admin_user',
        'password': 'Password123'
    })
    
    # Admin list users
    res_list = client.get('/api/auth/users')
    assert res_list.status_code == 200
    users = json.loads(res_list.data)
    assert len(users) == 2
    assert any(u['username'] == 'user_b' for u in users)
    
    # Admin tries to self-delete. Should be Bad Request.
    admin_id = json.loads(client.get('/api/auth/status').data)['user']['id']
    res_self_delete = client.delete(f'/api/auth/users/{admin_id}')
    assert res_self_delete.status_code == 400
    
    # Admin deletes User B
    res_delete = client.delete(f'/api/auth/users/{user_b_id}')
    assert res_delete.status_code == 200
    
    # Check users list again
    res_list2 = client.get('/api/auth/users')
    users2 = json.loads(res_list2.data)
    assert len(users2) == 1
    assert users2[0]['username'] == 'admin_user'


def test_default_map_style_profile(client):
    # Try to set style while unauthorized
    res = client.put('/api/auth/default-map-style', json={'default_map_style': 'noir'})
    assert res.status_code == 401
    
    # Register and login
    res = client.post('/api/auth/register', json={
        'username': 'style_user',
        'password': 'Password123'
    })
    assert res.status_code == 201
    user_data = json.loads(res.data)['user']
    assert user_data['default_map_style'] == 'dark' # default
    
    # Update default map style
    res = client.put('/api/auth/default-map-style', json={'default_map_style': 'blueprint'})
    assert res.status_code == 200
    assert json.loads(res.data)['default_map_style'] == 'blueprint'
    
    # Status check should reflect updated default style
    res = client.get('/api/auth/status')
    assert res.status_code == 200
    assert json.loads(res.data)['user']['default_map_style'] == 'blueprint'
    
    # Log out
    client.post('/api/auth/logout')
    
    # Login check should reflect updated default style
    res = client.post('/api/auth/login', json={
        'username': 'style_user',
        'password': 'Password123'
    })
    assert res.status_code == 200
    assert json.loads(res.data)['user']['default_map_style'] == 'blueprint'

def test_week_start_day_profile(client):
    # Try to set start of week while unauthorized
    res = client.put('/api/auth/week-start-day', json={'week_start_day': 'Sunday'})
    assert res.status_code == 401
    
    # Register and login
    res = client.post('/api/auth/register', json={
        'username': 'week_user',
        'password': 'Password123'
    })
    assert res.status_code == 201
    user_data = json.loads(res.data)['user']
    assert user_data['week_start_day'] == 'Monday' # default
    
    # Try to set an invalid day
    res = client.put('/api/auth/week-start-day', json={'week_start_day': 'Funday'})
    assert res.status_code == 400
    assert 'Invalid week_start_day' in json.loads(res.data)['error']
    
    # Update to a valid day
    res = client.put('/api/auth/week-start-day', json={'week_start_day': 'Sunday'})
    assert res.status_code == 200
    assert json.loads(res.data)['week_start_day'] == 'Sunday'
    
    # Status check should reflect updated start day
    res = client.get('/api/auth/status')
    assert res.status_code == 200
    assert json.loads(res.data)['user']['week_start_day'] == 'Sunday'
    
    # Log out
    client.post('/api/auth/logout')
    
    # Login check should reflect updated start day
    res = client.post('/api/auth/login', json={
        'username': 'week_user',
        'password': 'Password123'
    })
    assert res.status_code == 200
    assert json.loads(res.data)['user']['week_start_day'] == 'Sunday'


def test_registration_disabled_after_first_user(client, monkeypatch):
    # Register first user
    res1 = client.post('/api/auth/register', json={
        'username': 'first_user',
        'password': 'Password123'
    })
    assert res1.status_code == 201

    # Temporarily disable registration by clearing/setting env var to false
    monkeypatch.setenv("BLAEU_ALLOW_REGISTRATION", "false")

    # Attempt to register second user
    res2 = client.post('/api/auth/register', json={
        'username': 'second_user',
        'password': 'Password123'
    })
    assert res2.status_code == 403
    assert b"Registration is closed" in res2.data

def test_csrf_protection_enforced(client, monkeypatch):
    # Register first user
    res_reg = client.post('/api/auth/register', json={
        'username': 'csrf_user',
        'password': 'Password123'
    })
    assert res_reg.status_code == 201
    csrf_token = json.loads(res_reg.data)['csrf_token']
    assert csrf_token is not None

    # Enable CSRF checks by temporarily disabling testing mode in app config
    monkeypatch.setitem(app.config, 'TESTING', False)

    try:
        # 1. State-changing request WITHOUT CSRF header should fail with 400 Bad Request
        res_fail = client.post('/api/folders', json={'name': 'No CSRF Folder'})
        assert res_fail.status_code == 400
        assert b"CSRF token validation failed" in res_fail.data

        # 2. State-changing request with WRONG CSRF header should fail with 400 Bad Request
        res_fail_wrong = client.post('/api/folders', json={'name': 'Wrong CSRF Folder'}, headers={'X-CSRF-Token': 'wrong_token'})
        assert res_fail_wrong.status_code == 400

        # 3. State-changing request with CORRECT CSRF header should succeed
        res_success = client.post('/api/folders', json={'name': 'CSRF Folder'}, headers={'X-CSRF-Token': csrf_token})
        assert res_success.status_code == 201
        assert json.loads(res_success.data)['name'] == 'CSRF Folder'
    finally:
        # Always restore TESTING mode
        app.config['TESTING'] = True


def test_password_complexity_requirements(client):
    # 1. No uppercase
    response = client.post('/api/auth/register', json={
        'username': 'weak_user1',
        'password': 'password123'
    })
    assert response.status_code == 400
    assert "Password must contain" in response.json['error']
    
    # 2. No lowercase
    response = client.post('/api/auth/register', json={
        'username': 'weak_user2',
        'password': 'PASSWORD123'
    })
    assert response.status_code == 400
    assert "Password must contain" in response.json['error']
    
    # 3. No digits
    response = client.post('/api/auth/register', json={
        'username': 'weak_user3',
        'password': 'Passwordabc'
    })
    assert response.status_code == 400
    assert "Password must contain" in response.json['error']
    
    # 4. Valid password
    response = client.post('/api/auth/register', json={
        'username': 'strong_user',
        'password': 'Password123'
    })
    assert response.status_code == 201


def test_sanitized_duplicate_registration_error(client):
    # Register first user
    response = client.post('/api/auth/register', json={
        'username': 'duplicate_user',
        'password': 'Password123'
    })
    assert response.status_code == 201
    
    # Register again with same username
    response = client.post('/api/auth/register', json={
        'username': 'duplicate_user',
        'password': 'Password123'
    })
    assert response.status_code == 400
    assert response.json['error'] == 'Username already exists'


def test_default_aspect_ratio_profile(client):
    # Try to set aspect ratio while unauthorized
    res = client.put('/api/auth/default-aspect-ratio', json={'default_aspect_ratio': '16:9'})
    assert res.status_code == 401
    
    # Register and login
    res = client.post('/api/auth/register', json={
        'username': 'ratio_user',
        'password': 'Password123'
    })
    assert res.status_code == 201
    user_data = json.loads(res.data)['user']
    assert user_data['default_aspect_ratio'] == '16:9' # default
    
    # Try to set an invalid aspect ratio
    res = client.put('/api/auth/default-aspect-ratio', json={'default_aspect_ratio': 'invalid'})
    assert res.status_code == 400
    assert 'Invalid aspect ratio' in json.loads(res.data)['error']
    
    # Update to a valid ratio
    res = client.put('/api/auth/default-aspect-ratio', json={'default_aspect_ratio': '9:16'})
    assert res.status_code == 200
    assert json.loads(res.data)['default_aspect_ratio'] == '9:16'
    
    # Status check should reflect updated aspect ratio
    res = client.get('/api/auth/status')
    assert res.status_code == 200
    assert json.loads(res.data)['user']['default_aspect_ratio'] == '9:16'
    
    # Log out
    client.post('/api/auth/logout')
    
    # Login check should reflect updated aspect ratio
    res = client.post('/api/auth/login', json={
        'username': 'ratio_user',
        'password': 'Password123'
    })
    assert res.status_code == 200
    assert json.loads(res.data)['user']['default_aspect_ratio'] == '9:16'

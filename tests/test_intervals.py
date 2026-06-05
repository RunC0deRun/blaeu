import os
import pytest
from unittest.mock import patch, MagicMock
from flask import json
import db

@pytest.fixture
def authed(client):
    client.post('/api/auth/register', json={
        'username': 'test_intervals_user',
        'password': 'Password123'
    })
    return client

def test_intervals_status_unauthorized(client):
    resp = client.get('/api/intervals/status')
    assert resp.status_code == 401

def test_intervals_status_disconnected(authed):
    resp = authed.get('/api/intervals/status')
    assert resp.status_code == 200
    assert json.loads(resp.data)['status'] == 'disconnected'

@patch('requests.get')
def test_intervals_connect_success(mock_get, authed):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {'id': '12345', 'name': 'Test Athlete'}
    mock_get.return_value = mock_resp

    resp = authed.post('/api/intervals/connect', json={
        'athlete_id': '12345',
        'api_key': 'test_key_abc'
    })
    assert resp.status_code == 200
    assert json.loads(resp.data)['status'] == 'connected'
    assert json.loads(resp.data)['athlete_id'] == '12345'

    # Check status
    resp = authed.get('/api/intervals/status')
    assert resp.status_code == 200
    status_data = json.loads(resp.data)
    assert status_data['status'] == 'connected'
    assert status_data['athlete_id'] == '12345'

@patch('requests.get')
def test_intervals_connect_failure_401(mock_get, authed):
    mock_resp = MagicMock()
    mock_resp.status_code = 401
    mock_get.return_value = mock_resp

    resp = authed.post('/api/intervals/connect', json={
        'athlete_id': '12345',
        'api_key': 'wrong_key'
    })
    assert resp.status_code == 401
    assert 'Invalid API Key' in json.loads(resp.data)['error']

@patch('requests.get')
def test_intervals_disconnect(mock_get, authed):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {'id': '12345', 'name': 'Test Athlete'}
    mock_get.return_value = mock_resp

    authed.post('/api/intervals/connect', json={
        'athlete_id': '12345',
        'api_key': 'test_key_abc'
    })

    # Disconnect
    resp = authed.post('/api/intervals/disconnect')
    assert resp.status_code == 200
    assert json.loads(resp.data)['status'] == 'disconnected'

    # Verify status
    resp = authed.get('/api/intervals/status')
    assert json.loads(resp.data)['status'] == 'disconnected'

@patch('requests.get')
def test_intervals_activities(mock_get, authed):
    # Set up connection
    mock_resp_validate = MagicMock()
    mock_resp_validate.status_code = 200
    mock_resp_validate.json.return_value = {'id': '12345', 'name': 'Test Athlete'}
    mock_get.return_value = mock_resp_validate

    authed.post('/api/intervals/connect', json={
        'athlete_id': '12345',
        'api_key': 'test_key_abc'
    })

    # Mock activity list
    mock_resp_list = MagicMock()
    mock_resp_list.status_code = 200
    mock_resp_list.json.return_value = [
        {
            'id': 9999,
            'name': 'Afternoon Run',
            'type': 'Run',
            'start_date_local': '2026-06-03T18:00:00',
            'start_date': '2026-06-03T16:00:00Z',
            'distance': 5000,
            'moving_time': 1800
        }
    ]
    mock_get.return_value = mock_resp_list

    resp = authed.get('/api/intervals/activities')
    assert resp.status_code == 200
    data = json.loads(resp.data)
    assert data['status'] == 'success'
    assert len(data['activities']) == 1
    assert data['activities'][0]['activityId'] == 9999
    assert data['activities'][0]['activityName'] == 'Afternoon Run'

@patch('requests.get')
def test_intervals_import(mock_get, authed):
    # Set up connection
    mock_resp_validate = MagicMock()
    mock_resp_validate.status_code = 200
    mock_resp_validate.json.return_value = {'id': '12345', 'name': 'Test Athlete'}
    mock_get.return_value = mock_resp_validate

    authed.post('/api/intervals/connect', json={
        'athlete_id': '12345',
        'api_key': 'test_key_abc'
    })

    # Mock streams API response
    mock_resp_streams = MagicMock()
    mock_resp_streams.status_code = 200
    mock_resp_streams.json.return_value = [
        {
            'type': 'time',
            'data': [0, 10]
        },
        {
            'type': 'latlng',
            'data': [52.5200, 52.5210],
            'data2': [13.4050, 13.4060]
        },
        {
            'type': 'altitude',
            'data': [34.0, 36.0]
        }
    ]
    mock_get.return_value = mock_resp_streams

    resp = authed.post('/api/intervals/import', json={
        'activityId': '9999',
        'activityName': 'Imported Run',
        'startTimeLocal': '2026-06-03T18:00:00Z'
    })
    assert resp.status_code == 200
    assert json.loads(resp.data)['status'] == 'success'
    assert json.loads(resp.data)['name'] == 'Imported Run'

    # Verify route exists in DB
    routes_resp = authed.get('/api/routes')
    routes = json.loads(routes_resp.data)
    assert len(routes) == 1
    assert routes[0]['name'] == 'Imported Run'

from unittest.mock import MagicMock, patch

import pytest

import db


@pytest.fixture
def user_id(authed_client):
    return db.get_user_by_username('test_user')['id']


def _prefs(client):
    resp = client.get('/api/auth/data-preferences')
    assert resp.status_code == 200
    return resp.get_json()


def _put(client, payload):
    return client.put('/api/auth/data-preferences', json=payload)


def test_requires_login(client):
    assert client.get('/api/auth/data-preferences').status_code == 401
    assert client.put('/api/auth/data-preferences', json={}).status_code == 401


def test_no_prompt_without_intervals_connection(authed_client, user_id):
    prefs = _prefs(authed_client)
    assert prefs['intervals_connected'] is False
    assert prefs['wellness_opt_in'] is None
    assert prefs['prompt'] == {'show': False, 'ask_wellness': False, 'offer_history': False}


def test_existing_connection_gets_history_offer_and_wellness_question(authed_client, user_id):
    # A connection made before the backfill existed: no job yet
    db.save_intervals_connection(user_id, 'a1', 'key')
    prefs = _prefs(authed_client)
    assert prefs['prompt'] == {'show': True, 'ask_wellness': True, 'offer_history': True}
    assert prefs['history'] is None
    assert prefs['history_default_months'] == 12


def test_answering_prompt_starts_history_and_saves_wellness(authed_client, user_id):
    db.save_intervals_connection(user_id, 'a1', 'key')
    resp = _put(authed_client, {
        'prompt_answered': True, 'import_history': True, 'history_months': 6, 'wellness_opt_in': True,
    })
    assert resp.status_code == 200
    prefs = resp.get_json()
    assert prefs['wellness_opt_in'] is True
    assert prefs['history']['status'] == 'pending'
    assert prefs['history']['windows_total'] == 7  # 6 months in 30-day windows
    assert prefs['prompt']['show'] is False
    assert authed_client.get('/api/auth/status').get_json()['user']['wellness_opt_in'] == 1


def test_declining_both_hides_prompt_without_starting_import(authed_client, user_id):
    db.save_intervals_connection(user_id, 'a1', 'key')
    prefs = _put(authed_client, {'prompt_answered': True, 'import_history': False, 'wellness_opt_in': False}).get_json()
    assert prefs['wellness_opt_in'] is False
    assert prefs['history'] is None
    assert prefs['prompt']['show'] is False


def test_ask_me_later_keeps_prompt(authed_client, user_id):
    db.save_intervals_connection(user_id, 'a1', 'key')
    # "Ask me later" saves nothing, so the next login asks again
    assert _prefs(authed_client)['prompt']['show'] is True
    assert _prefs(authed_client)['prompt']['show'] is True


def test_new_connection_only_asks_about_wellness(authed_client, user_id):
    with patch('requests.get', return_value=MagicMock(status_code=200, json=MagicMock(return_value={'id': 'a1'}))):
        authed_client.post('/api/intervals/connect', json={'athlete_id': 'a1', 'api_key': 'key'})
    prefs = _prefs(authed_client)
    assert prefs['history']['status'] == 'pending'  # started by connecting
    assert prefs['prompt'] == {'show': True, 'ask_wellness': True, 'offer_history': False}


def test_opting_in_later_fetches_wellness_history(authed_client, user_id):
    from blueprints.intervals_backfill import start_backfill
    db.save_intervals_connection(user_id, 'a1', 'key')
    first = start_backfill(user_id)
    db.update_sync_job(first['id'], status='completed')
    _put(authed_client, {'wellness_opt_in': False})

    prefs = _put(authed_client, {'wellness_opt_in': True}).get_json()
    assert prefs['history']['id'] != first['id']
    assert prefs['history']['status'] == 'pending'


def test_opting_in_again_does_not_restart_history(authed_client, user_id):
    db.save_intervals_connection(user_id, 'a1', 'key')
    first = _put(authed_client, {'wellness_opt_in': True}).get_json()['history']
    db.update_sync_job(first['id'], status='completed')
    again = _put(authed_client, {'wellness_opt_in': True}).get_json()['history']
    assert again['id'] == first['id']


def test_opting_out_keeps_history_job(authed_client, user_id):
    db.save_intervals_connection(user_id, 'a1', 'key')
    _put(authed_client, {'wellness_opt_in': True})
    prefs = _put(authed_client, {'wellness_opt_in': False}).get_json()
    assert prefs['wellness_opt_in'] is False
    assert db.get_user_wellness_opt_in(user_id) == 0


@pytest.mark.parametrize('payload', [
    {'wellness_opt_in': 'yes'},
    {'import_history': 1},
    {'history_months': 0},
    {'history_months': 25},
    {'history_months': '12'},
])
def test_invalid_payloads_are_rejected(authed_client, user_id, payload):
    assert _put(authed_client, payload).status_code == 400

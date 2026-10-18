import json
from datetime import date
from unittest.mock import MagicMock, patch

import pytest
import requests

import db
from coach import llm, settings as coach_settings
from coach.context import build_messages, build_system_prompt, ContextTooSmall, estimate_tokens

TODAY = date(2026, 10, 18)


@pytest.fixture
def admin_id(authed_client):
    return db.get_user_by_username('test_user')['id']


@pytest.fixture
def configured(admin_id, monkeypatch):
    monkeypatch.delenv('BLAEU_LLM_API_KEY', raising=False)
    coach_settings.save_llm_settings({
        'enabled': True, 'base_url': 'http://ollama:11434/v1', 'model': 'qwen3:8b', 'context_window': 8192,
    })
    return coach_settings.get_llm_config()


# --- Settings ------------------------------------------------------------------------

@pytest.mark.parametrize('url,external', [
    ('http://localhost:11434/v1', False),
    ('http://127.0.0.1:11434', False),
    ('http://192.168.1.20:11434/v1', False),
    ('http://10.0.0.5/v1', False),
    ('http://host.docker.internal:11434/v1', False),
    ('http://ollama:11434', False),
    ('http://gpu-box.lan:8000/v1', False),
    ('https://api.openai.com/v1', True),
    ('https://openrouter.ai/api/v1', True),
    ('http://8.8.8.8/v1', True),
])
def test_is_external_endpoint(url, external):
    assert coach_settings.is_external_endpoint(url) is external


def test_settings_validation(admin_id):
    for bad in ({'base_url': 'ftp://x'}, {'base_url': 'not a url'}, {'context_window': 100},
                {'context_window': '8192'}, {'temperature': 3}, {'enabled': 'yes'}):
        with pytest.raises(coach_settings.SettingsError):
            coach_settings.save_llm_settings(bad)


def test_api_key_is_kept_cleared_and_never_public(admin_id, monkeypatch):
    monkeypatch.delenv('BLAEU_LLM_API_KEY', raising=False)
    coach_settings.save_llm_settings({'api_key': 'sk-secret'})
    coach_settings.save_llm_settings({'api_key': '', 'model': 'm'})  # empty keeps the key
    assert coach_settings.get_llm_config()['api_key'] == 'sk-secret'
    public = coach_settings.public_llm_config()
    assert 'api_key' not in public and public['api_key_set'] is True
    coach_settings.save_llm_settings({'clear_api_key': True})
    assert coach_settings.get_llm_config()['api_key'] == ''


def test_env_api_key_wins(admin_id, monkeypatch):
    coach_settings.save_llm_settings({'api_key': 'stored'})
    monkeypatch.setenv('BLAEU_LLM_API_KEY', 'from-env')
    config = coach_settings.get_llm_config()
    assert config['api_key'] == 'from-env' and config['api_key_from_env'] is True


def test_env_defaults(admin_id, monkeypatch):
    monkeypatch.setenv('BLAEU_LLM_BASE_URL', 'http://ollama:11434/v1/')
    monkeypatch.setenv('BLAEU_LLM_MODEL', 'llama3.1')
    monkeypatch.setenv('BLAEU_LLM_ENABLED', 'true')
    config = coach_settings.get_llm_config()
    assert config['base_url'] == 'http://ollama:11434/v1'
    assert config['model'] == 'llama3.1'
    assert coach_settings.is_configured(config)


# --- LLM client ----------------------------------------------------------------------

def _stream_response(lines, status=200, body=None):
    resp = MagicMock()
    resp.status_code = status
    resp.iter_lines.return_value = iter(lines)
    resp.json.return_value = body or {}
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


CONFIG = {
    'base_url': 'http://llm:8000/v1', 'model': 'm', 'api_key': 'k', 'context_window': 4096,
    'temperature': 0.4, 'max_answer_tokens': 512,
}


def test_think_splitter_handles_tags_split_across_chunks():
    splitter = llm.ThinkSplitter()
    events = []
    for chunk in ['<thi', 'nk>plan', 'ning</th', 'ink>Hello', ' <', 'b>world']:
        events += splitter.feed(chunk)
    events += splitter.flush()
    reasoning = ''.join(t for k, t in events if k == 'reasoning')
    content = ''.join(t for k, t in events if k == 'content')
    assert reasoning == 'planning'
    assert content == 'Hello <b>world'


def test_openai_stream_parsing():
    lines = [
        'data: ' + json.dumps({'choices': [{'delta': {'reasoning_content': 'hmm'}}]}),
        '',
        'data: ' + json.dumps({'choices': [{'delta': {'content': 'Hi'}}]}),
        'data: ' + json.dumps({'choices': [{'delta': {'content': ' there'}}]}),
        'data: [DONE]',
    ]
    with patch('requests.request', return_value=_stream_response(lines)) as req:
        events = list(llm.stream_chat(CONFIG, [{'role': 'user', 'content': 'x'}], is_ollama=False))
    assert events == [('reasoning', 'hmm'), ('content', 'Hi'), ('content', ' there')]
    args, kwargs = req.call_args
    assert args[1] == 'http://llm:8000/v1/chat/completions'
    assert kwargs['headers']['Authorization'] == 'Bearer k'
    assert kwargs['json']['stream'] is True and kwargs['json']['max_tokens'] == 512


def test_ollama_native_stream_sets_context_size():
    lines = [
        json.dumps({'message': {'thinking': 'let me see'}, 'done': False}),
        json.dumps({'message': {'content': 'Rest today.'}, 'done': False}),
        json.dumps({'message': {'content': ''}, 'done': True}),
    ]
    config = dict(CONFIG, base_url='http://ollama:11434/v1')
    with patch('requests.request', return_value=_stream_response(lines)) as req:
        events = list(llm.stream_chat(config, [{'role': 'user', 'content': 'x'}], is_ollama=True))
    assert events == [('reasoning', 'let me see'), ('content', 'Rest today.')]
    args, kwargs = req.call_args
    assert args[1] == 'http://ollama:11434/api/chat'
    assert kwargs['json']['options']['num_ctx'] == 4096


@pytest.mark.parametrize('status,fragment', [
    (401, 'API key'), (404, 'not found'), (429, 'rate limiting'), (500, 'HTTP 500'),
])
def test_http_errors_become_readable(status, fragment):
    with patch('requests.request', return_value=_stream_response([], status=status, body={'error': 'boom'})):
        with pytest.raises(llm.LLMError, match=fragment):
            list(llm.stream_chat(CONFIG, [], is_ollama=False))


def test_unreachable_endpoint():
    with patch('requests.request', side_effect=requests.exceptions.ConnectionError()):
        with pytest.raises(llm.LLMError, match='Cannot reach'):
            list(llm.stream_chat(CONFIG, [], is_ollama=False))


def test_error_inside_stream():
    lines = ['data: ' + json.dumps({'error': {'message': 'model overloaded'}})]
    with patch('requests.request', return_value=_stream_response(lines)):
        with pytest.raises(llm.LLMError, match='overloaded'):
            list(llm.stream_chat(CONFIG, [], is_ollama=False))


def test_connection_test_reports_models_and_reply():
    def fake_request(method, url, **kwargs):
        if url.endswith('/models'):
            resp = MagicMock(status_code=200)
            resp.json.return_value = {'data': [{'id': 'm'}, {'id': 'other'}]}
            return resp
        return _stream_response(['data: ' + json.dumps({'choices': [{'delta': {'content': 'OK'}}]}), 'data: [DONE]'])

    with patch('coach.llm.detect_ollama', return_value=False), patch('requests.request', side_effect=fake_request):
        result = llm.test_connection(CONFIG)
    assert result['ok'] and result['reply'] == 'OK'
    assert result['models'] == ['m', 'other'] and result['model_found'] is True


def test_connection_test_reports_failure():
    with patch('coach.llm.detect_ollama', return_value=False), \
            patch('requests.request', side_effect=requests.exceptions.ConnectionError()):
        result = llm.test_connection(CONFIG)
    assert not result['ok'] and 'Cannot reach' in result['error']


# --- Training context ----------------------------------------------------------------

def _seed_training(user_id):
    db.save_coach_profile(user_id, {'goals': 'Sub 1:45 half marathon', 'injuries': 'Sore left calf'})
    db.upsert_athlete_thresholds(user_id, 'Run', 'intervals', {'lthr': 172, 'max_hr': 191, 'threshold_pace': 4.0})
    db.upsert_daily_wellness(user_id, '2026-10-17', 'intervals',
                             {'ctl': 52.0, 'atl': 58.0, 'ramp_rate': 1.2, 'resting_hr': 44, 'hrv': 71})
    db.upsert_activity(user_id, 'intervals', 'i1', {
        'name': 'Tempo run', 'sport_type': 'Run', 'start_time': '2026-10-16 06:00:00',
        'start_time_local': '2026-10-16 08:00:00', 'moving_time': 3000, 'distance': 10000,
        'avg_hr': 155, 'training_load': 70, 'intensity': 0.88,
    })
    db.upsert_activity(user_id, 'intervals', 'i2', {
        'name': 'Gym', 'sport_type': 'WeightTraining', 'start_time': '2026-10-14 17:00:00', 'moving_time': 2400,
    })


def test_system_prompt_contains_training_data(admin_id):
    _seed_training(admin_id)
    prompt = build_system_prompt(admin_id, TODAY)
    assert 'Today is Sunday 2026-10-18' in prompt
    assert 'Sub 1:45 half marathon' in prompt and 'Sore left calf' in prompt
    assert 'threshold HR 172 bpm' in prompt and 'threshold pace 4:10 /km' in prompt
    assert 'fitness (CTL) 52, fatigue (ATL) 58, form -6' in prompt
    assert 'Tempo run' in prompt and 'WeightTraining' in prompt
    assert '1x Run, 1x WeightTraining' in prompt
    # Wellness body data only with the opt-in
    assert 'HRV' not in prompt.split('## Fitness')[1].split('## Training')[0]
    db.update_user_wellness_opt_in(admin_id, True)
    assert '## Wellness' in build_system_prompt(admin_id, TODAY)


def test_empty_profile_is_explained(admin_id):
    prompt = build_system_prompt(admin_id, TODAY)
    assert "hasn't filled in their coach profile" in prompt
    assert 'No workouts in the last 14 days.' in prompt


def test_build_messages_keeps_recent_history_within_budget(admin_id):
    _seed_training(admin_id)
    config = {'context_window': 4096, 'max_answer_tokens': 512}
    history = []
    for i in range(40):
        history.append({'role': 'user', 'content': f'Question {i} ' + 'x' * 400})
        history.append({'role': 'assistant', 'content': f'Answer {i} ' + 'y' * 400})
    history.append({'role': 'user', 'content': 'Latest question'})
    messages = build_messages(admin_id, history, config, TODAY)
    total = sum(estimate_tokens(m['content']) for m in messages)
    assert total <= 4096 - 512
    assert messages[0]['role'] == 'system'
    assert messages[1]['role'] == 'user'  # never starts with a cut-off answer
    assert messages[-1]['content'] == 'Latest question'
    assert len(messages) < len(history) + 1


def test_build_messages_reduces_detail_for_small_windows(admin_id):
    _seed_training(admin_id)
    full = build_system_prompt(admin_id, TODAY, level=0)
    messages = build_messages(admin_id, [{'role': 'user', 'content': 'Hi'}],
                              {'context_window': 2048, 'max_answer_tokens': 1024}, TODAY)
    assert len(messages[0]['content']) < len(full)
    assert 'Sub 1:45 half marathon' in messages[0]['content']  # the profile always stays


def test_build_messages_rejects_message_that_cannot_fit(admin_id):
    with pytest.raises(ContextTooSmall):
        build_messages(admin_id, [{'role': 'user', 'content': 'x' * 20000}],
                       {'context_window': 2048, 'max_answer_tokens': 1024}, TODAY)


# --- API -----------------------------------------------------------------------------

def _register_second_user(client):
    client.post('/api/auth/logout')
    client.post('/api/auth/register', json={'username': 'second_user', 'password': 'Password123'})
    return db.get_user_by_username('second_user')['id']


def test_admin_llm_endpoints(authed_client, admin_id, monkeypatch):
    monkeypatch.delenv('BLAEU_LLM_API_KEY', raising=False)
    resp = authed_client.put('/api/admin/llm', json={
        'enabled': True, 'base_url': 'https://api.example.com/v1', 'model': 'gpt', 'api_key': 'sk-1',
    })
    assert resp.status_code == 200
    data = resp.get_json()
    assert 'api_key' not in data and data['api_key_set'] is True and data['is_external'] is True
    assert 'sk-1' not in json.dumps(authed_client.get('/api/admin/llm').get_json())
    assert authed_client.put('/api/admin/llm', json={'base_url': 'nope'}).status_code == 400


def test_admin_llm_test_uses_unsaved_form_values(authed_client, admin_id):
    with patch('coach.llm.test_connection', return_value={'ok': True}) as tester:
        resp = authed_client.post('/api/admin/llm/test', json={'base_url': 'http://ollama:11434/v1', 'model': 'x'})
    assert resp.status_code == 200
    tested = tester.call_args[0][0]
    assert tested['base_url'] == 'http://ollama:11434/v1' and tested['model'] == 'x'
    assert coach_settings.get_llm_config()['base_url'] == ''  # nothing saved


def test_admin_endpoints_require_admin(authed_client, admin_id):
    _register_second_user(authed_client)
    assert authed_client.get('/api/admin/llm').status_code == 403
    assert authed_client.put('/api/admin/llm', json={}).status_code == 403
    assert authed_client.post('/api/admin/llm/test', json={}).status_code == 403


def test_coach_endpoints_require_login(client):
    for method, url in (('get', '/api/coach/status'), ('get', '/api/coach/profile'),
                        ('get', '/api/coach/conversations'), ('post', '/api/coach/conversations')):
        assert getattr(client, method)(url).status_code == 401


def test_coach_status(authed_client, admin_id):
    assert authed_client.get('/api/coach/status').get_json()['available'] is False
    coach_settings.save_llm_settings({'enabled': True, 'base_url': 'http://ollama:11434/v1', 'model': 'm'})
    status = authed_client.get('/api/coach/status').get_json()
    assert status == {'available': True, 'model': 'm', 'is_external': False}


def test_profile_roundtrip_and_validation(authed_client, admin_id):
    resp = authed_client.put('/api/coach/profile', json={'goals': '  Marathon  ', 'notes': ''})
    assert resp.status_code == 200
    assert resp.get_json()['goals'] == 'Marathon' and resp.get_json()['notes'] is None
    assert authed_client.put('/api/coach/profile', json={'goals': 5}).status_code == 400
    assert authed_client.put('/api/coach/profile', json={'goals': 'x' * 5000}).status_code == 400


def _sse_events(resp):
    events = []
    for block in resp.get_data(as_text=True).strip().split('\n\n'):
        lines = dict(line.split(': ', 1) for line in block.split('\n'))
        events.append((lines['event'], json.loads(lines['data'])))
    return events


def test_chat_streams_and_stores_answer(authed_client, configured):
    conversation = authed_client.post('/api/coach/conversations').get_json()

    def fake_stream(config, messages, **kwargs):
        assert messages[0]['role'] == 'system' and messages[-1]['content'] == 'How was my week?'
        yield ('reasoning', 'Checking load')
        yield ('content', 'A solid ')
        yield ('content', 'week.')

    # The response streams lazily: read it while the fake LLM is still patched in
    with patch('coach.llm.stream_chat', side_effect=fake_stream):
        resp = authed_client.post(f"/api/coach/conversations/{conversation['id']}/messages",
                                  json={'content': 'How was my week?'})
        assert resp.mimetype == 'text/event-stream'
        events = _sse_events(resp)
    assert [e for e, _ in events] == ['start', 'reasoning', 'content', 'content', 'done']
    assert events[-1][1]['status'] == 'complete'

    detail = authed_client.get(f"/api/coach/conversations/{conversation['id']}").get_json()
    assert detail['title'] == 'How was my week?'
    assert [(m['role'], m['content']) for m in detail['messages']] == [
        ('user', 'How was my week?'), ('assistant', 'A solid week.')]
    assert detail['messages'][1]['reasoning'] == 'Checking load'


def test_chat_error_keeps_question_for_retry(authed_client, configured):
    conversation = authed_client.post('/api/coach/conversations').get_json()
    url = f"/api/coach/conversations/{conversation['id']}/messages"
    with patch('coach.llm.stream_chat', side_effect=llm.LLMError('Cannot reach the LLM endpoint at x.')):
        events = _sse_events(authed_client.post(url, json={'content': 'Plan my week'}))
    assert ('error', {'message': 'Cannot reach the LLM endpoint at x.'}) in events
    assert events[-1][1]['status'] == 'error'
    messages = authed_client.get(f"/api/coach/conversations/{conversation['id']}").get_json()['messages']
    assert [m['role'] for m in messages] == ['user']

    with patch('coach.llm.stream_chat', return_value=iter([('content', 'Here is a plan.')])):
        events = _sse_events(authed_client.post(url, json={'retry': True}))
    assert events[-1][1]['status'] == 'complete'
    messages = authed_client.get(f"/api/coach/conversations/{conversation['id']}").get_json()['messages']
    assert [m['role'] for m in messages] == ['user', 'assistant']  # the question wasn't duplicated
    assert authed_client.post(url, json={'retry': True}).status_code == 400  # nothing left to retry


def test_partial_answer_is_kept_on_error(authed_client, configured):
    conversation = authed_client.post('/api/coach/conversations').get_json()

    def failing(config, messages, **kwargs):
        yield ('content', 'Start of an answer')
        raise llm.LLMError('The connection to the LLM endpoint was interrupted.')

    with patch('coach.llm.stream_chat', side_effect=failing):
        _sse_events(authed_client.post(f"/api/coach/conversations/{conversation['id']}/messages",
                                       json={'content': 'Hi'}))
    messages = authed_client.get(f"/api/coach/conversations/{conversation['id']}").get_json()['messages']
    assert messages[-1]['content'] == 'Start of an answer'
    assert messages[-1]['status'] == 'interrupted'


def test_chat_requires_configuration_and_valid_input(authed_client, admin_id):
    conversation = authed_client.post('/api/coach/conversations').get_json()
    url = f"/api/coach/conversations/{conversation['id']}/messages"
    assert authed_client.post(url, json={'content': 'Hi'}).status_code == 503
    coach_settings.save_llm_settings({'enabled': True, 'base_url': 'http://ollama:11434/v1', 'model': 'm'})
    assert authed_client.post(url, json={'content': '   '}).status_code == 400
    assert authed_client.post(url, json={'content': 'x' * 9000}).status_code == 400


def test_conversations_are_private(authed_client, configured):
    conversation = authed_client.post('/api/coach/conversations').get_json()
    _register_second_user(authed_client)
    assert authed_client.get('/api/coach/conversations').get_json() == []
    cid = conversation['id']
    assert authed_client.get(f'/api/coach/conversations/{cid}').status_code == 404
    assert authed_client.delete(f'/api/coach/conversations/{cid}').status_code == 404
    assert authed_client.post(f'/api/coach/conversations/{cid}/messages', json={'content': 'Hi'}).status_code == 404


def test_delete_conversation_removes_messages(authed_client, configured):
    conversation = authed_client.post('/api/coach/conversations').get_json()
    with patch('coach.llm.stream_chat', return_value=iter([('content', 'Hello')])):
        _sse_events(authed_client.post(f"/api/coach/conversations/{conversation['id']}/messages",
                                       json={'content': 'Hi'}))
    assert authed_client.delete(f"/api/coach/conversations/{conversation['id']}").status_code == 200
    assert db.get_chat_messages(conversation['id']) == []

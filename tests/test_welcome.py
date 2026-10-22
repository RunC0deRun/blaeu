import time
from datetime import date, timedelta
from unittest.mock import patch

import pytest

import db
from blueprints.welcome import compare_with_similar
from coach import daily_note, llm, settings as coach_settings

TODAY = date.today()


def _day(offset):
    return (TODAY - timedelta(days=offset)).isoformat()


def _run(name, days_ago, distance, moving_time, avg_hr, **extra):
    return {
        'name': name, 'sport_type': 'Run', 'start_time': f'{_day(days_ago)} 07:00:00',
        'start_time_local': f'{_day(days_ago)} 09:00:00', 'timezone': 'Europe/Amsterdam',
        'duration': moving_time + 60, 'moving_time': moving_time, 'distance': distance,
        'avg_hr': avg_hr, 'training_load': 50, **extra,
    }


@pytest.fixture
def user_id(authed_client):
    return db.get_user_by_username('test_user')['id']


@pytest.fixture
def configured(user_id, monkeypatch):
    monkeypatch.delenv('BLAEU_LLM_API_KEY', raising=False)
    coach_settings.save_llm_settings({
        'enabled': True, 'base_url': 'http://ollama:11434/v1', 'model': 'qwen3:8b', 'context_window': 8192,
    })
    return coach_settings.get_llm_config()


def test_welcome_requires_login(client):
    assert client.get('/api/welcome').status_code == 401
    assert client.get('/api/coach/daily-note').status_code == 401


def test_welcome_empty_log(authed_client):
    data = authed_client.get('/api/welcome').get_json()
    assert data['latest'] is None
    assert data['recent'] == [] and data['comparison'] is None and data['fitness'] is None
    assert data['last_seven_days']['sessions'] == 0


def test_welcome_latest_and_comparison(authed_client, user_id):
    # Four earlier 10 km runs at 5:00/km and 150 bpm, then a faster one at a lower heart rate
    for i in range(4):
        db.upsert_activity(user_id, 'intervals', f'old{i}', _run(f'Run {i}', 10 + i, 10000, 3000, 150))
    db.upsert_activity(user_id, 'intervals', 'far', _run('Long run', 9, 25000, 7500, 145))
    db.upsert_activity(user_id, 'intervals', 'new', _run(
        'Tempo run', 1, 10000, 2900, 147, hr_zone_times=[60, 1200, 1500, 140], aerobic_te=3.4))

    data = authed_client.get(f'/api/welcome?date={TODAY.isoformat()}').get_json()
    latest = data['latest']
    assert latest['name'] == 'Tempo run'
    assert latest['hr_zone_times'] == [60, 1200, 1500, 140]
    assert latest['aerobic_te'] == 3.4
    assert 'raw_summary' not in latest
    assert [a['name'] for a in data['recent']] == ['Long run', 'Run 0', 'Run 1', 'Run 2', 'Run 3']
    # The 25 km run is not similar; 10 s/km faster at 3 bpm lower
    assert data['comparison'] == {'count': 4, 'metric': 'pace', 'diff': -10.0, 'hr_diff': -3}
    assert data['last_seven_days']['sessions'] == 1


def test_comparison_needs_two_similar_workouts():
    latest = _run('Now', 0, 10000, 3000, 150)
    assert compare_with_similar(latest, [_run('One', 1, 10000, 3000, 150)]) is None
    ride = {**latest, 'sport_type': 'Ride', 'distance': 40000, 'moving_time': 4800}
    earlier = [{**ride, 'moving_time': 5000}, {**ride, 'moving_time': 5000, 'avg_hr': None}]
    result = compare_with_similar(ride, earlier)
    assert result['metric'] == 'speed' and result['diff'] == 1.2 and result['hr_diff'] == 0
    assert compare_with_similar({**latest, 'distance': None}, earlier) is None


def test_welcome_wellness_only_with_opt_in(authed_client, user_id):
    for i in range(10):
        db.upsert_daily_wellness(user_id, _day(i), 'intervals', {
            'ctl': 50 - i, 'atl': 60 - i, 'ramp_rate': 2.5, 'hrv': 60 + i % 3, 'resting_hr': 46,
        })
    data = authed_client.get('/api/welcome').get_json()
    assert data['fitness']['ctl'] == 50 and data['fitness']['form'] == -10
    assert [p['value'] for p in data['fitness']['ctl_series']][-1] == 50
    assert data['recovery'] is None

    db.update_user_wellness_opt_in(user_id, True)
    recovery = authed_client.get('/api/welcome').get_json()['recovery']
    assert recovery['hrv']['value'] == 60
    assert recovery['resting_hr']['avg7'] == 46
    assert len(recovery['hrv']['series']) == 10


def test_welcome_events(authed_client, user_id):
    conversation_id = db.create_conversation(user_id, 'Taper plan')
    db.add_chat_message(conversation_id, 'user', 'Hi')
    db.create_sync_job(user_id, 'intervals', 'backfill', _day(365), _day(0), 12)
    events = authed_client.get('/api/welcome').get_json()['events']
    titles = {e['title']: e for e in events}
    assert titles['Taper plan']['detail'] == 'Coach conversation, 1 messages'
    assert titles['Training history import']['detail'] == 'waiting to start, 0 of 12 steps'


def test_parse_day():
    utc_today = daily_note._now().date()
    assert daily_note.parse_day(None) == utc_today
    assert daily_note.parse_day((utc_today - timedelta(days=1)).isoformat()) == utc_today - timedelta(days=1)
    assert daily_note.parse_day('nonsense') == utc_today
    assert daily_note.parse_day('2001-01-01') == utc_today


# --- Daily note ----------------------------------------------------------------------

def _wait_for_note(client, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        note = client.get(f'/api/coach/daily-note?date={TODAY.isoformat()}').get_json()
        if note['status'] != 'generating':
            return note
        time.sleep(0.05)
    raise AssertionError('The note was still generating')


def test_note_unavailable_without_coach(authed_client):
    assert authed_client.get('/api/coach/daily-note').get_json() == {'status': 'unavailable'}
    assert authed_client.post('/api/coach/daily-note/refresh', json={}).status_code == 503


def test_note_generated_once_and_cached(authed_client, user_id, configured):
    db.upsert_activity(user_id, 'intervals', 'a1', _run('Easy run', 1, 8000, 2700, 138))
    calls = []

    def fake_stream(config, messages, **kwargs):
        calls.append(messages)
        yield ('reasoning', 'Looking at the data')
        yield ('content', 'Easy day today: 40 minutes in zone 2.')

    with patch('coach.llm.stream_chat', side_effect=fake_stream):
        first = authed_client.get(f'/api/coach/daily-note?date={TODAY.isoformat()}').get_json()
        assert first['status'] in ('generating', 'ready')
        note = _wait_for_note(authed_client)
        assert note['status'] == 'ready'
        assert note['content'] == 'Easy day today: 40 minutes in zone 2.'
        assert note['model'] == 'qwen3:8b'
        _wait_for_note(authed_client)
    assert len(calls) == 1
    assert calls[0][-1]['content'] == daily_note.PROMPT
    assert 'Easy run' in calls[0][0]['content']


def test_note_regenerated_after_new_workout(authed_client, user_id, configured):
    db.upsert_activity(user_id, 'intervals', 'a1', _run('Easy run', 2, 8000, 2700, 138))
    answers = iter(['First note.', 'Second note.'])
    with patch('coach.llm.stream_chat', side_effect=lambda *a, **k: iter([('content', next(answers))])):
        assert _wait_for_note(authed_client)['content'] == 'First note.'
        db.upsert_activity(user_id, 'intervals', 'a2', _run('Intervals', 1, 9000, 2700, 160))
        authed_client.get(f'/api/coach/daily-note?date={TODAY.isoformat()}')
        assert _wait_for_note(authed_client)['content'] == 'Second note.'


def test_note_failure_is_reported_and_not_retried(authed_client, configured):
    with patch('coach.llm.stream_chat', side_effect=llm.LLMError('Cannot reach the LLM endpoint at x.')) as chat:
        note = _wait_for_note(authed_client)
        assert note['status'] == 'failed' and note['error'] == 'Cannot reach the LLM endpoint at x.'
        _wait_for_note(authed_client)
        assert chat.call_count == 1

    with patch('coach.llm.stream_chat', return_value=iter([('reasoning', 'hmm'), ('truncated', '')])):
        resp = authed_client.post('/api/coach/daily-note/refresh', json={'date': TODAY.isoformat()})
        assert resp.status_code == 200
        note = _wait_for_note(authed_client)
    assert note['status'] == 'failed' and 'reasoning' in note['error']


def test_note_lease_prevents_parallel_generation(user_id, configured):
    now = daily_note._now()
    lease = daily_note._fmt(now + timedelta(minutes=5))
    assert db.claim_daily_note(user_id, TODAY.isoformat(), None, daily_note._fmt(now), lease)
    assert not db.claim_daily_note(user_id, TODAY.isoformat(), None, daily_note._fmt(now), lease)
    # An abandoned lease can be taken over
    later = daily_note._fmt(now + timedelta(minutes=6))
    assert db.claim_daily_note(user_id, TODAY.isoformat(), None, later, daily_note._fmt(now + timedelta(minutes=20)))


def test_discuss_note_starts_conversation(authed_client, configured):
    assert authed_client.post('/api/coach/daily-note/discuss', json={'date': TODAY.isoformat()}).status_code == 409
    with patch('coach.llm.stream_chat', return_value=iter([('content', 'Rest today.')])):
        _wait_for_note(authed_client)
    resp = authed_client.post('/api/coach/daily-note/discuss', json={'date': TODAY.isoformat()})
    assert resp.status_code == 200
    conversation_id = resp.get_json()['id']
    detail = authed_client.get(f"/api/coach/conversations/{conversation_id}").get_json()
    assert [(m['role'], m['content']) for m in detail['messages']] == [
        ('user', daily_note.DISCUSS_QUESTION), ('assistant', 'Rest today.')]

    # Asking again reopens the same conversation; after deleting it, a new one starts
    again = authed_client.post('/api/coach/daily-note/discuss', json={'date': TODAY.isoformat()})
    assert again.get_json()['id'] == conversation_id
    authed_client.delete(f'/api/coach/conversations/{conversation_id}')
    fresh = authed_client.post('/api/coach/daily-note/discuss', json={'date': TODAY.isoformat()})
    assert fresh.get_json()['id'] != conversation_id


# --- Metric history ------------------------------------------------------------------

def _metric(client, metric, days=42):
    return client.get(f'/api/welcome/metrics/{metric}?days={days}&date={TODAY.isoformat()}')


def test_metric_history_validation(client, authed_client):
    assert _metric(authed_client, 'nonsense').status_code == 404
    assert _metric(authed_client, 'form', days=30).status_code == 400
    assert _metric(authed_client, 'hrv').status_code == 403  # wellness needs opt-in
    authed_client.post('/api/auth/logout')
    assert _metric(authed_client, 'form').status_code == 401


def test_metric_history_fitness_and_form(authed_client, user_id):
    for i in range(50):
        db.upsert_daily_wellness(user_id, _day(i), 'intervals', {'ctl': 60 - i * 0.2, 'atl': 55 + (i % 7)})
    data = _metric(authed_client, 'fitness').get_json()
    assert data['start'] == _day(41) and data['end'] == _day(0)
    points = data['points']
    assert len(points) == 42 and points[0]['date'] == _day(41) and points[-1]['date'] == _day(0)
    assert points[-1]['ctl'] == 60 and points[-1]['atl'] == 55 and points[-1]['value'] == 5
    assert _metric(authed_client, 'form').get_json()['points'] == points


def test_metric_history_wellness_rolling_average(authed_client, user_id):
    db.update_user_wellness_opt_in(user_id, True)
    # HRV on alternate days only; the average covers the readings of the last 7 days
    for i in range(0, 60, 2):
        db.upsert_daily_wellness(user_id, _day(i), 'intervals', {'hrv': 60 + i % 4})
    points = _metric(authed_client, 'hrv').get_json()['points']
    assert [p['date'] for p in points] == [_day(i) for i in range(40, -1, -2)]
    # Readings on days 0, 2, 4 and 6: 60, 62, 60, 62
    assert points[-1]['value'] == 60 and points[-1]['rolling'] == 61


def test_metric_history_load(authed_client, user_id):
    db.upsert_activity(user_id, 'intervals', 'a', _run('Tempo', 0, 10000, 2900, 150, training_load=80))
    db.upsert_activity(user_id, 'intervals', 'b', _run('Easy', 0, 6000, 2100, 130, training_load=30))
    db.upsert_activity(user_id, 'intervals', 'c', _run('Long', 6, 20000, 6600, 145, training_load=120))
    db.upsert_activity(user_id, 'intervals', 'd', _run('Old', 7, 8000, 2700, 140, training_load=40))
    points = _metric(authed_client, 'load').get_json()['points']
    assert len(points) == 42 and points[-1]['date'] == _day(0)
    today = points[-1]
    assert today['value'] == 110
    assert sorted(w['name'] for w in today['workouts']) == ['Easy', 'Tempo']
    # The rolling total counts the last 7 days: today and day 6, not day 7
    assert today['rolling'] == 230
    assert points[-2]['value'] == 0 and points[-2]['workouts'] == []

import json
from datetime import date, timedelta

import pytest
from werkzeug.security import generate_password_hash

import db
from coach import agent, data_tools, library, llm
from coach.context import build_messages, build_system_prompt
from tests.test_agent import ScriptedLLM, AGENT_CONFIG

TODAY = date(2026, 10, 18)

RUN_RAW = {
    'icu_hr_zones': [147, 156, 165, 174, 179, 184, 193],
    'icu_hr_zone_times': [3016, 85, 0, 0, 0, 0, 0],
    'pace_zones': [77.5, 87.7, 94.3, 100.0, 103.4, 111.5, 999.0],
    'pace_zone_times': [19, 3079, 7, 0, 0, 0, 0],
    'pace': 3.2256181, 'gap': 3.2009485, 'average_stride': 1.1358, 'total_elevation_loss': 20.8,
    'interval_summary': ['10x 5m12s 140bpm'], 'hr_load': 37, 'pace_load': 57,
}


@pytest.fixture
def user_id(authed_client):
    return db.get_user_by_username('test_user')['id']


def add_workout(user_id, source_id, day, sport='Run', minutes=50, km=10, load=60, hr=145, raw=None, zones=None):
    start = f'{day.isoformat()} 07:00:00'
    return db.upsert_activity(user_id, 'intervals', source_id, {
        'name': f'{sport} {source_id}', 'sport_type': sport, 'start_time': start, 'start_time_local': start,
        'moving_time': minutes * 60, 'duration': minutes * 60 + 60, 'distance': km * 1000 if km else None,
        'elevation_gain': 22, 'avg_hr': hr, 'max_hr': hr + 20, 'training_load': load, 'intensity': 0.8,
        'hr_zone_times': zones if zones is not None else [600, 1800, 300, 300, 0, 0, 0],
        'raw_summary': raw or {},
    })


@pytest.fixture
def season(user_id):
    """Ten weeks of training: a run every three days, a strength session weekly."""
    ids = {}
    for i in range(0, 70, 3):
        day = TODAY - timedelta(days=i)
        ids[day] = add_workout(user_id, f'r{i}', day, raw=RUN_RAW if i == 0 else None)
    for i in range(1, 70, 7):
        add_workout(user_id, f's{i}', TODAY - timedelta(days=i), sport='WeightTraining', minutes=30, km=0,
                    load=5, hr=90, zones=[1800, 0, 0, 0, 0, 0, 0])
    for i in range(60):
        db.upsert_daily_wellness(user_id, (TODAY - timedelta(days=i)).isoformat(), 'intervals', {
            'ctl': 50 - i * 0.1, 'atl': 55 - i * 0.1, 'ramp_rate': 1.0, 'resting_hr': 44, 'hrv': 70 + (i % 5),
            'sleep_secs': 27000,
        })
    return ids


def run(user_id, name, **arguments):
    return data_tools.run_tool(user_id, name, arguments)


# --- The tools -----------------------------------------------------------------------

def test_get_activities_lists_period_with_ids(user_id, season):
    text, summary = run(user_id, 'get_activities', start_date='2026-10-01', end_date='2026-10-18')
    lines = text.split('\n')
    assert lines[0].startswith('id | date')
    days = [line.split(' | ')[1] for line in lines[1:]]
    assert days == sorted(days, reverse=True) and min(days) >= '2026-10-01' and max(days) <= '2026-10-18'
    assert summary == f'{len(lines) - 1} workouts'


def test_get_activities_sport_filter_and_cap(user_id, season, monkeypatch):
    text, _ = run(user_id, 'get_activities', start_date='2026-08-01', end_date='2026-10-18', sport='weighttraining')
    assert 'Run ' not in text and 'WeightTraining' in text
    monkeypatch.setattr(data_tools, 'MAX_ACTIVITIES', 10)
    text, summary = run(user_id, 'get_activities', start_date='2026-01-01', end_date='2026-10-18')
    assert 'Showing the 10 most recent of 34 workouts' in text and summary == '34 workouts'
    assert len(text.split('\n')) == 1 + 10 + 1


def test_invalid_periods_return_errors_the_model_can_fix(user_id, season):
    text, summary = run(user_id, 'get_activities', start_date='last week', end_date='2026-10-18')
    assert text.startswith('Error:') and 'YYYY-MM-DD' in text and summary == 'invalid request'
    text, _ = run(user_id, 'get_activities', start_date='2024-01-01', end_date='2026-10-18')
    assert 'at most 400 days' in text
    # Reversed dates are simply swapped
    text, _ = run(user_id, 'get_activities', start_date='2026-10-18', end_date='2026-10-10')
    assert not text.startswith('Error')


def test_activity_details_formats_zones_pace_and_intervals(user_id, season):
    activity_id = season[TODAY]
    text, summary = run(user_id, 'get_activity_details', activity_id=activity_id)
    assert f'Workout {activity_id}: Run r0 (Run) on 2026-10-18' in text
    assert 'Pace: 5:10 /km' in text and 'Grade adjusted pace: 5:12 /km' in text
    assert 'Time in heart rate zones: Z1 (up to 147 bpm): 50 min, Z2 (147-156 bpm): 1 min' in text
    assert 'Time in pace zones (% of threshold pace): Z1 (up to 77.5 %): 0 min, Z2 (77.5-87.7 %): 51 min' in text
    assert 'Intervals or laps: 10x 5m12s 140bpm' in text
    assert 'heart rate load 37, pace load 57' in text
    assert summary == 'Run r0'


def test_activity_details_only_for_own_visible_workouts(user_id, season):
    other = db.add_user('other_user', generate_password_hash('Password123', method='pbkdf2:sha256'), 0)
    foreign = add_workout(other, 'x1', TODAY)
    text, _ = run(user_id, 'get_activity_details', activity_id=foreign)
    assert text.startswith('Error:') and 'no workout' in text
    hidden = add_workout(user_id, 'h1', TODAY)
    db.set_activity_hidden(hidden, True)
    assert run(user_id, 'get_activity_details', activity_id=hidden)[0].startswith('Error:')
    assert run(user_id, 'get_activity_details', activity_id='abc')[0].startswith('Error:')


def test_training_summary_per_week_and_month(user_id, season):
    text, summary = run(user_id, 'get_training_summary', start_date='2026-09-28', end_date='2026-10-18')
    lines = text.split('\n')
    assert lines[0].startswith('week starting') and summary == '3 weeks'
    assert [line.split(' | ')[0] for line in lines[1:]] == ['2026-09-28', '2026-10-05', '2026-10-12']
    # Runs spend 2700 of 3000 s in Z1-2 terms below Z4: 40 min of 50 in Z1-2 => strength adds Z1 time
    assert all(line.split(' | ')[-1].count('%') == 3 for line in lines[1:])

    text, summary = run(user_id, 'get_training_summary', start_date='2026-08-10', end_date='2026-10-18', period='month')
    assert [line.split(' | ')[0] for line in text.split('\n')[1:]] == ['2026-08-01', '2026-09-01', '2026-10-01']
    assert summary == '3 months'


def test_fitness_trend_samples_long_periods(user_id, season, monkeypatch):
    monkeypatch.setattr(data_tools, 'MAX_DAILY_ROWS', 20)
    text, summary = run(user_id, 'get_fitness_trend', start_date='2026-10-12', end_date='2026-10-18')
    assert summary == '7 days' and '2026-10-18 | 50 | 55 | -5' in text
    text, summary = run(user_id, 'get_fitness_trend', start_date='2026-08-20', end_date='2026-10-18')
    assert summary.endswith('weeks') and 'one row per week' in text


def test_wellness_needs_the_opt_in(user_id, season):
    text, summary = run(user_id, 'get_wellness', start_date='2026-10-01', end_date='2026-10-18')
    assert summary == 'not shared' and "doesn't share" in text
    db.update_user_wellness_opt_in(user_id, True)
    text, summary = run(user_id, 'get_wellness', start_date='2026-10-01', end_date='2026-10-18')
    assert summary == '18 days'
    assert text.startswith('Averages over the period: resting HR 44, HRV 72, sleep 7h30')
    assert data_tools.tool_definitions(False)[-1]['function']['name'] != 'get_wellness'


def test_describe_labels():
    assert data_tools.describe('get_activities', {'start_date': '2026-09-01', 'end_date': '2026-09-30', 'sport': 'Run'}) \
        == 'Looked up Run workouts from 2026-09-01 to 2026-09-30'
    assert data_tools.describe('get_activity_details', {'activity_id': 12}) == 'Looked at workout 12 in detail'
    assert data_tools.describe('get_training_summary', {'period': 'month', 'start_date': 'a', 'end_date': 'b'}) \
        == 'Summarised training per month from a to b'


# --- The hybrid prompt ---------------------------------------------------------------

def test_compact_prompt_has_ids_and_less_history(user_id, season):
    full = build_system_prompt(user_id, TODAY)
    compact = build_system_prompt(user_id, TODAY, compact=True)
    assert 'last 8 weeks' in full and 'last 4 weeks' in compact
    assert len(compact) < len(full)
    recent = compact.split('## Recent workouts, newest first\n')[1].split('\n')
    assert recent[0].startswith('id | date') and recent[1].startswith(f'{season[TODAY]} | 2026-10-18')
    assert all(line.split(' | ')[1] >= '2026-10-11' for line in recent[1:])


def test_build_messages_uses_compact_overview(user_id, season):
    messages = build_messages(user_id, [{'role': 'user', 'content': 'Hi'}],
                              {'context_window': 16384, 'max_answer_tokens': 1024}, TODAY, compact=True)
    assert 'last 4 weeks' in messages[0]['content']


# --- In the agent loop ---------------------------------------------------------------

DATA_CONFIG = dict(AGENT_CONFIG, data_tools='auto')


@pytest.fixture
def tools_supported(monkeypatch):
    monkeypatch.setattr(llm, 'detect_ollama', lambda config, use_cache=True: True)
    monkeypatch.setattr(llm, 'model_supports_tools', lambda config: True)
    monkeypatch.setattr(library, 'has_searchable', lambda user_id, model: False)


def data_call(name, **arguments):
    return ('tool_call', {'id': 'c', 'name': name, 'arguments': arguments})


def test_agent_looks_up_data_and_shows_steps(user_id, season, tools_supported, monkeypatch):
    fake = ScriptedLLM([
        [data_call('get_training_summary', start_date='2026-09-01', end_date='2026-10-18', period='month')],
        [('content', 'October is going well.')],
    ])
    monkeypatch.setattr(llm, 'stream_chat', fake)
    messages = [{'role': 'system', 'content': 'You are a coach.'}, {'role': 'user', 'content': 'Compare months'}]
    events = list(agent.run(user_id, DATA_CONFIG, messages, 'Compare months'))

    assert [k for k, _ in events] == ['step', 'step_result', 'content', 'done']
    assert events[1][1] == {'kind': 'data', 'tool': 'get_training_summary',
                            'label': 'Summarised training per month from 2026-09-01 to 2026-10-18',
                            'summary': '2 months'}
    offered = {t['function']['name'] for t in fake.calls[0]['tools']}
    assert offered == {'get_activities', 'get_activity_details', 'get_training_summary', 'get_fitness_trend'}
    assert 'Looking up training data' in fake.calls[0]['messages'][0]['content']
    result = fake.calls[1]['messages'][-1]
    assert result['role'] == 'tool' and result['content'].startswith('month starting')
    assert events[-1][1]['steps'][0]['tool'] == 'get_training_summary'


def test_agent_refuses_tools_it_did_not_offer(user_id, season, tools_supported, monkeypatch):
    fake = ScriptedLLM([[data_call('get_wellness', start_date='2026-10-01', end_date='2026-10-18')],
                        [('content', 'Answer.')]])
    monkeypatch.setattr(llm, 'stream_chat', fake)
    messages = [{'role': 'system', 'content': 'x'}, {'role': 'user', 'content': 'Sleep?'}]
    events = list(agent.run(user_id, DATA_CONFIG, messages, 'Sleep?'))
    assert 'Unknown tool get_wellness' in fake.calls[1]['messages'][-1]['content']
    assert events[-1][1]['steps'] == []


def test_total_tool_calls_are_capped(user_id, season, tools_supported, monkeypatch):
    rounds = [[data_call('get_fitness_trend', start_date='2026-10-01', end_date='2026-10-18')] for _ in range(10)]
    fake = ScriptedLLM(rounds)
    monkeypatch.setattr(llm, 'stream_chat', fake)
    messages = [{'role': 'system', 'content': 'x'}, {'role': 'user', 'content': 'Trend?'}]
    events = list(agent.run(user_id, DATA_CONFIG, messages, 'Trend?'))
    assert len(events[-1][1]['steps']) == agent.MAX_TOOL_CALLS
    assert fake.calls[-1]['tools'] is None


def test_large_results_are_cut_to_fit(user_id, season):
    messages = [{'role': 'system', 'content': 'x' * 8000}, {'role': 'user', 'content': 'Hi'}]
    text = '\n'.join(f'line {i} ' + 'y' * 80 for i in range(200))
    cut = agent.fit_text(text, messages, dict(DATA_CONFIG, context_window=8192))
    assert 'cut off to fit' in cut and len(cut) < len(text)


def test_plan_follows_setting_and_tool_support(user_id, monkeypatch):
    monkeypatch.setattr(library, 'has_searchable', lambda user_id, model: False)
    monkeypatch.setattr(llm, 'model_supports_tools', lambda config: True)
    assert agent.plan(DATA_CONFIG, user_id)['data_tools'] is True
    assert agent.plan(dict(DATA_CONFIG, data_tools='off'), user_id)['data_tools'] is False
    monkeypatch.setattr(llm, 'model_supports_tools', lambda config: False)
    assert agent.plan(DATA_CONFIG, user_id)['data_tools'] is False


def test_chat_endpoint_uses_compact_prompt_with_data_tools(authed_client, user_id, season, tools_supported, monkeypatch):
    from coach import settings as coach_settings
    from unittest.mock import patch
    from tests.test_coach import _sse_events
    coach_settings.save_llm_settings({'enabled': True, 'base_url': 'http://ollama:11434/v1', 'model': 'm',
                                      'context_window': 16384})
    fake = ScriptedLLM([[data_call('get_activities', start_date='2026-09-01', end_date='2026-09-30')],
                        [('content', 'September had many runs.')]])
    conversation = authed_client.post('/api/coach/conversations').get_json()
    with patch('coach.llm.stream_chat', side_effect=fake):
        events = _sse_events(authed_client.post(f"/api/coach/conversations/{conversation['id']}/messages",
                                                json={'content': 'How was September?'}))
    assert [e for e, _ in events] == ['start', 'step', 'step_result', 'content', 'done']
    assert 'last 4 weeks' in fake.calls[0]['messages'][0]['content']
    stored = authed_client.get(f"/api/coach/conversations/{conversation['id']}").get_json()['messages'][-1]
    assert stored['steps'][0]['label'] == 'Looked up workouts from 2026-09-01 to 2026-09-30'

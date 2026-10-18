import json
from unittest.mock import MagicMock, patch

import pytest

import db
from coach import agent, library, llm
from coach import settings as coach_settings
from tests.test_coach import _stream_response, _sse_events, CONFIG


# --- Tool calls in the LLM client ----------------------------------------------------

def test_ollama_tool_calls_are_parsed():
    lines = [
        json.dumps({'message': {'thinking': 'check the data'}, 'done': False}),
        json.dumps({'message': {'content': '', 'tool_calls': [
            {'function': {'name': 'search_literature', 'arguments': {'query': 'recovery after a race'}}}]},
            'done': False}),
        json.dumps({'message': {'content': ''}, 'done': True, 'done_reason': 'stop'}),
    ]
    with patch('requests.request', return_value=_stream_response(lines)) as req:
        events = list(llm.stream_chat(CONFIG, [], is_ollama=True, tools=[agent.SEARCH_TOOL]))
    assert req.call_args.kwargs['json']['tools'] == [agent.SEARCH_TOOL]
    assert events == [('reasoning', 'check the data'),
                      ('tool_call', {'id': None, 'name': 'search_literature',
                                     'arguments': {'query': 'recovery after a race'}})]


def test_openai_tool_call_fragments_are_assembled():
    def delta(**d):
        return 'data: ' + json.dumps({'choices': [{'delta': d}]})
    lines = [
        delta(tool_calls=[{'index': 0, 'id': 'call_1', 'function': {'name': 'search_literature', 'arguments': ''}}]),
        delta(tool_calls=[{'index': 0, 'function': {'arguments': '{"query": "tap'}}]),
        delta(tool_calls=[{'index': 0, 'function': {'arguments': 'ering"}'}}]),
        'data: ' + json.dumps({'choices': [{'delta': {}, 'finish_reason': 'tool_calls'}]}),
        'data: [DONE]',
    ]
    with patch('requests.request', return_value=_stream_response(lines)):
        events = list(llm.stream_chat(CONFIG, [], is_ollama=False, tools=[agent.SEARCH_TOOL]))
    assert events == [('tool_call', {'id': 'call_1', 'name': 'search_literature', 'arguments': {'query': 'tapering'}})]


def test_tools_are_not_sent_unless_offered():
    with patch('requests.request', return_value=_stream_response(['data: [DONE]'])) as req:
        list(llm.stream_chat(CONFIG, [], is_ollama=False))
    assert 'tools' not in req.call_args.kwargs['json']


def test_tool_messages_match_each_api():
    call = {'id': 'call_1', 'name': 'search_literature', 'arguments': {'query': 'x'}}
    assert llm.assistant_tool_call_message([call], '', True)['tool_calls'] == [
        {'function': {'name': 'search_literature', 'arguments': {'query': 'x'}}}]
    openai = llm.assistant_tool_call_message([call], '', False)['tool_calls'][0]
    assert openai['id'] == 'call_1' and json.loads(openai['function']['arguments']) == {'query': 'x'}
    assert llm.tool_result_message(call, 'r', True) == {'role': 'tool', 'tool_name': 'search_literature', 'content': 'r'}
    assert llm.tool_result_message(call, 'r', False) == {'role': 'tool', 'tool_call_id': 'call_1', 'content': 'r'}


def test_model_tool_support_from_ollama_capabilities():
    llm._tools_cache.clear()
    show = MagicMock(status_code=200)
    show.json.return_value = {'capabilities': ['completion', 'tools']}
    with patch('coach.llm.detect_ollama', return_value=True), patch('requests.post', return_value=show):
        assert llm.model_supports_tools(dict(CONFIG, model='with-tools'))
    show.json.return_value = {'capabilities': ['completion']}
    with patch('coach.llm.detect_ollama', return_value=True), patch('requests.post', return_value=show):
        assert not llm.model_supports_tools(dict(CONFIG, model='without-tools'))
    with patch('coach.llm.detect_ollama', return_value=False):
        assert llm.model_supports_tools(CONFIG)


# --- The agent loop ------------------------------------------------------------------

AGENT_CONFIG = dict(CONFIG, context_window=16384, embedding_model='fake', embedding_base_url='',
                    library_search='auto')


def passage(chunk_id, title='Guide', page=1, text=None):
    return {'chunk_id': chunk_id, 'document_id': 1, 'title': title, 'page': page, 'section': None,
            'text': text or f'Passage {chunk_id} about training.', 'similarity': 0.7, 'score': 0.03}


class ScriptedLLM:
    """Plays back one list of events per round and records what each round was sent."""

    def __init__(self, rounds):
        self.rounds = list(rounds)
        self.calls = []

    def __call__(self, config, messages, max_tokens=None, is_ollama=None, tools=None, **kwargs):
        self.calls.append({'messages': [dict(m) for m in messages], 'tools': tools, 'max_tokens': max_tokens})
        return iter(self.rounds.pop(0) if self.rounds else [('content', 'Final answer.')])


def tool_call(query):
    return ('tool_call', {'id': 'c', 'name': 'search_literature', 'arguments': {'query': query}})


@pytest.fixture
def library_available(monkeypatch):
    monkeypatch.setattr(library, 'has_searchable', lambda user_id, model: True)
    monkeypatch.setattr(llm, 'detect_ollama', lambda config, use_cache=True: True)
    monkeypatch.setattr(llm, 'model_supports_tools', lambda config: True)


def _run(messages, question='Plan tomorrow'):
    events = list(agent.run(1, AGENT_CONFIG, messages, question))
    return events, events[-1][1]


def _messages():
    return [{'role': 'system', 'content': 'You are a coach.'}, {'role': 'user', 'content': 'Plan tomorrow'}]


def test_model_searches_then_answers_with_citations(library_available, monkeypatch):
    searches = []
    monkeypatch.setattr(library, 'search', lambda u, q, c, limit: searches.append(q) or [passage(11), passage(12, page=4)])
    fake = ScriptedLLM([
        [('reasoning', 'Yesterday was a race.'), tool_call('recovery after a race')],
        [('content', 'Take an easy day [1].')],
    ])
    monkeypatch.setattr(llm, 'stream_chat', fake)

    events, done = _run(_messages())
    assert searches == ['recovery after a race']
    kinds = [k for k, _ in events]
    assert kinds == ['reasoning', 'search', 'search_result', 'content', 'done']
    assert events[2][1] == {'query': 'recovery after a race', 'found': 2, 'titles': ['Guide'], 'automatic': False}
    assert [s['n'] for s in done['sources']] == [1, 2]

    # The first round offered the tool and explained it; the second got the passages back
    assert fake.calls[0]['tools'] == [agent.SEARCH_TOOL]
    assert 'search_literature tool' in fake.calls[0]['messages'][0]['content']
    second = fake.calls[1]['messages']
    assert second[-2]['tool_calls'][0]['function']['arguments'] == {'query': 'recovery after a race'}
    assert second[-1]['role'] == 'tool' and second[-1]['content'].startswith('[1] Guide, p. 1')


def test_search_limit_ends_tool_offers(library_available, monkeypatch):
    monkeypatch.setattr(library, 'search', lambda u, q, c, limit: [passage(hash(q) % 1000)])
    fake = ScriptedLLM([[tool_call(f'topic {i}')] for i in range(5)])
    monkeypatch.setattr(llm, 'stream_chat', fake)
    events, done = _run(_messages())
    assert len(done['steps']) == agent.MAX_SEARCHES
    assert fake.calls[-1]['tools'] is None  # the last round must answer


def test_same_passage_keeps_its_number(library_available, monkeypatch):
    results = {'first': [passage(1), passage(2)], 'second': [passage(2), passage(3)]}
    monkeypatch.setattr(library, 'search', lambda u, q, c, limit: results[q])
    fake = ScriptedLLM([[tool_call('first')], [tool_call('second')], [('content', 'See [2] and [3].')]])
    monkeypatch.setattr(llm, 'stream_chat', fake)
    events, done = _run(_messages())
    assert [(s['n'], s['chunk_id']) for s in done['sources']] == [(1, 1), (2, 2), (3, 3)]
    second_result = fake.calls[2]['messages'][-1]['content']
    assert second_result.startswith('[2] ') and '\n[3] ' in second_result


def test_empty_search_tells_the_model(library_available, monkeypatch):
    monkeypatch.setattr(library, 'search', lambda u, q, c, limit: [])
    fake = ScriptedLLM([[tool_call('cycling cadence')], [('content', 'The library has nothing on that.')]])
    monkeypatch.setattr(llm, 'stream_chat', fake)
    events, done = _run(_messages())
    assert 'No relevant passages' in fake.calls[1]['messages'][-1]['content']
    assert done['sources'] == [] and done['steps'][0]['found'] == 0


def test_passages_are_trimmed_to_the_context_window(library_available, monkeypatch):
    long_text = 'word ' * 1500  # about 3,700 estimated tokens per passage
    monkeypatch.setattr(library, 'search', lambda u, q, c, limit: [passage(i, text=long_text) for i in range(4)])
    fake = ScriptedLLM([[tool_call('anything')], [('content', 'Answer.')]])
    monkeypatch.setattr(llm, 'stream_chat', fake)
    events = list(agent.run(1, dict(AGENT_CONFIG, context_window=12000), _messages(), 'Plan tomorrow'))
    done = events[-1][1]
    assert 0 < len(done['sources']) < 4
    # Only passages the model received are numbered and listed
    sent = fake.calls[1]['messages'][-1]['content']
    assert all(f"[{s['n']}] " in sent for s in done['sources'])


def test_automatic_search_for_models_without_tools(monkeypatch):
    monkeypatch.setattr(library, 'has_searchable', lambda user_id, model: True)
    monkeypatch.setattr(llm, 'detect_ollama', lambda config, use_cache=True: True)
    monkeypatch.setattr(llm, 'model_supports_tools', lambda config: False)
    searches = []
    monkeypatch.setattr(library, 'search', lambda u, q, c, limit: searches.append(q) or [passage(5)])
    fake = ScriptedLLM([[('content', 'Rest [1].')]])
    monkeypatch.setattr(llm, 'stream_chat', fake)
    events, done = _run(_messages(), question='Hoeveel rustdagen na een wedstrijd?')
    assert searches == ['Hoeveel rustdagen na een wedstrijd?']
    assert fake.calls[0]['tools'] is None
    assert '[1] Guide, p. 1' in fake.calls[0]['messages'][0]['content']
    assert done['steps'][0]['automatic'] is True and done['sources'][0]['n'] == 1


def test_no_library_means_no_tools_and_no_search(monkeypatch):
    monkeypatch.setattr(library, 'has_searchable', lambda user_id, model: False)
    monkeypatch.setattr(llm, 'detect_ollama', lambda config, use_cache=True: True)
    monkeypatch.setattr(library, 'search', lambda *a, **k: pytest.fail('should not search'))
    fake = ScriptedLLM([[('content', 'Answer.')]])
    monkeypatch.setattr(llm, 'stream_chat', fake)
    events, done = _run(_messages())
    assert fake.calls[0]['tools'] is None
    assert 'Training literature' not in fake.calls[0]['messages'][0]['content']
    assert done == {'sources': [], 'steps': [], 'truncated': False}


@pytest.mark.parametrize('setting,supports,expected', [
    ('auto', True, 'tools'), ('auto', False, 'always'), ('tools', False, 'tools'),
    ('always', True, 'always'), ('off', True, None),
])
def test_search_mode(setting, supports, expected, monkeypatch):
    monkeypatch.setattr(library, 'has_searchable', lambda user_id, model: True)
    monkeypatch.setattr(llm, 'model_supports_tools', lambda config: supports)
    assert agent.search_mode(dict(AGENT_CONFIG, library_search=setting), 1) == expected


def test_search_mode_setting_validation(authed_client):
    assert coach_settings.save_llm_settings({'library_search': 'always'})['library_search'] == 'always'
    with pytest.raises(coach_settings.SettingsError):
        coach_settings.save_llm_settings({'library_search': 'sometimes'})


# --- Through the chat endpoint ------------------------------------------------------

def test_chat_streams_searches_and_stores_sources(authed_client, library_available, monkeypatch):
    coach_settings.save_llm_settings({'enabled': True, 'base_url': 'http://ollama:11434/v1', 'model': 'm',
                                      'embedding_model': 'fake', 'context_window': 16384})
    monkeypatch.setattr(library, 'search', lambda u, q, c, limit: [passage(7, title='Daniels', page=45)])
    fake = ScriptedLLM([[tool_call('long run pace')], [('content', 'Run it easier [1].')]])
    conversation = authed_client.post('/api/coach/conversations').get_json()
    url = f"/api/coach/conversations/{conversation['id']}/messages"
    with patch('coach.llm.stream_chat', side_effect=fake):
        events = _sse_events(authed_client.post(url, json={'content': 'Is my long run too fast?'}))
    names = [e for e, _ in events]
    assert names == ['start', 'search', 'search_result', 'content', 'sources', 'done']
    sources = dict(events)['sources']['sources']
    assert sources[0]['title'] == 'Daniels' and sources[0]['page'] == 45

    stored = authed_client.get(f"/api/coach/conversations/{conversation['id']}").get_json()['messages'][-1]
    assert stored['content'] == 'Run it easier [1].'
    assert stored['sources'][0]['n'] == 1 and stored['sources'][0]['title'] == 'Daniels'
    assert stored['steps'] == [{'query': 'long run pace', 'found': 1, 'titles': ['Daniels'], 'automatic': False}]

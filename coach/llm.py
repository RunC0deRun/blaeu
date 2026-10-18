"""
A small LLM client for OpenAI-compatible endpoints, with a native path for Ollama.

Ollama's OpenAI-compatible endpoint ignores the context size, and its default context is
small: a long prompt is silently cut off. When the endpoint is Ollama, we therefore use its
native /api/chat, which accepts num_ctx. Everything else goes through /chat/completions.

stream_chat() yields ('content', text) and ('reasoning', text) events. Reasoning from
"thinking" models arrives either as a separate field or inline in <think> tags; both end up
as 'reasoning' so the UI can fold it away.
"""
import json
import logging
import time

import requests

logger = logging.getLogger('blaeu.coach.llm')

CONNECT_TIMEOUT = 10
# Local models on a CPU can take minutes before the first token
READ_TIMEOUT = 300
PROBE_TIMEOUT = 5


class LLMError(Exception):
    """A problem talking to the LLM, with a message that is safe to show to users."""


def api_root(base_url):
    """The server root, without a trailing /v1 (Ollama's native API lives there)."""
    root = base_url.rstrip('/')
    return root[:-3] if root.endswith('/v1') else root


def _headers(config):
    headers = {'Content-Type': 'application/json'}
    if config.get('api_key'):
        headers['Authorization'] = f"Bearer {config['api_key']}"
    return headers


def _http_error(resp, what):
    detail = ''
    try:
        body = resp.json()
        error = body.get('error') if isinstance(body, dict) else None
        detail = error.get('message') if isinstance(error, dict) else (error or '')
    except ValueError:
        pass
    if resp.status_code in (401, 403):
        message = 'The LLM endpoint rejected the API key.'
    elif resp.status_code == 404:
        message = 'The LLM endpoint or model was not found.'
    elif resp.status_code == 429:
        message = 'The LLM endpoint is rate limiting requests. Try again in a moment.'
    else:
        message = f'The LLM endpoint returned HTTP {resp.status_code} while {what}.'
    if detail:
        message += f' ({str(detail)[:200]})'
    return LLMError(message)


def _request(method, url, config, what, **kwargs):
    try:
        resp = requests.request(method, url, headers=_headers(config), **kwargs)
    except requests.exceptions.Timeout:
        raise LLMError(f'The LLM endpoint did not respond in time while {what}.')
    except requests.exceptions.RequestException:
        raise LLMError(f'Cannot reach the LLM endpoint at {config["base_url"]}.')
    if resp.status_code >= 400:
        raise _http_error(resp, what)
    return resp


_ollama_cache = {}
OLLAMA_CACHE_SECONDS = 300


def detect_ollama(config, use_cache=True):
    """True when the endpoint is an Ollama server (it answers /api/version)."""
    root = api_root(config['base_url'])
    cached = _ollama_cache.get(root)
    if use_cache and cached and time.monotonic() - cached[1] < OLLAMA_CACHE_SECONDS:
        return cached[0]
    try:
        resp = requests.get(f"{root}/api/version", timeout=PROBE_TIMEOUT)
        result = resp.status_code == 200 and 'version' in resp.json()
    except (requests.exceptions.RequestException, ValueError):
        result = False
    _ollama_cache[root] = (result, time.monotonic())
    return result


def list_models(config, is_ollama=None):
    if is_ollama is None:
        is_ollama = detect_ollama(config)
    if is_ollama:
        resp = _request('GET', f"{api_root(config['base_url'])}/api/tags", config,
                        'listing models', timeout=(CONNECT_TIMEOUT, 30))
        return sorted(m.get('name') for m in resp.json().get('models', []) if m.get('name'))
    resp = _request('GET', f"{config['base_url']}/models", config,
                    'listing models', timeout=(CONNECT_TIMEOUT, 30))
    data = resp.json()
    return sorted(m.get('id') for m in data.get('data', []) if isinstance(m, dict) and m.get('id'))


class ThinkSplitter:
    """Separates inline <think>...</think> blocks from the answer, across chunk boundaries."""
    OPEN, CLOSE = '<think>', '</think>'

    def __init__(self):
        self.buffer = ''
        self.in_think = False

    def feed(self, text):
        self.buffer += text
        events = []
        while self.buffer:
            tag = self.CLOSE if self.in_think else self.OPEN
            kind = 'reasoning' if self.in_think else 'content'
            idx = self.buffer.find(tag)
            if idx >= 0:
                if idx:
                    events.append((kind, self.buffer[:idx]))
                self.buffer = self.buffer[idx + len(tag):]
                self.in_think = not self.in_think
                continue
            # Keep a possible partial tag at the end for the next chunk
            keep = 0
            for n in range(min(len(tag) - 1, len(self.buffer)), 0, -1):
                if tag.startswith(self.buffer[-n:]):
                    keep = n
                    break
            emit = self.buffer[:len(self.buffer) - keep]
            if emit:
                events.append((kind, emit))
            self.buffer = self.buffer[len(self.buffer) - keep:]
            break
        return events

    def flush(self):
        events = []
        if self.buffer:
            events.append(('reasoning' if self.in_think else 'content', self.buffer))
            self.buffer = ''
        return events


def _stream_ollama(config, messages, max_tokens, temperature):
    payload = {
        'model': config['model'],
        'messages': messages,
        'stream': True,
        'options': {
            'num_ctx': config['context_window'],
            'temperature': temperature,
            'num_predict': max_tokens,
        },
    }
    resp = _request('POST', f"{api_root(config['base_url'])}/api/chat", config, 'generating an answer',
                    json=payload, stream=True, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
    with resp:
        for line in resp.iter_lines(decode_unicode=True):
            if not line:
                continue
            try:
                chunk = json.loads(line)
            except ValueError:
                continue
            if chunk.get('error'):
                raise LLMError(f"The LLM returned an error: {str(chunk['error'])[:200]}")
            message = chunk.get('message') or {}
            if message.get('thinking'):
                yield ('reasoning', message['thinking'])
            if message.get('content'):
                yield ('content', message['content'])
            if chunk.get('done'):
                return


def _stream_openai(config, messages, max_tokens, temperature):
    payload = {
        'model': config['model'],
        'messages': messages,
        'stream': True,
        'temperature': temperature,
        'max_tokens': max_tokens,
    }
    resp = _request('POST', f"{config['base_url']}/chat/completions", config,
                    'generating an answer', json=payload, stream=True,
                    timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
    with resp:
        for line in resp.iter_lines(decode_unicode=True):
            if not line or not line.startswith('data:'):
                continue
            data = line[5:].strip()
            if data == '[DONE]':
                return
            try:
                chunk = json.loads(data)
            except ValueError:
                continue
            if chunk.get('error'):
                error = chunk['error']
                detail = error.get('message') if isinstance(error, dict) else error
                raise LLMError(f"The LLM returned an error: {str(detail)[:200]}")
            for choice in chunk.get('choices') or []:
                delta = choice.get('delta') or {}
                reasoning = delta.get('reasoning_content') or delta.get('reasoning')
                if reasoning:
                    yield ('reasoning', reasoning)
                if delta.get('content'):
                    yield ('content', delta['content'])


def stream_chat(config, messages, max_tokens=None, temperature=None, is_ollama=None):
    """Streams an answer as ('content' | 'reasoning', text) events."""
    if is_ollama is None:
        is_ollama = detect_ollama(config)
    max_tokens = max_tokens or config['max_answer_tokens']
    temperature = config['temperature'] if temperature is None else temperature
    source = (_stream_ollama if is_ollama else _stream_openai)(config, messages, max_tokens, temperature)
    splitter = ThinkSplitter()
    try:
        for kind, text in source:
            if kind == 'reasoning':
                yield (kind, text)
            else:
                yield from splitter.feed(text)
        yield from splitter.flush()
    except requests.exceptions.RequestException:
        raise LLMError('The connection to the LLM endpoint was interrupted.')


def complete(config, messages, max_tokens=None, temperature=None, is_ollama=None):
    return ''.join(text for kind, text in stream_chat(config, messages, max_tokens, temperature, is_ollama)
                   if kind == 'content')


def test_connection(config):
    """
    Checks an endpoint step by step for the admin page. Returns
    {'ok', 'is_ollama', 'models', 'model_found', 'reply', 'seconds', 'error'}.
    """
    result = {'ok': False, 'is_ollama': False, 'models': [], 'model_found': None,
              'reply': None, 'seconds': None, 'error': None}
    try:
        result['is_ollama'] = detect_ollama(config, use_cache=False)
        try:
            result['models'] = list_models(config, result['is_ollama'])
        except LLMError as e:
            # Some providers don't offer a model list; the chat test below still counts
            logger.info(f"Model list unavailable: {e}")
        if result['models'] and config['model']:
            result['model_found'] = config['model'] in result['models']
        if not config['model']:
            result['error'] = 'Choose a model to finish the test.'
            return result
        started = time.monotonic()
        reply = complete(config, [{'role': 'user', 'content': 'Reply with only the word OK.'}],
                         max_tokens=200, temperature=0, is_ollama=result['is_ollama'])
        result['seconds'] = round(time.monotonic() - started, 1)
        result['reply'] = reply.strip()[:200]
        result['ok'] = True
    except LLMError as e:
        result['error'] = str(e)
    return result

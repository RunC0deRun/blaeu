"""
Embeddings for the training literature library: turn text into vectors whose distance
reflects meaning. Uses Ollama's native /api/embed when the endpoint is Ollama, otherwise the
OpenAI-compatible /embeddings. Vectors are normalised and stored in sqlite-vec's float32
format, so the database can compare them directly.
"""
import math

import sqlite_vec

from coach import llm
from coach.settings import embedding_config

BATCH_SIZE = 16
EMBED_TIMEOUT = (llm.CONNECT_TIMEOUT, 120)


def _normalise(vectors):
    rows = []
    for vector in vectors:
        if not vector:
            raise llm.LLMError('The embedding endpoint returned an empty vector.')
        norm = math.sqrt(sum(x * x for x in vector)) or 1.0
        rows.append([x / norm for x in vector])
    return rows


def embed(config, texts):
    """Returns one normalised vector (a list of floats) per text."""
    if not texts:
        return []
    cfg = embedding_config(config)
    model = config['embedding_model']
    rows = []
    for start in range(0, len(texts), BATCH_SIZE):
        batch = texts[start:start + BATCH_SIZE]
        if llm.detect_ollama(cfg):
            resp = llm._request('POST', f"{llm.api_root(cfg['base_url'])}/api/embed", cfg, 'creating embeddings',
                                json={'model': model, 'input': batch, 'truncate': True}, timeout=EMBED_TIMEOUT)
            vectors = resp.json().get('embeddings') or []
        else:
            resp = llm._request('POST', f"{cfg['base_url']}/embeddings", cfg, 'creating embeddings',
                                json={'model': model, 'input': batch}, timeout=EMBED_TIMEOUT)
            data = sorted(resp.json().get('data') or [], key=lambda item: item.get('index', 0))
            vectors = [item.get('embedding') for item in data]
        if len(vectors) != len(batch):
            raise llm.LLMError('The embedding endpoint returned a different number of vectors than requested.')
        rows.extend(_normalise(vectors))
    return rows


def to_blob(vector):
    return sqlite_vec.serialize_float32(vector)


def test_embeddings(config):
    """For the admin connection test: embeds one sentence and reports the vector size."""
    if not config.get('embedding_model'):
        return None
    try:
        vectors = embed(config, ['Easy runs build the aerobic base.'])
        return {'ok': True, 'dimensions': len(vectors[0]), 'error': None}
    except llm.LLMError as e:
        return {'ok': False, 'dimensions': None, 'error': str(e)}

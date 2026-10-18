import json
import logging

from flask import Blueprint, Response, jsonify, request, stream_with_context

from db import (
    get_user_by_id, get_coach_profile, save_coach_profile, COACH_PROFILE_FIELDS,
    create_conversation, get_conversation, get_conversations, delete_conversation,
    get_chat_messages, add_chat_message, get_db,
)
from utils import get_current_user_id, rate_limit
from coach import agent, llm
from coach.context import build_messages, ContextTooSmall
from coach.settings import (
    get_llm_config, public_llm_config, is_configured, save_llm_settings, preview_llm_config,
    SettingsError,
)

logger = logging.getLogger('blaeu.coach')
coach_bp = Blueprint('coach', __name__)

MAX_MESSAGE_CHARS = 8000
MAX_PROFILE_FIELD_CHARS = 4000
TITLE_CHARS = 60


def _require_user():
    user_id = get_current_user_id()
    if not user_id:
        return None, (jsonify({'error': 'Unauthorized. Please log in.'}), 401)
    return user_id, None


def _require_admin():
    user_id, error = _require_user()
    if error:
        return None, error
    user = get_user_by_id(user_id)
    if not user or not user['is_admin']:
        return None, (jsonify({'error': 'Forbidden. Admin privileges required.'}), 403)
    return user_id, None


# --- Admin: LLM endpoint -------------------------------------------------------------

@coach_bp.route('/api/admin/llm', methods=['GET'])
def get_llm_settings():
    _, error = _require_admin()
    if error:
        return error
    return jsonify(public_llm_config())


@coach_bp.route('/api/admin/llm', methods=['PUT'])
def update_llm_settings():
    _, error = _require_admin()
    if error:
        return error
    try:
        config = save_llm_settings(request.json or {})
    except SettingsError as e:
        return jsonify({'error': str(e)}), 400
    except Exception:
        logger.exception("Failed to save LLM settings")
        return jsonify({'error': 'Failed to save the LLM settings: An unexpected server error occurred.'}), 500
    return jsonify(public_llm_config(config))


@coach_bp.route('/api/admin/llm/test', methods=['POST'])
@rate_limit(limit=10, period=60)
def test_llm_settings():
    """Tests the settings in the form (saved values fill in what the form leaves out)."""
    _, error = _require_admin()
    if error:
        return error
    try:
        config = preview_llm_config(request.json or {})
    except SettingsError as e:
        return jsonify({'error': str(e)}), 400
    if not config['base_url']:
        return jsonify({'error': 'Enter the endpoint URL first.'}), 400
    from coach.embeddings import test_embeddings
    result = llm.test_connection(config)
    result['embeddings'] = test_embeddings(config)
    return jsonify(result)


# --- Coach status and profile --------------------------------------------------------

@coach_bp.route('/api/coach/status', methods=['GET'])
def coach_status():
    user_id, error = _require_user()
    if error:
        return error
    config = get_llm_config()
    available = is_configured(config)
    return jsonify({
        'available': available,
        'model': config['model'] if available else None,
        'is_external': public_llm_config(config)['is_external'] if available else False,
    })


@coach_bp.route('/api/coach/profile', methods=['GET'])
def get_profile():
    user_id, error = _require_user()
    if error:
        return error
    profile = get_coach_profile(user_id)
    return jsonify({k: profile.get(k) for k in (*COACH_PROFILE_FIELDS, 'updated_at')})


@coach_bp.route('/api/coach/profile', methods=['PUT'])
def update_profile():
    user_id, error = _require_user()
    if error:
        return error
    data = request.json or {}
    fields = {}
    for key in COACH_PROFILE_FIELDS:
        value = data.get(key)
        if value is not None and not isinstance(value, str):
            return jsonify({'error': f'{key} must be text.'}), 400
        value = (value or '').strip()
        if len(value) > MAX_PROFILE_FIELD_CHARS:
            return jsonify({'error': f'{key} is too long (at most {MAX_PROFILE_FIELD_CHARS} characters).'}), 400
        fields[key] = value or None
    try:
        save_coach_profile(user_id, fields)
    except Exception:
        logger.exception("Failed to save coach profile")
        return jsonify({'error': 'Failed to save your coach profile: An unexpected server error occurred.'}), 500
    return get_profile()


# --- Conversations -------------------------------------------------------------------

def _serialize_message(message):
    data = {k: message.get(k) for k in ('id', 'role', 'content', 'reasoning', 'status', 'model', 'created_at')}
    for key in ('sources', 'steps'):
        data[key] = json.loads(message[key]) if message.get(key) else []
    return data


@coach_bp.route('/api/coach/conversations', methods=['GET'])
def list_conversations():
    user_id, error = _require_user()
    if error:
        return error
    return jsonify([
        {k: c[k] for k in ('id', 'title', 'created_at', 'updated_at')} for c in get_conversations(user_id)
    ])


@coach_bp.route('/api/coach/conversations', methods=['POST'])
def new_conversation():
    user_id, error = _require_user()
    if error:
        return error
    conversation_id = create_conversation(user_id, None)
    conversation = get_conversation(conversation_id, user_id)
    return jsonify({k: conversation[k] for k in ('id', 'title', 'created_at', 'updated_at')}), 201


@coach_bp.route('/api/coach/conversations/<int:conversation_id>', methods=['GET'])
def get_conversation_detail(conversation_id):
    user_id, error = _require_user()
    if error:
        return error
    conversation = get_conversation(conversation_id, user_id)
    if not conversation:
        return jsonify({'error': 'Conversation not found.'}), 404
    return jsonify({
        **{k: conversation[k] for k in ('id', 'title', 'created_at', 'updated_at')},
        'messages': [_serialize_message(m) for m in get_chat_messages(conversation_id)],
    })


@coach_bp.route('/api/coach/conversations/<int:conversation_id>', methods=['DELETE'])
def remove_conversation(conversation_id):
    user_id, error = _require_user()
    if error:
        return error
    if not delete_conversation(conversation_id, user_id):
        return jsonify({'error': 'Conversation not found.'}), 404
    return jsonify({'success': True})


def _set_title_if_missing(conversation, first_message):
    if conversation['title']:
        return
    title = ' '.join(first_message.split())
    if len(title) > TITLE_CHARS:
        title = title[:TITLE_CHARS - 1].rstrip() + '…'
    conn = get_db()
    try:
        conn.execute("UPDATE chat_conversations SET title = ? WHERE id = ?", (title, conversation['id']))
        conn.commit()
    finally:
        conn.close()


def _sse(event, data):
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@coach_bp.route('/api/coach/conversations/<int:conversation_id>/messages', methods=['POST'])
@rate_limit(limit=20, period=60)
def send_message(conversation_id):
    """
    Sends a message (or, with {"retry": true}, answers the last unanswered one again) and
    streams the coach's answer as server-sent events: start, reasoning, content, search,
    search_result, sources, error and done.
    """
    user_id, error = _require_user()
    if error:
        return error
    conversation = get_conversation(conversation_id, user_id)
    if not conversation:
        return jsonify({'error': 'Conversation not found.'}), 404

    config = get_llm_config()
    if not is_configured(config):
        return jsonify({'error': 'The AI coach is not set up yet. Ask an administrator to configure it.'}), 503

    data = request.json or {}
    history = [
        {'role': m['role'], 'content': m['content']}
        for m in get_chat_messages(conversation_id)
        if m['role'] in ('user', 'assistant') and m['content']
    ]
    retry = data.get('retry') is True
    if retry:
        if not history or history[-1]['role'] != 'user':
            return jsonify({'error': 'There is no unanswered message to retry.'}), 400
        content = history[-1]['content']
    else:
        content = data.get('content')
        if not isinstance(content, str) or not content.strip():
            return jsonify({'error': 'Write a message first.'}), 400
        content = content.strip()
        if len(content) > MAX_MESSAGE_CHARS:
            return jsonify({'error': f'Your message is too long (at most {MAX_MESSAGE_CHARS} characters).'}), 400
        history.append({'role': 'user', 'content': content})

    try:
        messages = build_messages(user_id, history, config)
    except ContextTooSmall:
        return jsonify({'error': "Your message doesn't fit in the model's context window. Try a shorter message."}), 400

    if not retry:
        add_chat_message(conversation_id, 'user', content)
        _set_title_if_missing(conversation, content)

    def generate():
        parts, reasoning = [], []
        result = {'sources': [], 'steps': [], 'truncated': False}
        status = 'interrupted'
        error_message = None
        try:
            yield _sse('start', {'model': config['model']})
            for kind, value in agent.run(user_id, config, messages, content):
                if kind in ('content', 'reasoning'):
                    (parts if kind == 'content' else reasoning).append(value)
                    yield _sse(kind, {'text': value})
                elif kind in ('search', 'search_result'):
                    yield _sse(kind, value)
                elif kind == 'done':
                    result = value
            truncated = result['truncated']
            if result['sources']:
                yield _sse('sources', {'sources': result['sources']})
            if truncated and not parts:
                status, error_message = 'error', (
                    'The model used its whole output limit on reasoning before answering. '
                    'An administrator can raise the context window in the AI coach settings.'
                )
            else:
                status = 'interrupted' if truncated else 'complete'
        except llm.LLMError as e:
            status, error_message = 'error', str(e)
        except GeneratorExit:
            # The browser went away (closed or pressed stop): keep what we have
            raise
        except Exception:
            logger.exception("Unexpected error while streaming a coach answer")
            status, error_message = 'error', 'An unexpected error occurred while generating the answer.'
        finally:
            answer = ''.join(parts)
            message_id = None
            # Store complete and partial answers; a failure without any text stores nothing,
            # so the question stays unanswered and can be retried
            if answer or status == 'complete':
                message_id = add_chat_message(
                    conversation_id, 'assistant', answer, ''.join(reasoning) or None,
                    config['model'], status if status != 'error' else 'interrupted',
                    sources=result['sources'], steps=result['steps']
                )
        if error_message:
            yield _sse('error', {'message': error_message})
        yield _sse('done', {'status': status, 'message_id': message_id})

    return Response(stream_with_context(generate()), mimetype='text/event-stream', headers={
        'Cache-Control': 'no-cache',
        'X-Accel-Buffering': 'no',  # don't let a reverse proxy buffer the stream
    })

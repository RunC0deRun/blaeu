"""
The coach's note on the welcome screen: a few sentences on how training and recovery look and
what to do today.

Local reasoning models can take minutes to answer, so the note is generated in a background
thread and cached per user and day. It is generated again when a newer workout arrives, so
the morning note reflects yesterday's session and an afternoon sync updates it. A failed note
is not retried automatically; the user can ask for a new one.
"""
import logging
import threading
from datetime import date, datetime, timedelta, timezone

from db import (
    get_db, get_daily_note, claim_daily_note, finish_daily_note, create_conversation, add_chat_message,
    get_conversation, set_daily_note_conversation,
)
from coach import llm
from coach.context import build_messages, output_budget, ContextTooSmall
from coach.settings import get_llm_config, is_configured

logger = logging.getLogger('blaeu.coach.daily_note')

# A slow model may take a few minutes; after this the note counts as abandoned
LEASE_MINUTES = 15

PROMPT = """Write today's note for the welcome screen of my training log, in 2 to 4 sentences.
Say briefly how my latest workout and my recovery look, then suggest what to do today with a
duration and intensity. Mention a target event only if my profile names one. Plain sentences:
no greeting, headings or lists."""

# What the conversation shows as the question when the athlete discusses the note
DISCUSS_QUESTION = "How should I train today?"


def _now():
    return datetime.now(timezone.utc)


def _fmt(dt):
    return dt.strftime('%Y-%m-%d %H:%M:%S')


def latest_activity_id(user_id):
    conn = get_db()
    try:
        row = conn.execute("""
            SELECT id FROM activities WHERE user_id = ? AND hidden = 0 AND merged_into IS NULL
            ORDER BY start_time DESC, id DESC LIMIT 1
        """, (user_id,)).fetchone()
    finally:
        conn.close()
    return row['id'] if row else None


def generate(user_id, day, config):
    """Asks the model for the note and stores the outcome; runs in a background thread."""
    try:
        messages = build_messages(user_id, [{'role': 'user', 'content': PROMPT}], config, today=day)
        parts, truncated = [], False
        for kind, value in llm.stream_chat(config, messages, max_tokens=output_budget(messages, config)):
            if kind == 'content':
                parts.append(value)
            elif kind == 'truncated':
                truncated = True
        content = ''.join(parts).strip()
        if not content:
            error = ('The model used its whole output limit on reasoning before writing the note.'
                     if truncated else 'The model returned an empty note.')
            finish_daily_note(user_id, day.isoformat(), 'failed', error=error)
            return
        finish_daily_note(user_id, day.isoformat(), 'ready', content=content, model=config['model'])
    except ContextTooSmall:
        finish_daily_note(user_id, day.isoformat(), 'failed',
                          error="The training data doesn't fit in the model's context window.")
    except llm.LLMError as e:
        finish_daily_note(user_id, day.isoformat(), 'failed', error=str(e))
    except Exception:
        logger.exception("Generating the daily coach note failed")
        finish_daily_note(user_id, day.isoformat(), 'failed', error='An unexpected error occurred.')


def _start(app, user_id, day, config):
    def run():
        with app.app_context():
            generate(user_id, day, config)
    threading.Thread(target=run, name='coach-daily-note', daemon=True).start()


def get_or_start(app, user_id, day, refresh=False):
    """
    Returns the day's note, starting its generation when there is none yet, when a newer
    workout came in, or when refresh is set. Returns None when the coach is not set up.
    """
    config = get_llm_config()
    if not is_configured(config):
        return None
    note = get_daily_note(user_id, day.isoformat())
    latest = latest_activity_id(user_id)
    now = _now()
    generating = note and note['status'] == 'generating' and note['locked_until'] and note['locked_until'] > _fmt(now)
    stale = note is None or note['based_on_activity_id'] != latest or (
        note['status'] == 'generating' and not generating)
    if not generating and (stale or refresh):
        if claim_daily_note(user_id, day.isoformat(), latest, _fmt(now), _fmt(now + timedelta(minutes=LEASE_MINUTES))):
            _start(app, user_id, day, config)
        note = get_daily_note(user_id, day.isoformat())
    return note


def discuss(user_id, day):
    """
    Returns the coach conversation about the day's note, starting one with the note as its
    first answer when there is none yet (or the user deleted it).
    """
    note = get_daily_note(user_id, day.isoformat())
    if not note or note['status'] != 'ready':
        return None
    if note['conversation_id'] and get_conversation(note['conversation_id'], user_id):
        return note['conversation_id']
    conversation_id = create_conversation(user_id, f"Today's note, {day.strftime('%d %b')}")
    add_chat_message(conversation_id, 'user', DISCUSS_QUESTION)
    add_chat_message(conversation_id, 'assistant', note['content'], model=note['model'])
    set_daily_note_conversation(user_id, day.isoformat(), conversation_id)
    return conversation_id


def parse_day(value):
    """The user's local date from the browser; falls back to today in UTC when missing or implausible."""
    today = _now().date()
    try:
        day = date.fromisoformat(value) if value else today
    except (TypeError, ValueError):
        return today
    # Time zones reach from UTC-12 to UTC+14, so the local date is at most a day off
    return day if abs((day - today).days) <= 1 else today

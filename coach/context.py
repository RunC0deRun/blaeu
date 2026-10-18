"""
Builds what the coach knows: a system prompt with the athlete's profile, thresholds, fitness,
wellness (only with the user's opt-in) and recent training, plus as much conversation
history as fits in the model's context window.

Activity data is summarised here rather than handed over raw: small local models have small
context windows and do better with a compact, pre-computed overview.
"""
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from db import (
    get_activities, get_athlete_thresholds, get_coach_profile, get_daily_wellness,
    get_user_by_id,
)

# Characters per token; deliberately low so estimates err on the safe side
CHARS_PER_TOKEN = 3.5
# Room kept free for formatting overhead per message
MESSAGE_OVERHEAD_TOKENS = 8
SAFETY_MARGIN_TOKENS = 256
# Detail levels, tried from most to least detailed until the prompt fits
DETAIL_LEVELS = (
    {'weeks': 8, 'recent_days': 14, 'recent_max': 15, 'wellness_days': 7},
    {'weeks': 4, 'recent_days': 7, 'recent_max': 8, 'wellness_days': 3},
    {'weeks': 0, 'recent_days': 0, 'recent_max': 0, 'wellness_days': 0},
)
WEEKDAYS = ('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday')

INSTRUCTIONS = """You are the training coach in Blaeu, a self-hosted app where the athlete keeps their training log.
Coach the athlete using the data below: their goals, recent training, fitness and wellness.

- Base your advice on this data. Say so when data is missing; never invent numbers or workouts.
- Be concise and practical: concrete sessions, durations and intensities rather than general advice.
- Respect their goals, availability and limitations.
- You are not a doctor. For pain, injury, illness, chest pain, dizziness or anything worrying,
  tell them to see a medical professional rather than giving a diagnosis.
- Use metric units, and answer in the language the athlete writes in.
- Training load is a TSS-like score. Fitness (CTL) is the 42-day average load, fatigue (ATL)
  the 7-day average, and form is fitness minus fatigue (negative means fatigued).
- Sleep quality is on a 1 to 4 scale where 1 is the best."""


def estimate_tokens(text):
    return int(len(text) / CHARS_PER_TOKEN) + 1


def _fmt_duration(seconds):
    if not seconds:
        return '-'
    minutes = int(round(seconds / 60))
    return f"{minutes // 60}h{minutes % 60:02d}" if minutes >= 60 else f"{minutes} min"


def _fmt_km(meters):
    return f"{meters / 1000:.1f} km" if meters else '-'


def _fmt_pace(meters_per_second, per_meters, unit):
    if not meters_per_second:
        return None
    seconds = per_meters / meters_per_second
    return f"{int(seconds // 60)}:{int(round(seconds % 60)):02d} {unit}"


def _num(value, digits=0):
    if value is None:
        return '-'
    return f"{value:.{digits}f}"


def _activity_day(activity):
    stamp = activity.get('start_time_local') or activity.get('start_time') or ''
    try:
        return date.fromisoformat(stamp[:10])
    except ValueError:
        return None


def _profile_section(profile):
    labels = (
        ('goals', 'Goals'), ('target_events', 'Target events'), ('availability', 'Availability'),
        ('injuries', 'Injuries and limitations'), ('notes', 'Other notes'),
    )
    lines = [f"{label}: {profile[key].strip()}" for key, label in labels if (profile.get(key) or '').strip()]
    if not lines:
        return "The athlete hasn't filled in their coach profile yet. Ask about their goals when it matters."
    return '\n'.join(lines)


def _thresholds_section(thresholds):
    lines = []
    for t in thresholds:
        parts = []
        if t.get('ftp'):
            parts.append(f"FTP {t['ftp']:.0f} W")
        if t.get('lthr'):
            parts.append(f"threshold HR {t['lthr']:.0f} bpm")
        if t.get('max_hr'):
            parts.append(f"max HR {t['max_hr']:.0f} bpm")
        if t.get('resting_hr'):
            parts.append(f"resting HR {t['resting_hr']:.0f} bpm")
        sport = (t.get('sport') or '').lower()
        if t.get('threshold_pace'):
            pace = _fmt_pace(t['threshold_pace'], 100, '/100m') if 'swim' in sport \
                else _fmt_pace(t['threshold_pace'], 1000, '/km')
            if pace:
                parts.append(f"threshold pace {pace}")
        if parts:
            lines.append(f"{t['sport']}: {', '.join(parts)}")
    return '\n'.join(lines) if lines else 'No thresholds known.'


def _merge_wellness(rows):
    """Combines per-source rows into one record per date, preferring values that are set."""
    by_date = {}
    for row in rows:
        merged = by_date.setdefault(row['date'], {})
        for key, value in row.items():
            if value is not None and merged.get(key) is None:
                merged[key] = value
    return [by_date[d] for d in sorted(by_date, reverse=True)]


def _fitness_section(wellness):
    latest = next((w for w in wellness if w.get('ctl') is not None and w.get('atl') is not None), None)
    if not latest:
        return 'No fitness data yet.'
    form = latest['ctl'] - latest['atl']
    line = (f"On {latest['date']}: fitness (CTL) {latest['ctl']:.0f}, fatigue (ATL) {latest['atl']:.0f}, "
            f"form {form:+.0f}")
    if latest.get('ramp_rate') is not None:
        line += f", ramp rate {latest['ramp_rate']:+.1f} per week"
    return line


def _wellness_section(wellness, days):
    recent = [w for w in wellness[:days]]
    if not recent:
        return None
    lines = ['date | resting HR | HRV | sleep | sleep score | sleep quality | weight']
    for w in recent:
        sleep = _fmt_duration(w.get('sleep_secs')) if w.get('sleep_secs') else '-'
        lines.append(' | '.join([
            w['date'], _num(w.get('resting_hr')), _num(w.get('hrv')), sleep,
            _num(w.get('sleep_score')), _num(w.get('sleep_quality')), _num(w.get('weight'), 1),
        ]))
    baseline = [w for w in wellness[:28]]
    averages = []
    for key, label in (('resting_hr', 'resting HR'), ('hrv', 'HRV')):
        values = [w[key] for w in baseline if w.get(key) is not None]
        if values:
            averages.append(f"{label} {sum(values) / len(values):.0f}")
    vo2 = next((w['vo2max'] for w in wellness if w.get('vo2max')), None)
    if averages:
        lines.append(f"28-day average: {', '.join(averages)}")
    if vo2:
        lines.append(f"Latest VO2max estimate: {vo2:.0f}")
    return '\n'.join(lines)


def _weekly_section(activities, today, weeks, week_start):
    if not weeks:
        return None
    start_offset = (today.weekday() - WEEKDAYS.index(week_start)) % 7
    this_week = today - timedelta(days=start_offset)
    lines = ['week starting | sessions | time | distance | load | sports']
    for i in range(weeks - 1, -1, -1):
        week_start_day = this_week - timedelta(weeks=i)
        week_end = week_start_day + timedelta(days=7)
        acts = [a for a in activities if (d := _activity_day(a)) and week_start_day <= d < week_end]
        sports = defaultdict(int)
        for a in acts:
            sports[a.get('sport_type') or 'Unknown'] += 1
        load = sum(a.get('training_load') or 0 for a in acts)
        label = week_start_day.isoformat() + (' (this week, so far)' if i == 0 else '')
        lines.append(' | '.join([
            label, str(len(acts)),
            _fmt_duration(sum(a.get('moving_time') or a.get('duration') or 0 for a in acts)),
            _fmt_km(sum(a.get('distance') or 0 for a in acts)),
            f"{load:.0f}" if load else '-',
            ', '.join(f"{n}x {s}" for s, n in sorted(sports.items())) or '-',
        ]))
    return '\n'.join(lines)


def _recent_section(activities, today, days, limit):
    if not days:
        return None
    since = today - timedelta(days=days)
    recent = [a for a in activities if (d := _activity_day(a)) and d >= since][:limit]
    if not recent:
        return f"No workouts in the last {days} days."
    lines = ['date | sport | name | time | distance | avg HR | power | load | intensity | RPE']
    for a in recent:
        lines.append(' | '.join([
            str(_activity_day(a)), a.get('sport_type') or 'Unknown', (a.get('name') or '')[:40],
            _fmt_duration(a.get('moving_time') or a.get('duration')), _fmt_km(a.get('distance')),
            _num(a.get('avg_hr')), f"{a['avg_power']:.0f} W" if a.get('avg_power') else '-',
            _num(a.get('training_load')), _num(a.get('intensity'), 2), _num(a.get('rpe')),
        ]))
    return '\n'.join(lines)


def build_system_prompt(user_id, today=None, level=0):
    """The coach's instructions plus the athlete's data, at a given detail level."""
    today = today or datetime.now(timezone.utc).date()
    detail = DETAIL_LEVELS[level]
    user = get_user_by_id(user_id) or {}
    week_start = user.get('week_start_day') if user.get('week_start_day') in WEEKDAYS else 'Monday'

    activities = get_activities(user_id, start=(today - timedelta(weeks=9)).isoformat())
    wellness = _merge_wellness(get_daily_wellness(user_id, start=(today - timedelta(days=35)).isoformat()))

    sections = [
        INSTRUCTIONS,
        f"Today is {WEEKDAYS[today.weekday()]} {today.isoformat()}.",
        "## Athlete profile\n" + _profile_section(get_coach_profile(user_id)),
        "## Thresholds\n" + _thresholds_section(get_athlete_thresholds(user_id)),
        "## Fitness\n" + _fitness_section(wellness),
    ]
    if user.get('wellness_opt_in') == 1:
        section = _wellness_section(wellness, detail['wellness_days'])
        if section:
            sections.append(f"## Wellness, last {detail['wellness_days']} days\n" + section)
    weekly = _weekly_section(activities, today, detail['weeks'], week_start)
    if weekly:
        sections.append(f"## Training per week, last {detail['weeks']} weeks\n" + weekly)
    recent = _recent_section(activities, today, detail['recent_days'], detail['recent_max'])
    if recent:
        sections.append(f"## Recent workouts, newest first\n" + recent)
    return '\n\n'.join(sections)


class ContextTooSmall(Exception):
    """Even the shortest prompt and the latest message don't fit the context window."""


def build_messages(user_id, history, config, today=None):
    """
    Returns the messages to send: the system prompt, then as many recent history messages
    as fit. history is a list of {'role', 'content'} ending with the user's new message.
    """
    budget = config['context_window'] - config['max_answer_tokens'] - SAFETY_MARGIN_TOKENS
    for level in range(len(DETAIL_LEVELS)):
        system = build_system_prompt(user_id, today, level)
        system_tokens = estimate_tokens(system) + MESSAGE_OVERHEAD_TOKENS
        # Leave at least a third of the budget for the conversation, unless at minimal detail
        if system_tokens <= budget * 2 / 3 or level == len(DETAIL_LEVELS) - 1:
            break

    remaining = budget - system_tokens
    kept = []
    for message in reversed(history):
        cost = estimate_tokens(message['content']) + MESSAGE_OVERHEAD_TOKENS
        if cost > remaining:
            break
        kept.append({'role': message['role'], 'content': message['content']})
        remaining -= cost
    if not kept:
        raise ContextTooSmall()
    kept.reverse()
    # A conversation sent to the model should start with the user, not a cut-off answer
    while kept and kept[0]['role'] != 'user':
        kept.pop(0)
    return [{'role': 'system', 'content': system}] + kept

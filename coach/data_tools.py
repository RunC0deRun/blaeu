"""
Tools that let the coach look up the athlete's own training data: workouts in a period, one
workout in detail, totals per week or month, the fitness trend and (with the opt-in) wellness.

The prompt already carries a compact overview of the recent past; these tools are for anything
older or more detailed. Every tool only reads the requesting user's data, validates its
arguments, and returns compact text with a size cap, so a broad question can't flood the
context window.
"""
import json
from collections import defaultdict
from datetime import date, timedelta

from db import get_activities, get_activity, get_daily_wellness, get_user_by_id
from coach.context import _activity_day, _fmt_duration, _fmt_km, _fmt_pace, _merge_wellness, _num

MAX_RANGE_DAYS = 400
MAX_ACTIVITIES = 40
MAX_DAILY_ROWS = 62
MAX_PERIODS = 60


class ToolInputError(ValueError):
    """Invalid arguments; the message goes back to the model so it can correct itself."""


def _function(name, description, properties, required):
    return {'type': 'function', 'function': {
        'name': name, 'description': description,
        'parameters': {'type': 'object', 'required': required, 'properties': properties},
    }}


DATE = {'type': 'string', 'description': 'A date as YYYY-MM-DD.'}
SPORT = {'type': 'string', 'description': 'Optional sport filter, e.g. Run, Ride, WeightTraining.'}

TOOLS = {
    'get_activities': _function(
        'get_activities',
        "List the athlete's workouts between two dates (inclusive), newest first, with id, sport, "
        "duration, distance, heart rate, load and intensity. Use the id with get_activity_details.",
        {'start_date': DATE, 'end_date': DATE, 'sport': SPORT}, ['start_date', 'end_date'],
    ),
    'get_activity_details': _function(
        'get_activity_details',
        "Details of one workout by id: time in heart rate and pace zones, pace and grade adjusted "
        "pace, cadence, elevation, intervals or laps, load and perceived effort.",
        {'activity_id': {'type': 'integer', 'description': 'The workout id.'}}, ['activity_id'],
    ),
    'get_training_summary': _function(
        'get_training_summary',
        "Training totals per week or per month between two dates: sessions, time, distance, load, "
        "and how training time was spread over the heart rate zones. Good for comparing periods.",
        {'start_date': DATE, 'end_date': DATE, 'sport': SPORT,
         'period': {'type': 'string', 'enum': ['week', 'month'], 'description': 'Group per week or month.'}},
        ['start_date', 'end_date'],
    ),
    'get_fitness_trend': _function(
        'get_fitness_trend',
        "Fitness (CTL), fatigue (ATL) and form (CTL minus ATL) over time between two dates.",
        {'start_date': DATE, 'end_date': DATE}, ['start_date', 'end_date'],
    ),
    'get_wellness': _function(
        'get_wellness',
        "Daily resting heart rate, HRV, sleep and weight between two dates, with averages. "
        "Only available when the athlete shares wellness data.",
        {'start_date': DATE, 'end_date': DATE}, ['start_date', 'end_date'],
    ),
}

INSTRUCTIONS = """## Looking up training data
The overview above only covers the recent past. Use the data tools when a question needs more:
get_activities for workouts in a period, get_activity_details for one workout (by id),
get_training_summary to compare weeks or months, get_fitness_trend and get_wellness for trends.
Ask for the period you need, not more. Never guess data you could look up."""


def tool_definitions(wellness_allowed):
    return [t for name, t in TOOLS.items() if wellness_allowed or name != 'get_wellness']


def describe(name, arguments):
    """A short, human-readable label for a tool call, shown in the chat."""
    span = _span_label(arguments)
    if name == 'get_activities':
        sport = arguments.get('sport')
        return f"Looked up {sport + ' ' if sport else ''}workouts{span}"
    if name == 'get_activity_details':
        return f"Looked at workout {arguments.get('activity_id')} in detail"
    if name == 'get_training_summary':
        return f"Summarised training per {arguments.get('period') or 'week'}{span}"
    if name == 'get_fitness_trend':
        return f"Looked up the fitness trend{span}"
    if name == 'get_wellness':
        return f"Looked up wellness{span}"
    return f"Used {name}"


def _span_label(arguments):
    start, end = arguments.get('start_date'), arguments.get('end_date')
    return f" from {start} to {end}" if start and end else ''


def _dates(arguments):
    try:
        start = date.fromisoformat(str(arguments.get('start_date')))
        end = date.fromisoformat(str(arguments.get('end_date')))
    except ValueError:
        raise ToolInputError('start_date and end_date must be dates as YYYY-MM-DD.')
    if end < start:
        start, end = end, start
    if (end - start).days > MAX_RANGE_DAYS:
        raise ToolInputError(f'The period can be at most {MAX_RANGE_DAYS} days. Ask for a shorter period.')
    return start, end


def _activities_between(user_id, start, end, sport=None):
    activities = get_activities(user_id, start=start.isoformat(), end=(end + timedelta(days=1)).isoformat())
    activities = [a for a in activities if (d := _activity_day(a)) and start <= d <= end]
    if sport:
        wanted = str(sport).lower()
        activities = [a for a in activities if wanted in (a.get('sport_type') or '').lower()]
    return activities


def get_activities_tool(user_id, arguments):
    start, end = _dates(arguments)
    activities = _activities_between(user_id, start, end, arguments.get('sport'))
    if not activities:
        return 'No workouts in this period.', '0 workouts'
    shown = activities[:MAX_ACTIVITIES]
    lines = ['id | date | sport | name | time | distance | avg HR | load | intensity | RPE']
    for a in shown:
        lines.append(' | '.join([
            str(a['id']), str(_activity_day(a)), a.get('sport_type') or 'Unknown', (a.get('name') or '')[:40],
            _fmt_duration(a.get('moving_time') or a.get('duration')), _fmt_km(a.get('distance')),
            _num(a.get('avg_hr')), _num(a.get('training_load')), _num(a.get('intensity'), 2), _num(a.get('rpe')),
        ]))
    if len(activities) > len(shown):
        lines.append(f'(Showing the {len(shown)} most recent of {len(activities)} workouts; ask for a shorter period '
                     'or use get_training_summary for totals.)')
    return '\n'.join(lines), f'{len(activities)} workout{"s" if len(activities) != 1 else ""}'


def _zones(bounds, times, unit):
    """'Z1 (up to 147 bpm): 50 min' lines for zones with time in them."""
    if not isinstance(times, list) or not any(times):
        return None
    parts, lower = [], None
    for i, seconds in enumerate(times):
        upper = bounds[i] if isinstance(bounds, list) and i < len(bounds) else None
        if unit == '%' and upper is not None and upper >= 999:
            label = f'above {lower:g}%' if lower is not None else ''
        elif upper is not None:
            label = f'{lower:g}-{upper:g} {unit}' if lower is not None else f'up to {upper:g} {unit}'
        else:
            label = ''
        if seconds:
            parts.append(f"Z{i + 1}{f' ({label})' if label else ''}: {_fmt_duration(seconds)}")
        lower = upper
    return ', '.join(parts)


def get_activity_details_tool(user_id, arguments):
    try:
        activity_id = int(arguments.get('activity_id'))
    except (TypeError, ValueError):
        raise ToolInputError('activity_id must be a workout id (a number).')
    activity = get_activity(activity_id)
    if not activity or activity['user_id'] != user_id or activity['hidden'] or activity.get('merged_into'):
        raise ToolInputError(f'There is no workout with id {activity_id}. Use get_activities to find ids.')
    raw = json.loads(activity['raw_summary']) if activity.get('raw_summary') else {}
    sport = activity.get('sport_type') or 'Unknown'
    lines = [f"Workout {activity_id}: {activity.get('name') or ''} ({sport}) on {_activity_day(activity)}"]

    def add(label, value):
        if value not in (None, '', '-'):
            lines.append(f"{label}: {value}")

    add('Moving time', _fmt_duration(activity.get('moving_time') or activity.get('duration')))
    add('Elapsed time', _fmt_duration(activity.get('duration')))
    add('Distance', _fmt_km(activity.get('distance')) if activity.get('distance') else None)
    if activity.get('elevation_gain'):
        loss = raw.get('total_elevation_loss')
        add('Elevation', f"+{activity['elevation_gain']:.0f} m" + (f" / -{loss:.0f} m" if loss else ''))
    if activity.get('avg_hr'):
        add('Heart rate', f"average {activity['avg_hr']:.0f} bpm, max {_num(activity.get('max_hr'))} bpm")
    if raw.get('pace') and 'run' in sport.lower():
        add('Pace', _fmt_pace(raw['pace'], 1000, '/km'))
        add('Grade adjusted pace', _fmt_pace(raw.get('gap'), 1000, '/km') if raw.get('gap') else None)
    if activity.get('avg_power'):
        add('Power', f"average {activity['avg_power']:.0f} W"
            + (f", normalized {activity['normalized_power']:.0f} W" if activity.get('normalized_power') else ''))
    add('Cadence', _num(activity.get('avg_cadence')) if activity.get('avg_cadence') else None)
    if raw.get('average_stride'):
        add('Stride length', f"{raw['average_stride']:.2f} m")
    load = _num(activity.get('training_load'))
    if raw.get('hr_load') or raw.get('pace_load'):
        load += f" (heart rate load {_num(raw.get('hr_load'))}, pace load {_num(raw.get('pace_load'))})"
    add('Training load', load)
    add('Intensity', _num(activity.get('intensity'), 2) if activity.get('intensity') is not None else None)
    add('Perceived effort (RPE)', _num(activity.get('rpe')) if activity.get('rpe') else None)
    add('Feel (1 best, 5 worst)', _num(activity.get('feel')) if activity.get('feel') else None)
    add('Time in heart rate zones', _zones(raw.get('icu_hr_zones'), raw.get('icu_hr_zone_times'), 'bpm'))
    add('Time in pace zones (% of threshold pace)', _zones(raw.get('pace_zones'), raw.get('pace_zone_times'), '%'))
    add('Time in grade adjusted pace zones', _zones(raw.get('pace_zones'), raw.get('gap_zone_times'), '%'))
    if raw.get('interval_summary'):
        add('Intervals or laps', '; '.join(str(i) for i in raw['interval_summary'][:10]))
    if raw.get('description'):
        add('Athlete notes', str(raw['description'])[:500])
    return '\n'.join(lines), f"{activity.get('name') or sport}"


def _period_key(day, period):
    if period == 'month':
        return day.replace(day=1)
    return day - timedelta(days=day.weekday())


def get_training_summary_tool(user_id, arguments):
    start, end = _dates(arguments)
    period = arguments.get('period') if arguments.get('period') in ('week', 'month') else 'week'
    activities = _activities_between(user_id, start, end, arguments.get('sport'))
    groups = defaultdict(list)
    for a in activities:
        groups[_period_key(_activity_day(a), period)].append(a)
    keys = []
    key = _period_key(start, period)
    while key <= end and len(keys) < MAX_PERIODS:
        keys.append(key)
        key = (key + timedelta(days=32)).replace(day=1) if period == 'month' else key + timedelta(days=7)

    lines = [f'{period} starting | sessions | time | distance | load | sports | time in HR zones Z1-2 / Z3 / Z4+']
    for key in keys:
        acts = groups.get(key, [])
        sports = defaultdict(int)
        zones = [0.0, 0.0, 0.0]
        for a in acts:
            sports[a.get('sport_type') or 'Unknown'] += 1
            times = json.loads(a['hr_zone_times']) if a.get('hr_zone_times') else []
            for i, seconds in enumerate(times):
                zones[0 if i < 2 else 1 if i == 2 else 2] += seconds or 0
        total_zone = sum(zones)
        zone_text = ' / '.join(f'{100 * z / total_zone:.0f}%' for z in zones) if total_zone else '-'
        load = sum(a.get('training_load') or 0 for a in acts)
        lines.append(' | '.join([
            key.isoformat(), str(len(acts)),
            _fmt_duration(sum(a.get('moving_time') or a.get('duration') or 0 for a in acts)),
            _fmt_km(sum(a.get('distance') or 0 for a in acts)), f'{load:.0f}' if load else '-',
            ', '.join(f'{n}x {s}' for s, n in sorted(sports.items())) or '-', zone_text,
        ]))
    return '\n'.join(lines), f'{len(keys)} {period}{"s" if len(keys) != 1 else ""}'


def _wellness_rows(user_id, start, end):
    return [w for w in _merge_wellness(get_daily_wellness(user_id, start=start.isoformat(), end=end.isoformat()))]


def _sample(rows):
    """Daily rows for short periods; one row per week (the last day) for long ones."""
    if len(rows) <= MAX_DAILY_ROWS:
        return rows, 'day'
    rows = sorted(rows, key=lambda r: r['date'])
    return [r for i, r in enumerate(rows) if i % 7 == 6 or i == len(rows) - 1][::-1], 'week'


def get_fitness_trend_tool(user_id, arguments):
    start, end = _dates(arguments)
    rows = [r for r in _wellness_rows(user_id, start, end) if r.get('ctl') is not None]
    if not rows:
        return 'No fitness data in this period.', 'no data'
    rows, step = _sample(rows)
    lines = [f'date | fitness (CTL) | fatigue (ATL) | form | ramp rate  (one row per {step})']
    for r in rows:
        form = r['ctl'] - r['atl'] if r.get('atl') is not None else None
        lines.append(' | '.join([r['date'], _num(r['ctl']), _num(r.get('atl')),
                                 f'{form:+.0f}' if form is not None else '-', _num(r.get('ramp_rate'), 1)]))
    return '\n'.join(lines), f'{len(rows)} {step}s'


def get_wellness_tool(user_id, arguments):
    user = get_user_by_id(user_id) or {}
    if user.get('wellness_opt_in') != 1:
        return "The athlete doesn't share wellness data.", 'not shared'
    start, end = _dates(arguments)
    rows = [r for r in _wellness_rows(user_id, start, end)
            if any(r.get(k) is not None for k in ('resting_hr', 'hrv', 'sleep_secs', 'weight'))]
    if not rows:
        return 'No wellness data in this period.', 'no data'
    averages = []
    for key, label in (('resting_hr', 'resting HR'), ('hrv', 'HRV'), ('sleep_secs', 'sleep'), ('sleep_score', 'sleep score')):
        values = [r[key] for r in rows if r.get(key) is not None]
        if values:
            mean = sum(values) / len(values)
            averages.append(f"{label} {_fmt_duration(mean) if key == 'sleep_secs' else f'{mean:.0f}'}")
    sampled, step = _sample(rows)
    lines = [f"Averages over the period: {', '.join(averages)}",
             f'date | resting HR | HRV | sleep | sleep score | sleep quality (1 best) | weight  (one row per {step})']
    for r in sampled:
        lines.append(' | '.join([
            r['date'], _num(r.get('resting_hr')), _num(r.get('hrv')),
            _fmt_duration(r['sleep_secs']) if r.get('sleep_secs') else '-',
            _num(r.get('sleep_score')), _num(r.get('sleep_quality')), _num(r.get('weight'), 1),
        ]))
    return '\n'.join(lines), f'{len(rows)} days'


EXECUTORS = {
    'get_activities': get_activities_tool,
    'get_activity_details': get_activity_details_tool,
    'get_training_summary': get_training_summary_tool,
    'get_fitness_trend': get_fitness_trend_tool,
    'get_wellness': get_wellness_tool,
}


def run_tool(user_id, name, arguments):
    """Returns (text for the model, short summary for the chat). Errors become text the model can act on."""
    try:
        return EXECUTORS[name](user_id, arguments or {})
    except ToolInputError as e:
        return f'Error: {e}', 'invalid request'

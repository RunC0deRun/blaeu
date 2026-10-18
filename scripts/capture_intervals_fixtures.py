#!/usr/bin/env python3
"""
Records real Intervals.icu API responses as redacted test fixtures.

    read -s INTERVALS_API_KEY && export INTERVALS_API_KEY
    python scripts/capture_intervals_fixtures.py --athlete-id i12345

Writes to tests/fixtures/intervals/ by default. See docs/testing-with-real-data.md for
what is captured and how it is redacted. Review the files before committing them.
"""
import argparse
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

API = "https://intervals.icu/api/v1"
REQUEST_DELAY_SECONDS = 1.0
MAX_RETRIES = 3
STREAM_POINTS = 50
WELLNESS_DAYS = 14
MAX_DETAIL_FETCHES = 25
KINDS = ('outdoor_gps', 'indoor', 'no_gps', 'strava')

# Where redacted GPS tracks are moved to: the open Atlantic, far from anyone's real start
FAKE_ORIGIN = (10.0, -30.0)

REDACTED = 'REDACTED'

# Free-text and identifying fields, replaced wherever they occur
TEXT_KEYS = {
    'description', 'notes', 'note', 'comments', 'comment', 'bio', 'website',
    'email', 'firstname', 'lastname', 'first_name', 'last_name', 'sex', 'dob', 'date_of_birth',
    'city', 'state', 'country', 'location', 'address', 'phone',
    'device_name', 'external_id', 'file_name', 'filename', 'oauth_client_name',
    'profile', 'profile_medium', 'avatar', 'photo', 'photos', 'strava_id', 'garmin_id',
    'icu_api_key', 'api_key', 'gear', 'gear_id', '_note',
}
# Fields holding the athlete id
ATHLETE_ID_KEYS = {'athlete_id', 'icu_athlete_id', 'athleteId'}
# Fields holding a [lat, lon] pair
COORD_PAIR_KEYS = {'start_latlng', 'end_latlng'}
# Single coordinate fields
COORD_KEYS = {'lat', 'lng', 'lon', 'latitude', 'longitude'}
# Time zones reveal roughly where someone lives; the tests don't need the real one
TIMEZONE_KEYS = {'timezone', 'tz'}
# Profile fields worth keeping; everything else in the profile is dropped
PROFILE_KEEP = {'id', 'icu_resting_hr', 'timezone', 'sportSettings'}
# Wellness fields that describe training, not the body: kept as-is
WELLNESS_TRAINING_KEYS = {'id', 'ctl', 'atl', 'rampRate', 'ctlLoad', 'atlLoad', 'sportInfo', 'updated'}


class RedactionError(Exception):
    pass


class Redactor:
    """Replaces identifying values and remembers originals so the output can be checked."""

    def __init__(self, athlete_id, rng=None):
        self.athlete_id = str(athlete_id)
        self.rng = rng or random.Random()
        self.activity_ids = {}
        self.secrets = {self.athlete_id}
        self.coord_offsets = {}

    def fake_activity_id(self, real_id):
        real_id = str(real_id)
        if real_id not in self.activity_ids:
            self.activity_ids[real_id] = f"i{1001 + len(self.activity_ids)}"
            self.secrets.add(real_id)
        return self.activity_ids[real_id]

    def remember_secret(self, value):
        if isinstance(value, str) and len(value.strip()) >= 4 and value.strip() != REDACTED:
            self.secrets.add(value.strip())

    def set_track_origin(self, fake_id, lat, lon):
        self.coord_offsets[fake_id] = (FAKE_ORIGIN[0] - lat, FAKE_ORIGIN[1] - lon)
        # Remember the real start (3 decimals is ~100 m) to detect leaks
        self.secrets.add(f"{lat:.3f}")
        self.secrets.add(f"{lon:.3f}")

    def shift(self, fake_id, lat, lon):
        d_lat, d_lon = self.coord_offsets.get(fake_id, (None, None))
        if d_lat is None:
            return FAKE_ORIGIN
        return (round(lat + d_lat, 6), round(lon + d_lon, 6))

    def redact(self, value, fake_id=None, key=None):
        """Recursively redacts a JSON value. fake_id selects the GPS offset to apply."""
        if isinstance(value, dict):
            return {k: self._redact_field(k, v, fake_id) for k, v in value.items()}
        if isinstance(value, list):
            return [self.redact(v, fake_id, key) for v in value]
        if isinstance(value, str) and value.strip() in (self.athlete_id, self.athlete_id.lstrip('i')):
            return 'a0'
        if isinstance(value, int) and not isinstance(value, bool) and str(value) == self.athlete_id.lstrip('i'):
            return 0
        return value

    def _redact_field(self, key, value, fake_id):
        if key in TEXT_KEYS:
            self.remember_secret(value if isinstance(value, str) else None)
            return None if value is None else REDACTED
        if key in ATHLETE_ID_KEYS:
            return 'a0'
        if key == 'name':
            # Not remembered as a secret: names like "Run" also occur as sport types
            return f"Activity {fake_id}" if fake_id else REDACTED
        if key in TIMEZONE_KEYS and isinstance(value, str):
            return 'UTC'
        if key in COORD_PAIR_KEYS and isinstance(value, list) and len(value) == 2 \
                and all(isinstance(v, (int, float)) for v in value):
            return list(self.shift(fake_id, value[0], value[1]))
        if key in COORD_KEYS and isinstance(value, (int, float)):
            return None
        return self.redact(value, fake_id, key)

    def check(self, payload):
        """Fails if any original identifier is still present in the serialised output."""
        text = json.dumps(payload)
        leaks = sorted(s for s in self.secrets if s and re.search(re.escape(s), text))
        if leaks:
            raise RedactionError(
                f"Redacted output still contains original value(s) {', '.join(repr(s) for s in leaks)}; "
                "not writing fixtures. Add the field holding them to TEXT_KEYS."
            )


def perturb_body_values(record, rng):
    """Scales body measurements by a random ±15% so no real value is stored."""
    out = {}
    for key, value in record.items():
        if key in WELLNESS_TRAINING_KEYS:
            out[key] = value
        elif isinstance(value, bool) or value is None:
            out[key] = value
        elif isinstance(value, int):
            out[key] = int(round(value * rng.uniform(0.85, 1.15)))
        elif isinstance(value, float):
            out[key] = round(value * rng.uniform(0.85, 1.15), 2)
        elif isinstance(value, str):
            out[key] = REDACTED
        else:
            out[key] = None
    return out


def classify(summary):
    if summary.get('_note') or (
        str(summary.get('source', '')).upper() == 'STRAVA' and summary.get('moving_time') is None
    ):
        return 'strava'
    stream_types = summary.get('stream_types')
    has_gps = isinstance(stream_types, list) and 'latlng' in stream_types
    indoor = summary.get('trainer') is True or str(summary.get('type', '')).startswith('Virtual')
    if indoor:
        return 'indoor'
    return 'outdoor_gps' if has_gps else 'no_gps'


def make_getter(api_key, delay=REQUEST_DELAY_SECONDS, sleep=time.sleep):
    """Sequential, throttled GET with backoff on 429 and 5xx."""
    session = requests.Session()
    session.auth = ('API_KEY', api_key)
    state = {'first': True}

    def get(path, params=None):
        for attempt in range(MAX_RETRIES + 1):
            if not state['first']:
                sleep(delay)
            state['first'] = False
            resp = session.get(f"{API}{path}", params=params, timeout=30)
            if (resp.status_code == 429 or resp.status_code >= 500) and attempt < MAX_RETRIES:
                retry_after = resp.headers.get('Retry-After')
                try:
                    wait = min(float(retry_after), 300) if retry_after else 10 * (2 ** attempt)
                except ValueError:
                    wait = 10 * (2 ** attempt)
                print(f"  HTTP {resp.status_code}, waiting {wait:.0f}s", file=sys.stderr)
                sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
    return get


def truncate_streams(streams, redactor, fake_id):
    out = []
    for stream in streams if isinstance(streams, list) else []:
        stream = dict(stream)
        for key in ('data', 'data2'):
            if isinstance(stream.get(key), list):
                stream[key] = stream[key][:STREAM_POINTS]
        if stream.get('type') == 'latlng' and stream.get('data') and stream.get('data2'):
            lats, lons = stream['data'], stream['data2']
            first = next(((a, b) for a, b in zip(lats, lons) if a is not None and b is not None), None)
            if first:
                redactor.set_track_origin(fake_id, *first)
            shifted = [redactor.shift(fake_id, a, b) if a is not None and b is not None else (None, None)
                       for a, b in zip(lats, lons)]
            stream['data'] = [p[0] for p in shifted]
            stream['data2'] = [p[1] for p in shifted]
        out.append(stream)
    return out


def capture(get, athlete_id, days=90, per_kind=2, rng=None, log=print):
    """
    Fetches and redacts a fixture set. Returns {filename: payload}; nothing is written.
    `get(path, params=None)` performs one API request and returns decoded JSON.
    """
    rng = rng or random.Random()
    redactor = Redactor(athlete_id, rng)

    log("Fetching athlete profile")
    profile = get(f"/athlete/{athlete_id}")
    for key in ('name', 'firstname', 'lastname', 'email', 'city', 'state', 'country'):
        redactor.remember_secret(profile.get(key))
    profile_out = {k: v for k, v in profile.items() if k in PROFILE_KEEP}
    profile_out['id'] = 'a0'
    if isinstance(profile_out.get('icu_resting_hr'), (int, float)):
        profile_out['icu_resting_hr'] = int(round(profile_out['icu_resting_hr'] * rng.uniform(0.85, 1.15)))
    profile_out = redactor.redact(profile_out)

    oldest = (datetime.now(timezone.utc) - timedelta(days=days)).strftime('%Y-%m-%d')
    log(f"Fetching activity list since {oldest}")
    activities = get(f"/athlete/{athlete_id}/activities", params={'oldest': oldest}) or []
    activities.sort(key=lambda a: a.get('start_date') or a.get('start_date_local') or '', reverse=True)

    chosen = {kind: [] for kind in KINDS}
    seen_types = {kind: set() for kind in KINDS}
    list_entries, details, streams_out, manifest = [], {}, {}, {}
    state = {'fetched': 0}
    detail_cache = {}

    def take(act, require_new_type):
        """Fetches and keeps one activity if its kind still has room. Returns True if kept."""
        listed_kind = classify(act)
        if len(chosen[listed_kind]) >= per_kind and isinstance(act.get('stream_types'), list):
            return False
        if require_new_type and act.get('type') in seen_types[listed_kind]:
            return False
        real_id = str(act.get('id'))
        if listed_kind == 'strava':
            detail = act  # the API returns no data for these
        elif real_id in detail_cache:
            detail = detail_cache[real_id]
        else:
            if state['fetched'] >= MAX_DETAIL_FETCHES:
                return False
            log(f"Fetching activity ({act.get('type')})")
            detail = detail_cache[real_id] = get(f"/activity/{real_id}")
            state['fetched'] += 1
        kind = classify(detail)
        if len(chosen[kind]) >= per_kind or (require_new_type and act.get('type') in seen_types[kind]):
            return False

        fake_id = redactor.fake_activity_id(real_id)
        stream_types = detail.get('stream_types')
        if kind != 'strava' and (not isinstance(stream_types, list) or 'latlng' in stream_types):
            log("  fetching streams")
            raw_streams = get(f"/activity/{real_id}/streams.json", params={'types': 'time,latlng,altitude'})
            streams_out[fake_id] = truncate_streams(raw_streams, redactor, fake_id)

        chosen[kind].append(fake_id)
        seen_types[kind].add(act.get('type'))
        details[fake_id] = redactor.redact(dict(detail, id=fake_id), fake_id)
        list_entries.append(redactor.redact(dict(act, id=fake_id), fake_id))
        manifest[fake_id] = {'kind': kind, 'type': act.get('type')}
        return True

    # First pass prefers a different sport type per sample; the second fills remaining slots
    taken = set()
    for require_new_type in (True, False):
        for act in activities:
            if all(len(chosen[k]) >= per_kind for k in KINDS):
                break
            if str(act.get('id')) not in taken and take(act, require_new_type):
                taken.add(str(act.get('id')))

    newest = datetime.now(timezone.utc).date()
    log(f"Fetching wellness for the last {WELLNESS_DAYS} days")
    wellness = get(f"/athlete/{athlete_id}/wellness", params={
        'oldest': (newest - timedelta(days=WELLNESS_DAYS)).isoformat(), 'newest': newest.isoformat()
    }) or []
    wellness_out = [redactor.redact(perturb_body_values(r, rng)) for r in wellness if isinstance(r, dict)]

    files = {
        'athlete.json': profile_out,
        'activities_list.json': list_entries,
        'wellness.json': wellness_out,
        'manifest.json': {
            'captured_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'activities': manifest,
            'missing_kinds': [k for k in KINDS if not chosen[k]],
        },
    }
    for fake_id, detail in details.items():
        files[f'activity_{fake_id}.json'] = detail
    for fake_id, streams in streams_out.items():
        files[f'streams_{fake_id}.json'] = streams

    redactor.check(files)
    return files


def write_fixtures(files, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    for old in os.listdir(out_dir):
        if old.endswith('.json'):
            os.remove(os.path.join(out_dir, old))
    for name, payload in sorted(files.items()):
        with open(os.path.join(out_dir, name), 'w', encoding='utf-8') as f:
            json.dump(payload, f, indent=2, sort_keys=True)
            f.write('\n')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--athlete-id', required=True, help="Intervals.icu athlete id, e.g. i12345")
    parser.add_argument('--days', type=int, default=90, help="How far back to look for sample activities (default 90)")
    parser.add_argument('--per-kind', type=int, default=2, help="Activities to keep per kind (default 2)")
    parser.add_argument('--out', default=os.path.join('tests', 'fixtures', 'intervals'), help="Output directory")
    args = parser.parse_args(argv)

    api_key = os.environ.get('INTERVALS_API_KEY')
    if not api_key:
        parser.error("Set INTERVALS_API_KEY in the environment (not on the command line).")

    try:
        files = capture(make_getter(api_key), args.athlete_id, days=args.days, per_kind=args.per_kind)
    except RedactionError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except requests.exceptions.HTTPError as e:
        print(f"Error: Intervals.icu request failed: {e}", file=sys.stderr)
        return 1

    write_fixtures(files, args.out)
    manifest = files['manifest.json']
    print(f"\nWrote {len(files)} files to {args.out}")
    for fake_id, info in manifest['activities'].items():
        print(f"  {fake_id}: {info['kind']} ({info['type']})")
    if manifest['missing_kinds']:
        print(f"No samples found for: {', '.join(manifest['missing_kinds'])} (try a larger --days)")
    print("\nReview the files before committing them: heart rate, power and dates are kept.")
    return 0


if __name__ == '__main__':
    sys.exit(main())

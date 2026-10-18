# Testing with real data

Unit tests use mocked API responses. Before a change that touches syncing or the database
reaches production, test it in two more ways, neither of which needs a commit:

1. **A test instance**: the current working tree, built as a Docker image and run on a
   *copy* of the production data.
2. **Recorded API responses**: real Intervals.icu responses, redacted and saved as test
   fixtures, so the test suite checks against what the API really returns.

---

## 1. Test instance on a copy of production data

`docker-compose.test.yml` builds the working tree as `blaeu:test` and runs it as a separate
Compose project (`blaeu-test`) with:

| | Production | Test instance |
|---|---|---|
| Port | 5000 | **5101** (override with `BLAEU_TEST_PORT`) |
| Data volume | your production volume | **`blaeu_test_data`** |
| Background auto-sync | per user setting | **always off** (`BLAEU_DISABLE_AUTO_SYNC=true`) |
| Session cookie | secure (HTTPS) | plain HTTP on localhost |

Auto-sync is off because the copy holds the same Garmin and Intervals.icu connections as
production. Two instances syncing the same accounts double the API load, and Garmin
rate-limits aggressively.

> [!WARNING]
> Never point the test instance at the production volume. Migrations change the database
> and can't easily be undone. Always work on a copy.

### Step 1: find the production volume

On the host that runs production:

```bash
docker volume ls --filter name=blaeu
```

The volume is usually called `<project>_blaeu_data`, for example `blaeu_blaeu_data`. Below
it's referred to as `PROD_VOLUME`.

### Step 2: copy the data

Stop production briefly while copying, so the SQLite database isn't copied in the middle of
a write. It takes a few seconds; the caches are skipped because the test instance rebuilds
them.

**Production runs on the same machine:**

```bash
docker stop <production-container>
docker volume create blaeu_test_data
docker run --rm -v PROD_VOLUME:/from:ro -v blaeu_test_data:/to alpine \
  sh -c 'find /to -mindepth 1 -delete && cd /from && tar --exclude=./tiles_cache --exclude=./osmnx_cache --exclude=./poster_maps -cf - . | tar -C /to -xpf -'
docker start <production-container>
```

**Production runs on another host:** export there, then copy the archive over:

```bash
# On the production host
docker stop <production-container>
docker run --rm -v PROD_VOLUME:/from:ro alpine \
  tar -C /from --exclude=./tiles_cache --exclude=./osmnx_cache --exclude=./poster_maps -czf - . > blaeu-data.tgz
docker start <production-container>

# On your machine
scp production-host:blaeu-data.tgz .
docker volume create blaeu_test_data
docker run --rm -i -v blaeu_test_data:/to alpine \
  sh -c 'find /to -mindepth 1 -delete && tar -C /to -xzpf -' < blaeu-data.tgz
```

The archive contains API keys, Garmin session tokens and health data. `*.tgz` is in
`.gitignore`, but delete the archive once it's restored.

### Step 3: build and start the test instance

From the repository root, with your uncommitted changes in place:

```bash
docker compose -f docker-compose.yml -f docker-compose.test.yml up -d --build
```

Open <http://localhost:5101> and log in with your production account. The migrations run
on startup; check the logs for errors:

```bash
docker compose -f docker-compose.yml -f docker-compose.test.yml logs web
```

### Step 4: check the result

Quick database checks from inside the container:

```bash
docker compose -f docker-compose.yml -f docker-compose.test.yml exec web python -c "
import app
import db
c = db.get_db()
print('routes:', c.execute('SELECT COUNT(*) FROM routes').fetchone()[0])
print('routes without activity:', c.execute('SELECT COUNT(*) FROM routes r LEFT JOIN activities a ON a.route_id = r.id WHERE a.id IS NULL').fetchone()[0])
for row in c.execute('SELECT source, COUNT(*), SUM(has_gps), SUM(raw_summary IS NOT NULL) FROM activities GROUP BY source'):
    print('activities from %s: %d total, %d with GPS, %d enriched' % tuple(row))
"
```

`routes without activity` should be `0`.

To run one Intervals.icu sync by hand (auto-sync is off), use your user id. It's `1` if
you were the first user:

```bash
docker compose -f docker-compose.yml -f docker-compose.test.yml exec web python -c "
import logging; logging.basicConfig(level=logging.INFO)
import app  # load the app first: the blueprints import it, and importing them directly fails
from blueprints.intervals import sync_user_intervals_activities_in_background
sync_user_intervals_activities_in_background(1)
"
```

Then repeat the database check and look at the imported activities in the UI.

To test the history backfill, start one for your user. The background worker then
processes one 30-day window per minute (also on the test instance; only auto-sync is off):

```bash
docker compose -f docker-compose.yml -f docker-compose.test.yml exec web python -c "
import app
from blueprints.intervals_backfill import start_backfill
print(start_backfill(1))
"
```

Follow its progress (or use `GET /api/intervals/backfill` while logged in):

```bash
docker compose -f docker-compose.yml -f docker-compose.test.yml exec web python -c "
import app, db
from blueprints.intervals_backfill import job_progress, SOURCE, KIND
print(job_progress(db.get_latest_sync_job(1, SOURCE, KIND)))
"
```

A backfill job copied over from production also continues on the test instance, with the
same slow pace.

Avoid Garmin syncs on the test instance unless you're testing Garmin itself. If the
production instance later asks you to reconnect Garmin, reconnect there.

### Rebuilding after code changes, and starting over

```bash
# Rebuild with the latest working tree (keeps the test data)
docker compose -f docker-compose.yml -f docker-compose.test.yml up -d --build

# Remove the test instance
docker compose -f docker-compose.yml -f docker-compose.test.yml down

# Delete the test data too; then copy fresh data again (step 2)
docker volume rm blaeu_test_data
```

`blaeu_test_data` is declared `external`: Compose uses it but never creates or deletes it,
so `down -v` leaves it alone. Delete it by name as above, and double-check the name: the
production volume is the one you must never remove.

If the port is taken (for example by another Blaeu container), pick another one:

```bash
BLAEU_TEST_PORT=5202 docker compose -f docker-compose.yml -f docker-compose.test.yml up -d --build
```

---

## 2. Recording real Intervals.icu responses as fixtures

`scripts/capture_intervals_fixtures.py` downloads a small, representative set of real
responses and redacts them:

- **Activities:** up to two each of outdoor GPS workouts, indoor workouts, workouts without
  GPS (strength, yoga…) and Strava-sourced activities, preferring different sport types.
  Full detail, plus streams cut down to 50 points.
- **Athlete profile:** only the id, resting HR and per-sport settings (thresholds and zones).
- **Wellness:** the last 14 days.

Run it with your API key in the environment, never on the command line:

```bash
read -s INTERVALS_API_KEY && export INTERVALS_API_KEY
python scripts/capture_intervals_fixtures.py --athlete-id i12345
```

Requests are sequential with a 1-second pause and back off on HTTP 429, so a run takes
around half a minute.

### What gets redacted

| Data | Treatment |
|---|---|
| Athlete and activity ids | replaced with fake ids (`a0`, `i1001`, …), consistent across files |
| Names, descriptions, notes, email, location fields, gear, device and external ids | replaced with `REDACTED` or a generic name |
| GPS coordinates | moved to a fixed spot; the shape of the first 50 points is kept |
| Time zones | set to `UTC` |
| Body data in wellness (weight, HRV, sleep, resting HR…) | randomly scaled by ±15%, so the type and rough size stay realistic |
| Training data (load, HR, power, zones, CTL/ATL, dates) | kept, since that's what the tests check |

Before writing, the script checks the output for any original id, name, email or
coordinate it saw, and refuses to write if one remains. **Still read the files before you
commit them** (`git diff --stat tests/fixtures/` and a look at a few files): heart rate,
power and training dates remain, and the repository may be public.

### Using the fixtures

`tests/test_intervals_fixtures.py` runs automatically when `tests/fixtures/intervals/`
exists, and is skipped otherwise. For every recorded activity it checks that:

- normalisation finds the basic fields (sport type, start time, duration);
- HR and power are picked up whenever the activity has those streams;
- importing creates a route exactly when there is GPS, and never creates duplicates;
- the wellness and athlete-profile fields map to Blaeu's columns.

When a check fails, the field mapping in `blueprints/intervals.py` doesn't match the real
API, which is exactly what these fixtures are meant to catch.

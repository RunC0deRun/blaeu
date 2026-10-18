"""
Runs the Intervals.icu import against recorded real responses. Skipped until fixtures
are captured with scripts/capture_intervals_fixtures.py (see docs/testing-with-real-data.md).
"""
import os

import pytest

import db
from blueprints import intervals
from tests import intervals_fixture_checks as checks

FIXTURE_DIR = os.environ.get(
    'BLAEU_INTERVALS_FIXTURES', os.path.join(os.path.dirname(__file__), 'fixtures', 'intervals')
)
HAS_FIXTURES = os.path.exists(os.path.join(FIXTURE_DIR, 'manifest.json'))

pytestmark = pytest.mark.skipif(not HAS_FIXTURES, reason="no recorded Intervals.icu fixtures")

ACTIVITY_IDS = sorted(checks.manifest(FIXTURE_DIR)['activities']) if HAS_FIXTURES else []


@pytest.fixture
def user_id(authed_client):
    return db.get_user_by_username('test_user')['id']


@pytest.fixture(autouse=True)
def no_pause(monkeypatch):
    monkeypatch.setattr(intervals, 'REQUEST_DELAY_SECONDS', 0)
    monkeypatch.setattr(intervals.time, 'sleep', lambda s: None)


@pytest.mark.parametrize('fake_id', ACTIVITY_IDS)
def test_recorded_activity(fake_id, user_id):
    checks.check_activity(FIXTURE_DIR, fake_id, user_id)


def test_recorded_wellness():
    checks.check_wellness(FIXTURE_DIR)


def test_recorded_athlete_thresholds(user_id):
    checks.check_athlete(FIXTURE_DIR, user_id)

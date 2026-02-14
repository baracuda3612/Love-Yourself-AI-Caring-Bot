import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("DATABASE_URL", "postgresql://test-user:test-pass@localhost:5432/test-db")
os.environ.setdefault("OPENAI_API_KEY", "test-key")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.append(str(ROOT))

from app.telemetry import get_skip_streak


def _mock_db_with_events(events):
    db = MagicMock()
    query = db.query.return_value
    filtered = query.filter.return_value
    ordered = filtered.order_by.return_value
    limited = ordered.limit.return_value
    limited.all.return_value = events
    return db


def test_get_skip_streak_counts_consecutive_skip_like_events():
    db = _mock_db_with_events([
        ("task_skipped",),
        ("task_ignored",),
        ("task_failed",),
    ])

    assert get_skip_streak(db, user_id=7, limit=9) == 3


def test_get_skip_streak_stops_on_completed_reset_event():
    db = _mock_db_with_events([
        ("task_skipped",),
        ("task_skipped",),
        ("task_completed",),
        ("task_skipped",),
    ])

    assert get_skip_streak(db, user_id=7, limit=9) == 2

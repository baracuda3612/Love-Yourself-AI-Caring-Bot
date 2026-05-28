"""
Tests for plan_pause module (T5.1).

Verifies:
  - pause_plan sets is_paused=True, increments pause_count, sets ACTIVE_PAUSED
  - resume_plan sets is_paused=False, sets ACTIVE
  - Double-pause raises PlanAlreadyPausedError
  - Resume when not paused raises PlanNotPausedError
  - Pause when not ACTIVE raises PlanNotActiveError
  - pause_plan does NOT touch plan steps
  - resume_plan does not decrement pause_count
"""

import sys
from unittest.mock import MagicMock

import pytest

# ── Stub app.db before any module tries to import it ─────────────────────────
# plan_pause.py lazily imports (from app.db import User, UserProfile) inside
# function bodies. Without this stub the SQLAlchemy engine init fires and
# psycopg2 (not installed in the test runner) causes ModuleNotFoundError.
if "app.db" not in sys.modules:
    sys.modules["app.db"] = MagicMock()  # type: ignore[assignment]

from app.plan_pause import (  # noqa: E402 — must be after sys.modules patch
    PlanAlreadyPausedError,
    PlanNotActiveError,
    PlanNotPausedError,
    pause_plan,
    resume_plan,
)


# ── Helpers ────────────────────────────────────────────────────────────────────


def _make_user(current_state: str = "ACTIVE") -> MagicMock:
    user = MagicMock()
    user.id = 1
    user.current_state = current_state
    return user


def _make_profile(is_paused: bool = False, pause_count: int = 0) -> MagicMock:
    profile = MagicMock()
    profile.is_paused = is_paused
    profile.pause_count = pause_count
    return profile


def _make_db(user: MagicMock, profile: MagicMock) -> MagicMock:
    """
    Returns a mock Session whose .query().filter().first() calls return
    user on the first call and profile on the second.
    """
    db = MagicMock()
    db.query.return_value.filter.return_value.first.side_effect = [user, profile]
    return db


# ── pause_plan ────────────────────────────────────────────────────────────────


def test_pause_sets_is_paused_and_increments_count() -> None:
    user = _make_user("ACTIVE")
    profile = _make_profile(is_paused=False, pause_count=2)
    db = _make_db(user, profile)

    pause_plan(db, user_id=1)

    assert profile.is_paused is True
    assert profile.pause_count == 3
    assert user.current_state == "ACTIVE_PAUSED"
    db.flush.assert_called_once()


def test_pause_raises_when_not_active() -> None:
    user = _make_user("IDLE_FINISHED")
    profile = _make_profile()
    db = _make_db(user, profile)

    with pytest.raises(PlanNotActiveError):
        pause_plan(db, user_id=1)


def test_pause_raises_when_already_paused() -> None:
    user = _make_user("ACTIVE")
    profile = _make_profile(is_paused=True)
    db = _make_db(user, profile)

    with pytest.raises(PlanAlreadyPausedError):
        pause_plan(db, user_id=1)


def test_pause_does_not_touch_plan_steps() -> None:
    """Pause must not modify any plan steps — invariant 7."""
    user = _make_user("ACTIVE")
    profile = _make_profile()
    db = _make_db(user, profile)

    pause_plan(db, user_id=1)

    # db.query should only have been called with User and UserProfile —
    # never with AIPlanStep or AIPlanDay.
    queried_args = [str(call.args) for call in db.query.call_args_list]
    assert all("AIPlanStep" not in a and "AIPlanDay" not in a for a in queried_args), (
        "pause_plan must not query or modify AIPlanStep / AIPlanDay"
    )


# ── resume_plan ───────────────────────────────────────────────────────────────


def test_resume_clears_is_paused_and_sets_active() -> None:
    user = _make_user("ACTIVE_PAUSED")
    profile = _make_profile(is_paused=True, pause_count=1)
    db = _make_db(user, profile)

    resume_plan(db, user_id=1)

    assert profile.is_paused is False
    assert user.current_state == "ACTIVE"
    db.flush.assert_called_once()


def test_resume_does_not_decrement_pause_count() -> None:
    """pause_count is a monotonically increasing analytics counter — never decremented."""
    user = _make_user("ACTIVE_PAUSED")
    profile = _make_profile(is_paused=True, pause_count=3)
    db = _make_db(user, profile)

    resume_plan(db, user_id=1)

    assert profile.pause_count == 3  # unchanged


def test_resume_raises_when_not_paused() -> None:
    user = _make_user("ACTIVE")
    profile = _make_profile(is_paused=False)
    db = _make_db(user, profile)

    with pytest.raises(PlanNotPausedError):
        resume_plan(db, user_id=1)

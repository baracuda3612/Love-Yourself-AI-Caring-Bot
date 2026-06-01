"""
Tests for app/plan_runtime/tools.py (T5.6).

All tests use DummyUser / DummyProfile — no real DB or scheduler.

tools.py uses lazy imports (like plan_pause.py) so we stub app.db,
app.plan_drafts.service, app.plan_pause, and app.scheduler in sys.modules
BEFORE importing tools, then patch individual attributes on those stubs
per-test using unittest.mock.patch.
"""

from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import MagicMock, patch, call

import pytest


# ─── Stub heavy modules before any import of tools ───────────────────────────

def _make_stub(name: str) -> MagicMock:
    mod = MagicMock()
    sys.modules[name] = mod  # type: ignore[assignment]
    return mod


_db_stub = _make_stub("app.db")
_service_stub = _make_stub("app.plan_drafts.service")
_pause_stub = _make_stub("app.plan_pause")
_scheduler_stub = _make_stub("app.scheduler")
_finalization_stub = _make_stub("app.plan_finalization")
_time_slots_stub = _make_stub("app.time_slots")

# Now import is safe — no real DB engine fires
from app.plan_runtime import tools  # noqa: E402


# ─── Helpers ──────────────────────────────────────────────────────────────────


class DummyUser:
    def __init__(self, state: str = "IDLE_ONBOARDED") -> None:
        self.id = 1
        self.current_state = state


class DummyProfile:
    def __init__(
        self,
        day_time: str | None = "14:00",
        evening_time: str | None = None,
        evening_slot_collected: bool = False,
        is_paused: bool = False,
        pause_count: int = 0,
    ) -> None:
        self.user_id = 1
        slots: dict = {}
        if day_time:
            slots["DAY"] = day_time
        if evening_time:
            slots["EVENING"] = evening_time
        self.daily_time_slots = slots
        self.evening_slot_collected = evening_slot_collected
        self.is_paused = is_paused
        self.pause_count = pause_count


def _make_session_cm(query_side_effects: list) -> MagicMock:
    """
    Build a mock context manager for SessionLocal().
    query_side_effects is the list of values returned by successive
    .query().filter().first() calls within the session.
    """
    db = MagicMock()
    db.query.return_value.filter.return_value.first.side_effect = query_side_effects
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=db)
    cm.__exit__ = MagicMock(return_value=False)
    return cm, db


# ─── create_first_plan ────────────────────────────────────────────────────────


def test_create_first_plan_happy_path():
    """IDLE_ONBOARDED + day_time → plan created, state becomes ACTIVE, side effects called."""
    user = DummyUser("IDLE_ONBOARDED")
    profile = DummyProfile(day_time="13:00")
    fake_plan = MagicMock()
    fake_plan.id = 42

    cm, db = _make_session_cm([user, profile, user])

    _db_stub.SessionLocal.return_value = cm
    _db_stub.User = MagicMock()
    _db_stub.UserProfile = MagicMock()
    _service_stub.create_plan.return_value = fake_plan
    _finalization_stub.activate_plan_side_effects.reset_mock()

    result = tools.create_first_plan(user_id=1)

    assert result == {"status": "ok", "plan_type": "SHORT"}
    assert user.current_state == "ACTIVE"
    _service_stub.create_plan.assert_called_once()
    call_kwargs = _service_stub.create_plan.call_args
    assert call_kwargs.kwargs.get("plan_type") == "SHORT" or call_kwargs.args[2] == "SHORT"
    _finalization_stub.activate_plan_side_effects.assert_called_once_with(42, 1)


def test_create_first_plan_wrong_state():
    """Non-IDLE_ONBOARDED state raises ValueError."""
    user = DummyUser("IDLE_NEW")
    profile = DummyProfile()

    cm, db = _make_session_cm([user, profile])
    _db_stub.SessionLocal.return_value = cm
    _db_stub.User = MagicMock()
    _db_stub.UserProfile = MagicMock()

    with pytest.raises(ValueError, match="IDLE_ONBOARDED"):
        tools.create_first_plan(user_id=1)


def test_create_first_plan_no_day_time():
    """IDLE_ONBOARDED but daily_time_slots has no DAY key → ValueError."""
    user = DummyUser("IDLE_ONBOARDED")
    profile = DummyProfile(day_time=None)

    cm, db = _make_session_cm([user, profile])
    _db_stub.SessionLocal.return_value = cm
    _db_stub.User = MagicMock()
    _db_stub.UserProfile = MagicMock()

    with pytest.raises(ValueError, match="day_time required"):
        tools.create_first_plan(user_id=1)


# ─── create_followup_plan ─────────────────────────────────────────────────────


def test_create_followup_plan_short():
    """IDLE_FINISHED → SHORT plan created, state becomes ACTIVE, side effects called."""
    user = DummyUser("IDLE_FINISHED")
    profile = DummyProfile(day_time="14:00")
    fake_plan = MagicMock()
    fake_plan.id = 7

    cm, db = _make_session_cm([user, profile, user])
    _db_stub.SessionLocal.return_value = cm
    _db_stub.User = MagicMock()
    _db_stub.UserProfile = MagicMock()
    _service_stub.create_plan.return_value = fake_plan
    _finalization_stub.activate_plan_side_effects.reset_mock()

    result = tools.create_followup_plan(user_id=1, plan_type="SHORT")

    assert result == {"status": "ok", "plan_type": "SHORT"}
    assert user.current_state == "ACTIVE"
    _finalization_stub.activate_plan_side_effects.assert_called_once_with(7, 1)


def test_create_followup_plan_medium_needs_evening():
    """IDLE_FINISHED + evening_slot_collected=False → needs_evening_time (no raise)."""
    user = DummyUser("IDLE_FINISHED")
    profile = DummyProfile(evening_slot_collected=False)

    cm, db = _make_session_cm([user, profile])
    _db_stub.SessionLocal.return_value = cm
    _db_stub.User = MagicMock()
    _db_stub.UserProfile = MagicMock()

    result = tools.create_followup_plan(user_id=1, plan_type="MEDIUM")

    assert result == {"status": "needs_evening_time"}


def test_create_followup_plan_medium_ok():
    """IDLE_FINISHED + evening_slot_collected=True → MEDIUM plan, ACTIVE, side effects called."""
    user = DummyUser("IDLE_FINISHED")
    profile = DummyProfile(
        day_time="14:00",
        evening_time="21:00",
        evening_slot_collected=True,
    )
    fake_plan = MagicMock()
    fake_plan.id = 9

    cm, db = _make_session_cm([user, profile, user])
    _db_stub.SessionLocal.return_value = cm
    _db_stub.User = MagicMock()
    _db_stub.UserProfile = MagicMock()
    _service_stub.create_plan.return_value = fake_plan
    _finalization_stub.activate_plan_side_effects.reset_mock()

    result = tools.create_followup_plan(user_id=1, plan_type="MEDIUM")

    assert result == {"status": "ok", "plan_type": "MEDIUM"}
    assert user.current_state == "ACTIVE"
    # Verify evening_time was passed
    create_call = _service_stub.create_plan.call_args
    assert "MEDIUM" in create_call.args or create_call.kwargs.get("plan_type") == "MEDIUM"
    _finalization_stub.activate_plan_side_effects.assert_called_once_with(9, 1)


# ─── record_evening_time ──────────────────────────────────────────────────────


def test_record_evening_time():
    """Valid HH:MM stores time in daily_time_slots and sets evening_slot_collected."""
    user = DummyUser()
    profile = DummyProfile()

    cm, db = _make_session_cm([user, profile])
    _db_stub.SessionLocal.return_value = cm
    _db_stub.User = MagicMock()
    _db_stub.UserProfile = MagicMock()

    result = tools.record_evening_time(user_id=1, hhmm="20:30")

    assert result == {"status": "ok", "evening_time": "20:30"}
    assert profile.daily_time_slots["EVENING"] == "20:30"
    assert profile.evening_slot_collected is True


def test_record_evening_time_bad_format():
    """'9:00' (single-digit hour) raises ValueError — HH:MM requires two digits."""
    with pytest.raises(ValueError, match="Invalid time format"):
        tools.record_evening_time(user_id=1, hhmm="9:00")


# ─── change_day_time ──────────────────────────────────────────────────────────


def test_change_day_time_reschedules():
    """change_day_time calls update_user_time_slots and reschedule_plan_steps."""
    user = DummyUser("ACTIVE")
    profile = DummyProfile(day_time="14:00")

    cm, db = _make_session_cm([user, profile])

    _db_stub.SessionLocal.return_value = cm
    _db_stub.User = MagicMock()
    _db_stub.UserProfile = MagicMock()
    # update_user_time_slots returns (updated_ids, active_ids)
    _time_slots_stub.update_user_time_slots.return_value = ([1, 2], [1, 2])
    _time_slots_stub.TimeSlotError = ValueError
    _scheduler_stub.reschedule_plan_steps.return_value = 2

    result = tools.change_day_time(user_id=1, hhmm="11:00")

    assert result["status"] == "ok"
    assert result["day_time"] == "11:00"
    assert result["rescheduled"] == 2
    _time_slots_stub.update_user_time_slots.assert_called_once_with(db, user, {"DAY": "11:00"})
    _scheduler_stub.reschedule_plan_steps.assert_called_once_with([1, 2])


# ─── pause_plan / resume_plan ─────────────────────────────────────────────────


def test_pause_plan():
    """ACTIVE user → pause_plan delegates to plan_pause, state becomes ACTIVE_PAUSED."""
    user = DummyUser("ACTIVE")
    profile = DummyProfile(is_paused=False)

    cm, db = _make_session_cm([user, profile])
    _db_stub.SessionLocal.return_value = cm
    _db_stub.User = MagicMock()
    _db_stub.UserProfile = MagicMock()

    def _fake_pause(db_arg, user_id):
        user.current_state = "ACTIVE_PAUSED"
        profile.is_paused = True
        profile.pause_count = (profile.pause_count or 0) + 1

    _pause_stub.pause_plan.side_effect = _fake_pause
    _pause_stub.PlanNotActiveError = RuntimeError
    _pause_stub.PlanAlreadyPausedError = RuntimeError

    result = tools.pause_plan(user_id=1)

    assert result == {"status": "ok"}
    assert user.current_state == "ACTIVE_PAUSED"
    assert profile.is_paused is True


def test_resume_plan():
    """ACTIVE_PAUSED user → resume_plan delegates to plan_pause, state becomes ACTIVE."""
    user = DummyUser("ACTIVE_PAUSED")
    profile = DummyProfile(is_paused=True)

    cm, db = _make_session_cm([user, profile])
    _db_stub.SessionLocal.return_value = cm
    _db_stub.User = MagicMock()
    _db_stub.UserProfile = MagicMock()

    def _fake_resume(db_arg, user_id):
        user.current_state = "ACTIVE"
        profile.is_paused = False

    _pause_stub.resume_plan.side_effect = _fake_resume
    _pause_stub.PlanNotPausedError = RuntimeError

    result = tools.resume_plan(user_id=1)

    assert result == {"status": "ok"}
    assert user.current_state == "ACTIVE"
    assert profile.is_paused is False


def test_pause_wrong_state():
    """Non-ACTIVE state → ValueError when calling pause_plan."""
    user = DummyUser("ACTIVE_PAUSED")
    profile = DummyProfile(is_paused=True)

    cm, db = _make_session_cm([user, profile])
    _db_stub.SessionLocal.return_value = cm
    _db_stub.User = MagicMock()
    _db_stub.UserProfile = MagicMock()

    with pytest.raises(ValueError, match="ACTIVE"):
        tools.pause_plan(user_id=1)


# ─── cancel_plan ──────────────────────────────────────────────────────────────


def test_cancel_plan_from_active():
    """ACTIVE user → cancel_plan sets plan to abandoned, state to IDLE_PLAN_ABORTED,
    and calls cancel_plan_step_jobs with pending step IDs."""
    user = DummyUser("ACTIVE")
    profile = DummyProfile()

    fake_plan = MagicMock(spec=["id", "status", "end_date"])
    fake_plan.id = 10
    fake_plan.status = "active"
    fake_plan.end_date = None

    # _load_user_and_profile uses filter().first()
    # _get_active_plan uses filter().order_by().first()
    cm, db = _make_session_cm([user, profile])
    db.query.return_value.filter.return_value.order_by.return_value.first.return_value = fake_plan

    # The join query for step IDs returns two rows
    db.query.return_value.join.return_value.filter.return_value.all.return_value = [
        (3,), (4,)
    ]

    _db_stub.SessionLocal.return_value = cm
    _db_stub.User = MagicMock()
    _db_stub.UserProfile = MagicMock()
    _db_stub.AIPlan = MagicMock()
    _db_stub.AIPlanDay = MagicMock()
    _db_stub.AIPlanStep = MagicMock()
    _db_stub.AIPlanStep.id = MagicMock()
    _db_stub.AIPlanStep.step_status = MagicMock()
    _scheduler_stub.cancel_plan_step_jobs.reset_mock()

    result = tools.cancel_plan(user_id=1)

    assert result == {"status": "ok"}
    assert user.current_state == "IDLE_PLAN_ABORTED"
    assert fake_plan.status == "abandoned"
    _scheduler_stub.cancel_plan_step_jobs.assert_called_once_with([3, 4])


def test_cancel_plan_wrong_state():
    """IDLE_ONBOARDED state → ValueError when calling cancel_plan."""
    user = DummyUser("IDLE_ONBOARDED")
    profile = DummyProfile()

    cm, db = _make_session_cm([user, profile])
    _db_stub.SessionLocal.return_value = cm
    _db_stub.User = MagicMock()
    _db_stub.UserProfile = MagicMock()

    with pytest.raises(ValueError, match="cancel_plan requires ACTIVE or ACTIVE_PAUSED"):
        tools.cancel_plan(user_id=1)

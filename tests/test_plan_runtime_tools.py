from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from app import db as database
from app import lifecycle, plan_pause
from app import plan_finalization
from app.plan_drafts import service as plan_service
from app.plan_runtime import tools


class _Query:
    def __init__(self, value):
        self.value = value
        self.locked = False

    def filter(self, *_args, **_kwargs):
        return self

    def with_for_update(self):
        self.locked = True
        return self

    def one_or_none(self):
        return self.value

    def first(self):
        return self.value


class _DB:
    def __init__(self, *, user, profile, plan=None, receipt=None):
        self.user = user
        self.profile = profile
        self.plan = plan
        self.receipt = receipt
        self.commits = 0

    def query(self, model):
        if model is database.User:
            return _Query(self.user)
        if model is database.UserProfile:
            return _Query(self.profile)
        if model is database.AIPlan or model is database.AIPlan.id:
            return _Query(self.plan)
        if model is database.PlanLifecycleOperation:
            return _Query(self.receipt)
        raise AssertionError(f"unexpected model: {model}")

    def commit(self):
        self.commits += 1


@pytest.mark.parametrize("value", ["9:00", "0900", "24", ""])
def test_hhmm_rejects_non_canonical_shape(value):
    with pytest.raises(ValueError, match="Invalid time format"):
        tools._validate_hhmm(value)


def test_hhmm_accepts_canonical_shape():
    tools._validate_hhmm("09:30")


def test_pause_passes_stable_source_operation_and_writes_no_mirror(monkeypatch):
    user = SimpleNamespace(id=1, current_state="legacy-value")
    profile = SimpleNamespace(user_id=1, is_paused=False, pause_count=4)
    fake_db = _DB(user=user, profile=profile)
    captured = {}

    monkeypatch.setattr(database, "SessionLocal", lambda: nullcontext(fake_db))

    def fake_pause(db, user_id, *, source_operation_id):
        captured.update(
            db=db, user_id=user_id, source_operation_id=source_operation_id
        )
        return SimpleNamespace(duplicate=False)

    monkeypatch.setattr(plan_pause, "pause_plan", fake_pause)
    result = tools.pause_plan(1, source_operation_id="coach:call-1")

    assert result == {"status": "ok", "duplicate": False}
    assert captured["source_operation_id"] == "coach:call-1"
    assert user.current_state == "legacy-value"
    assert profile.is_paused is False
    assert profile.pause_count == 4
    assert fake_db.commits == 1


def test_resume_passes_stable_source_operation(monkeypatch):
    user = SimpleNamespace(id=1)
    profile = SimpleNamespace(user_id=1)
    fake_db = _DB(user=user, profile=profile)
    captured = {}

    monkeypatch.setattr(database, "SessionLocal", lambda: nullcontext(fake_db))

    def fake_resume(db, user_id, *, source_operation_id):
        captured["source_operation_id"] = source_operation_id
        return SimpleNamespace(duplicate=True)

    monkeypatch.setattr(plan_pause, "resume_plan", fake_resume)
    result = tools.resume_plan(1, source_operation_id="coach:call-2")

    assert result == {"status": "ok", "duplicate": True}
    assert captured["source_operation_id"] == "coach:call-2"


def test_cancel_uses_one_aggregate_operation_then_cancels_jobs(monkeypatch):
    user = SimpleNamespace(id=1)
    profile = SimpleNamespace(user_id=1)
    plan = SimpleNamespace(id=11, total_days=7)
    fake_db = _DB(user=user, profile=profile, plan=plan)
    canceled = []

    monkeypatch.setattr(database, "SessionLocal", lambda: nullcontext(fake_db))
    monkeypatch.setattr(
        lifecycle,
        "abandon_current_plan",
        lambda db, **kwargs: (SimpleNamespace(duplicate=False), [21, 22]),
    )
    monkeypatch.setattr(
        "app.scheduler.cancel_plan_step_jobs", lambda ids: canceled.extend(ids)
    )
    result = tools.cancel_plan(1, source_operation_id="telegram:cancel-1")

    assert result == {"status": "ok", "total_days": 7, "duplicate": False}
    assert canceled == [21, 22]
    assert fake_db.commits == 1


def test_create_first_plan_returns_committed_activation_retry_before_mode_guard(
    monkeypatch,
):
    user = SimpleNamespace(id=1)
    profile = SimpleNamespace(user_id=1)
    plan = SimpleNamespace(id=11, total_days=7)
    receipt = SimpleNamespace(operation="activate", plan_id=11)
    fake_db = _DB(user=user, profile=profile, plan=plan, receipt=receipt)

    monkeypatch.setattr(database, "SessionLocal", lambda: nullcontext(fake_db))

    result = tools.create_first_plan(1, source_operation_id="coach:activate-1")

    assert result == {"status": "ok", "plan_type": "SHORT", "duplicate": True}
    assert fake_db.commits == 0


def test_create_first_plan_uses_derived_mode_and_runs_side_effects_once(monkeypatch):
    user = SimpleNamespace(id=1, current_state="legacy-value")
    profile = SimpleNamespace(user_id=1, daily_time_slots={"DAY": "13:30"})
    created_plan = SimpleNamespace(id=21, total_days=7)
    fake_db = _DB(user=user, profile=profile)
    captured = {}
    side_effects = []

    monkeypatch.setattr(database, "SessionLocal", lambda: nullcontext(fake_db))
    monkeypatch.setattr(
        lifecycle,
        "derive_current_mode",
        lambda *_args: lifecycle.CurrentMode.NO_ACTIVE_PLAN,
    )

    def fake_create_plan(db, **kwargs):
        captured.update(db=db, **kwargs)
        return SimpleNamespace(plan=created_plan, duplicate=False)

    monkeypatch.setattr(plan_service, "create_plan", fake_create_plan)
    monkeypatch.setattr(
        plan_finalization,
        "activate_plan_side_effects",
        lambda plan_id, user_id: side_effects.append((plan_id, user_id)),
    )

    result = tools.create_first_plan(1, source_operation_id="coach:activate-2")

    assert result == {"status": "ok", "plan_type": "SHORT", "duplicate": False}
    assert captured["plan_type"] == "SHORT"
    assert captured["day_time"] == "13:30"
    assert captured["source_operation_id"] == "coach:activate-2"
    assert side_effects == [(21, 1)]
    assert user.current_state == "legacy-value"
    assert fake_db.commits == 1


def test_create_followup_medium_requires_collected_evening_slot(monkeypatch):
    user = SimpleNamespace(id=1)
    profile = SimpleNamespace(
        user_id=1,
        daily_time_slots={"DAY": "14:00", "EVENING": "20:30"},
        evening_slot_collected=False,
    )
    historical_plan = SimpleNamespace(id=10, total_days=7)
    fake_db = _DB(user=user, profile=profile, plan=historical_plan)

    monkeypatch.setattr(database, "SessionLocal", lambda: nullcontext(fake_db))
    monkeypatch.setattr(
        lifecycle,
        "derive_current_mode",
        lambda *_args: lifecycle.CurrentMode.NO_ACTIVE_PLAN,
    )

    result = tools.create_followup_plan(
        1,
        "MEDIUM",
        source_operation_id="coach:activate-3",
    )

    assert result == {"status": "needs_evening_time"}
    assert fake_db.commits == 0


def test_create_followup_passes_source_and_derived_prerequisites(monkeypatch):
    user = SimpleNamespace(id=1)
    profile = SimpleNamespace(
        user_id=1,
        daily_time_slots={"DAY": "14:00", "EVENING": "20:30"},
        evening_slot_collected=True,
    )
    historical_plan = SimpleNamespace(id=10, total_days=7)
    created_plan = SimpleNamespace(id=22, total_days=14)
    fake_db = _DB(user=user, profile=profile, plan=historical_plan)
    captured = {}
    side_effects = []

    monkeypatch.setattr(database, "SessionLocal", lambda: nullcontext(fake_db))
    monkeypatch.setattr(
        lifecycle,
        "derive_current_mode",
        lambda *_args: lifecycle.CurrentMode.NO_ACTIVE_PLAN,
    )

    def fake_create_plan(db, **kwargs):
        captured.update(db=db, **kwargs)
        return SimpleNamespace(plan=created_plan, duplicate=False)

    monkeypatch.setattr(plan_service, "create_plan", fake_create_plan)
    monkeypatch.setattr(
        plan_finalization,
        "activate_plan_side_effects",
        lambda plan_id, user_id: side_effects.append((plan_id, user_id)),
    )

    result = tools.create_followup_plan(
        1,
        "MEDIUM",
        source_operation_id="coach:activate-4",
    )

    assert result == {"status": "ok", "plan_type": "MEDIUM", "duplicate": False}
    assert captured["plan_type"] == "MEDIUM"
    assert captured["evening_time"] == "20:30"
    assert captured["source_operation_id"] == "coach:activate-4"
    assert side_effects == [(22, 1)]
    assert fake_db.commits == 1


def test_get_plan_status_uses_derived_mode_day_and_step_status(monkeypatch):
    user = SimpleNamespace(id=1, current_state="stale")
    profile = SimpleNamespace(user_id=1)
    plan = SimpleNamespace(
        id=31,
        total_days=7,
        days=[
            SimpleNamespace(
                steps=[
                    SimpleNamespace(step_status="completed"),
                    SimpleNamespace(step_status="skipped"),
                    SimpleNamespace(step_status="pending"),
                ]
            )
        ],
    )
    fake_db = _DB(user=user, profile=profile, plan=plan)

    monkeypatch.setattr(database, "SessionLocal", lambda: nullcontext(fake_db))
    monkeypatch.setattr(
        lifecycle,
        "derive_current_mode",
        lambda *_args: lifecycle.CurrentMode.ACTIVE_PAUSED,
    )
    monkeypatch.setattr(lifecycle, "derive_current_day", lambda *_args: 3)

    assert tools.get_plan_status(1) == {
        "state": "ACTIVE_PAUSED",
        "current_mode": "ACTIVE_PAUSED",
        "plan_active": True,
        "days_total": 7,
        "current_day": 3,
        "days_completed": 2,
        "days_remaining": 5,
        "steps_total": 3,
        "steps_completed": 1,
        "completion_rate": 33,
    }


def test_activation_retry_rejects_cross_operation_source_id():
    fake_db = _DB(
        user=SimpleNamespace(id=1),
        profile=SimpleNamespace(user_id=1),
        receipt=SimpleNamespace(operation="pause", plan_id=11),
    )

    with pytest.raises(ValueError, match="already belongs to pause"):
        tools._existing_activation_retry(fake_db, 1, "coach:shared-1")


def test_mutation_tools_require_source_operation_id():
    with pytest.raises(TypeError):
        tools.create_first_plan(1)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        tools.create_followup_plan(1, "SHORT")  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        tools.pause_plan(1)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        tools.resume_plan(1)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        tools.cancel_plan(1)  # type: ignore[call-arg]

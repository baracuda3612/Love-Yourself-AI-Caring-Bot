from __future__ import annotations

from contextlib import nullcontext
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app import db as database
from app import lifecycle, lifecycle_reconciliation
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


@pytest.mark.parametrize(
    "value",
    ["9:00", "0900", "24", "", "24:00", "12:60", "99:99", "-1:00"],
)
def test_hhmm_rejects_non_canonical_shape(value):
    with pytest.raises(ValueError, match="Invalid time format"):
        tools._validate_hhmm(value)


@pytest.mark.parametrize("value", ["00:00", "09:30", "23:59"])
def test_hhmm_accepts_canonical_shape(value):
    tools._validate_hhmm(value)


@pytest.mark.parametrize(
    ("tool_name", "slot", "result_key"),
    [
        ("change_day_time", "DAY", "day_time"),
        ("change_evening_time", "EVENING", "evening_time"),
    ],
)
def test_direct_time_tools_update_authority_and_reschedule_active_steps(
    monkeypatch,
    tool_name,
    slot,
    result_key,
):
    user = SimpleNamespace(id=1)
    profile = SimpleNamespace(user_id=1)
    fake_db = _DB(user=user, profile=profile)
    captured = {}

    monkeypatch.setattr(database, "SessionLocal", lambda: nullcontext(fake_db))

    def fake_change(db, **kwargs):
        captured.update(db=db, **kwargs)
        return lifecycle.LifecycleResult(
            user_id=1,
            plan_id=11,
            status="15:30",
            operation=f"change_{slot.lower()}_time",
            effects=(
                lifecycle.ExternalEffect(
                    kind="reconcile_plan_schedule",
                    target_ids=(101,),
                ),
            ),
        )

    monkeypatch.setattr(lifecycle, "change_delivery_time", fake_change)
    monkeypatch.setattr(
        lifecycle_reconciliation,
        "reconcile_scheduler_effects",
        lambda result: replace(
            result,
            effects=(
                replace(
                    result.effects[0],
                    state=lifecycle.ExternalEffectState.SUCCEEDED,
                    attempted=1,
                    succeeded=1,
                ),
            ),
        ),
    )

    result = getattr(tools, tool_name)(
        1,
        "15:30",
        source_operation_id="coach:time-1",
    )

    assert captured == {
        "db": fake_db,
        "user_id": 1,
        "slot": slot,
        "hhmm": "15:30",
        "source_operation_id": "coach:time-1",
    }
    assert result == {
        "status": "ok",
        result_key: "15:30",
        "saved": True,
        "jobs_reconciled": "succeeded",
        "rescheduled": 1,
        "duplicate": False,
        "disposition": "applied",
    }
    assert fake_db.commits == 1


@pytest.mark.parametrize(
    ("tool_name", "slot", "result_key"),
    [
        ("change_day_time", "DAY", "day_time"),
        ("change_evening_time", "EVENING", "evening_time"),
    ],
)
def test_time_tools_report_superseded_replays_without_reconciliation(
    monkeypatch,
    tool_name,
    slot,
    result_key,
):
    fake_db = _DB(
        user=SimpleNamespace(id=1),
        profile=SimpleNamespace(user_id=1),
    )
    monkeypatch.setattr(database, "SessionLocal", lambda: nullcontext(fake_db))
    monkeypatch.setattr(
        lifecycle,
        "change_delivery_time",
        lambda *_args, **_kwargs: lifecycle.LifecycleResult(
            user_id=1,
            plan_id=11,
            status="15:30",
            operation=f"change_{slot.lower()}_time",
            duplicate=True,
            code="superseded",
            applied=False,
            effects=(
                lifecycle.ExternalEffect(
                    kind="reconcile_plan_schedule",
                    state=lifecycle.ExternalEffectState.NOT_REQUIRED,
                ),
            ),
            details={
                "slot": slot,
                "value": "15:30",
                "authoritative_value": "16:45",
            },
        ),
    )
    monkeypatch.setattr(
        lifecycle_reconciliation,
        "reconcile_scheduler_effects",
        lambda _result: pytest.fail("superseded replay must not reconcile"),
    )

    result = getattr(tools, tool_name)(
        1,
        "15:30",
        source_operation_id="coach:time-1",
    )

    assert result == {
        "status": "error",
        "code": "superseded",
        result_key: "16:45",
        f"requested_{result_key}": "15:30",
        "saved": False,
        "jobs_reconciled": False,
        "duplicate": True,
        "disposition": "superseded",
    }
    assert fake_db.commits == 1


def test_evening_preference_superseded_replay_is_not_success(monkeypatch):
    fake_db = _DB(
        user=SimpleNamespace(id=1),
        profile=SimpleNamespace(user_id=1),
    )
    monkeypatch.setattr(database, "SessionLocal", lambda: nullcontext(fake_db))
    monkeypatch.setattr(
        lifecycle,
        "record_evening_time_preference",
        lambda *_args, **_kwargs: lifecycle.LifecycleResult(
            user_id=1,
            plan_id=11,
            status="20:30",
            operation="record_evening_time",
            duplicate=True,
            code="superseded",
            applied=False,
            details={"value": "20:30", "authoritative_value": "21:15"},
        ),
    )

    result = tools.record_evening_time(
        1,
        "20:30",
        context="followup",
        pending_source_operation_id="coach:activate:medium",
        source_operation_id="coach:evening-old",
    )

    assert result == {
        "status": "error",
        "code": "superseded",
        "evening_time": "21:15",
        "requested_evening_time": "20:30",
        "saved": False,
        "applied": False,
        "duplicate": True,
        "disposition": "superseded",
    }


def test_pause_passes_stable_source_operation_and_writes_no_mirror(monkeypatch):
    user = SimpleNamespace(id=1, current_state="legacy-value")
    profile = SimpleNamespace(user_id=1, is_paused=False, pause_count=4)
    fake_db = _DB(user=user, profile=profile)
    captured = {}

    monkeypatch.setattr(database, "SessionLocal", lambda: nullcontext(fake_db))

    def fake_transition(db, *, user_id, operation, source_operation_id):
        captured.update(
            db=db,
            user_id=user_id,
            operation=operation,
            source_operation_id=source_operation_id,
        )
        return lifecycle.LifecycleResult(
            user_id=user_id,
            plan_id=10,
            status="paused",
            operation=operation,
            effects=(
                lifecycle.ExternalEffect(
                    kind="pause_schedule_reconciliation",
                    state=lifecycle.ExternalEffectState.DEFERRED,
                ),
            ),
        )

    monkeypatch.setattr(lifecycle, "transition_current_plan", fake_transition)
    result = tools.pause_plan(1, source_operation_id="coach:call-1")

    assert result == {
        "status": "ok",
        "plan_id": 10,
        "plan_status": "paused",
        "schedule_reconciliation": "deferred",
        "duplicate": False,
        "disposition": "applied",
    }
    assert captured["source_operation_id"] == "coach:call-1"
    assert captured["operation"] == "pause"
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

    def fake_transition(db, *, user_id, operation, source_operation_id):
        captured["source_operation_id"] = source_operation_id
        return lifecycle.LifecycleResult(
            user_id=user_id,
            plan_id=10,
            status="active",
            operation=operation,
            duplicate=True,
            effects=(
                lifecycle.ExternalEffect(
                    kind="resume_schedule_reconciliation",
                    state=lifecycle.ExternalEffectState.DEFERRED,
                ),
            ),
        )

    monkeypatch.setattr(lifecycle, "transition_current_plan", fake_transition)
    result = tools.resume_plan(1, source_operation_id="coach:call-2")

    assert result == {
        "status": "ok",
        "plan_id": 10,
        "plan_status": "active",
        "schedule_reconciliation": "deferred",
        "duplicate": True,
        "disposition": "replayed",
    }
    assert captured["source_operation_id"] == "coach:call-2"


def test_resume_reports_persisted_schedule_failure_without_success_copy(monkeypatch):
    fake_db = _DB(user=SimpleNamespace(id=1), profile=SimpleNamespace(user_id=1))
    monkeypatch.setattr(database, "SessionLocal", lambda: nullcontext(fake_db))
    monkeypatch.setattr(
        lifecycle,
        "transition_current_plan",
        lambda *_args, **_kwargs: lifecycle.LifecycleResult(
            user_id=1,
            plan_id=10,
            status="active",
            operation="resume",
            effects=(lifecycle.ExternalEffect(
                kind="reconcile_plan_schedule", target_ids=(10,)
            ),),
        ),
    )
    monkeypatch.setattr(
        lifecycle_reconciliation,
        "reconcile_scheduler_effects",
        lambda result: replace(
            result,
            effects=(replace(
                result.effects[0], state=lifecycle.ExternalEffectState.FAILED
            ),),
        ),
    )

    result = tools.resume_plan(1, source_operation_id="resume:partial")

    assert result["status"] == "error"
    assert result["code"] == "resume_reconciliation_failed"
    assert result["persisted"] is True
    assert result["disposition"] == "partial_failure"
    assert fake_db.commits == 1


def test_switch_tool_reconciles_source_and_replacement_after_commit(monkeypatch):
    fake_db = _DB(user=SimpleNamespace(id=1), profile=SimpleNamespace(user_id=1))
    monkeypatch.setattr(database, "SessionLocal", lambda: nullcontext(fake_db))
    captured = {}

    def decide(_db, **kwargs):
        captured.update(kwargs)
        return lifecycle.LifecycleResult(
            user_id=1,
            plan_id=12,
            status="active",
            operation="switch_plan_format",
            plan_type="MEDIUM",
            effects=(
                lifecycle.ExternalEffect(kind="reconcile_plan_schedule", target_ids=(11,)),
                lifecycle.ExternalEffect(kind="reconcile_plan_schedule", target_ids=(12,)),
            ),
            details={"source_plan_id": 11},
        )

    monkeypatch.setattr(lifecycle, "switch_plan_format", decide)

    def reconcile(result):
        assert fake_db.commits == 1
        assert [effect.target_ids for effect in result.effects] == [(11,), (12,)]
        return replace(
            result,
            effects=tuple(
                replace(effect, state=lifecycle.ExternalEffectState.SUCCEEDED)
                for effect in result.effects
            ),
        )

    monkeypatch.setattr(lifecycle_reconciliation, "reconcile_scheduler_effects", reconcile)

    result = tools.switch_plan_format(
        1, "MEDIUM", source_operation_id="switch:applied"
    )

    assert captured["source_operation_id"] == "switch:applied"
    assert captured["target_plan_type"] == "MEDIUM"
    assert result["status"] == "ok"
    assert result["source_plan_id"] == 11
    assert result["plan_id"] == 12


def test_switch_recovery_waits_for_first_evening_without_new_intent(monkeypatch):
    fake_db = _DB(user=SimpleNamespace(id=1), profile=SimpleNamespace(user_id=1))
    monkeypatch.setattr(database, "SessionLocal", lambda: nullcontext(fake_db))
    captured = {}

    def recover(_db, **kwargs):
        captured.update(kwargs)
        return lifecycle.LifecycleResult(
            user_id=1,
            plan_id=11,
            status="active",
            operation="switch_plan_format",
            duplicate=True,
            code="needs_evening_time",
            applied=False,
            plan_type="SHORT",
            details={"target_plan_type": "MEDIUM"},
        )

    monkeypatch.setattr(lifecycle, "recover_plan_format_switch", recover)

    result = tools.recover_plan_format_switch(
        1, "MEDIUM", source_operation_id="switch:pending"
    )

    assert result == {
        "status": "not_ready",
        "target_plan_type": "MEDIUM",
        "duplicate": True,
        "disposition": "deferred",
    }
    assert captured["source_operation_id"] == "switch:pending"
    assert fake_db.commits == 1


def test_cancel_uses_one_aggregate_operation_then_cancels_jobs(monkeypatch):
    user = SimpleNamespace(id=1)
    profile = SimpleNamespace(user_id=1)
    plan = SimpleNamespace(id=11, total_days=7)
    fake_db = _DB(user=user, profile=profile, plan=plan)
    monkeypatch.setattr(database, "SessionLocal", lambda: nullcontext(fake_db))
    monkeypatch.setattr(
        lifecycle,
        "abandon_current_plan",
        lambda db, **kwargs: (
            lifecycle.LifecycleResult(
                user_id=1,
                plan_id=11,
                status="abandoned",
                operation="abandon",
                plan_type="SHORT",
                effects=(
                    lifecycle.ExternalEffect(
                        kind="cancel_step_jobs",
                        target_ids=(21, 22),
                    ),
                ),
            ),
            [21, 22],
        ),
    )
    monkeypatch.setattr(
        lifecycle_reconciliation,
        "reconcile_scheduler_effects",
        lambda result: replace(
            result,
            effects=(
                replace(
                    result.effects[0],
                    state=lifecycle.ExternalEffectState.SUCCEEDED,
                    attempted=2,
                    succeeded=2,
                ),
            ),
        ),
    )
    result = tools.cancel_plan(1, source_operation_id="telegram:cancel-1")

    assert result == {
        "status": "ok",
        "plan_id": 11,
        "total_days": 7,
        "jobs_reconciled": True,
        "duplicate": False,
        "disposition": "applied",
    }
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
        "prepare_followup_evening_collection",
        lambda *_args, **_kwargs: lifecycle.LifecycleResult(
            user_id=1,
            plan_id=10,
            status="abandoned",
            operation="prepare_followup_evening",
            code="needs_evening_time",
            applied=False,
        ),
    )
    result = tools.create_followup_plan(
        1,
        "MEDIUM",
        source_operation_id="coach:activate-3",
    )

    assert result == {
        "status": "needs_evening_time",
        "duplicate": False,
        "disposition": "deferred",
    }
    assert fake_db.commits == 1


def test_create_followup_passes_source_and_derived_prerequisites(monkeypatch):
    user = SimpleNamespace(id=1)
    profile = SimpleNamespace(
        user_id=1,
        daily_time_slots={"DAY": "14:00", "EVENING": "20:30"},
        evening_slot_collected=True,
    )
    historical_plan = SimpleNamespace(id=10, total_days=7)
    fake_db = _DB(user=user, profile=profile, plan=historical_plan)
    captured = {}

    monkeypatch.setattr(database, "SessionLocal", lambda: nullcontext(fake_db))

    def fake_activate(db, **kwargs):
        captured.update(db=db, **kwargs)
        return lifecycle.LifecycleResult(
            user_id=1,
            plan_id=22,
            status="active",
            operation="activate",
            plan_type="MEDIUM",
            effects=(
                lifecycle.ExternalEffect(
                    kind="reconcile_plan_schedule",
                    target_ids=(31, 32),
                ),
            ),
            details={"total_days": 14},
        )

    monkeypatch.setattr(lifecycle, "activate_plan", fake_activate)
    monkeypatch.setattr(
        lifecycle_reconciliation,
        "reconcile_scheduler_effects",
        lambda result: replace(
            result,
            effects=(
                replace(
                    result.effects[0],
                    state=lifecycle.ExternalEffectState.SUCCEEDED,
                    attempted=2,
                    succeeded=2,
                ),
            ),
        ),
    )

    result = tools.create_followup_plan(
        1,
        "MEDIUM",
        source_operation_id="coach:activate-4",
    )

    assert result == {
        "status": "ok",
        "plan_id": 22,
        "plan_type": "MEDIUM",
        "jobs_reconciled": True,
        "duplicate": False,
        "disposition": "applied",
    }
    assert captured["plan_type"] == "MEDIUM"
    assert captured["evening_time"] == "20:30"
    assert captured["source_operation_id"] == "coach:activate-4"
    assert captured["require_plan_history"] is True
    assert captured["required_previous_status"] == "abandoned"
    assert fake_db.commits == 1


def test_activation_reconciliation_failure_is_returned_as_error(monkeypatch):
    profile = SimpleNamespace(
        user_id=1,
        daily_time_slots={"DAY": "14:00"},
        evening_slot_collected=False,
    )
    fake_db = _DB(user=SimpleNamespace(id=1), profile=profile)
    decision = lifecycle.LifecycleResult(
        user_id=1,
        plan_id=22,
        status="active",
        operation="activate",
        plan_type="SHORT",
        effects=(
            lifecycle.ExternalEffect(
                kind="reconcile_plan_schedule",
                target_ids=(31,),
            ),
        ),
        details={"total_days": 7},
    )
    monkeypatch.setattr(database, "SessionLocal", lambda: nullcontext(fake_db))
    monkeypatch.setattr(lifecycle, "activate_plan", lambda *a, **k: decision)
    monkeypatch.setattr(
        lifecycle_reconciliation,
        "reconcile_scheduler_effects",
        lambda result: replace(
            result,
            effects=(
                replace(
                    result.effects[0],
                    state=lifecycle.ExternalEffectState.FAILED,
                    attempted=1,
                    error_code="scheduler_reconciliation_failed",
                ),
            ),
        ),
    )

    assert tools.create_followup_plan(
        1,
        "SHORT",
        source_operation_id="coach:activate:failed",
    ) == {
        "status": "error",
        "code": "activation_reconciliation_failed",
        "plan_id": 22,
        "plan_type": "SHORT",
        "persisted": True,
        "jobs_reconciled": False,
        "duplicate": False,
        "disposition": "partial_failure",
    }


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
        "read_lifecycle_status",
        lambda *_args: lifecycle.LifecycleStatus(
            user_id=1,
            current_mode=lifecycle.CurrentMode.ACTIVE_PAUSED,
            plan_id=31,
            plan_status="paused",
            plan_type="SHORT",
            days_total=7,
            current_day=3,
            days_completed=2,
            delivery_days_remaining=0,
            steps_total=3,
            steps_completed=1,
            steps_remaining=1,
            deliveries_remaining=1,
        ),
    )

    assert tools.get_plan_status(1) == {
        "state": "ACTIVE_PAUSED",
        "current_mode": "ACTIVE_PAUSED",
        "plan_active": True,
        "plan_id": 31,
        "plan_type": "SHORT",
        "days_total": 7,
        "current_day": 3,
        "days_completed": 2,
        "days_remaining": 0,
        "steps_total": 3,
        "steps_completed": 1,
        "steps_delivered": 0,
        "steps_remaining": 1,
        "deliveries_remaining": 1,
        "completion_rate": 33,
    }


def test_mutation_tools_require_source_operation_id():
    with pytest.raises(TypeError):
        tools.create_followup_plan(1, "SHORT")  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        tools.pause_plan(1)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        tools.resume_plan(1)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        tools.cancel_plan(1)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        tools.record_evening_time(1, "20:30")  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        tools.change_day_time(1, "09:30")  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        tools.change_evening_time(1, "20:30")  # type: ignore[call-arg]

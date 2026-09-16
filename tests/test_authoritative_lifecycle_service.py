from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import lifecycle, lifecycle_reconciliation, scheduler, telemetry


def _activation_receipt_status(
    plan_type: str,
    day_time: str,
    evening_time: str | None,
) -> str:
    material = f"{plan_type}|{day_time}|{evening_time or '-'}"
    return f"active:{hashlib.sha256(material.encode()).hexdigest()[:16]}"


class _OneQuery:
    def __init__(self, value):
        self.value = value
        self.populated = False
        self.locked = False

    def filter(self, *_args):
        return self

    def populate_existing(self):
        self.populated = True
        return self

    def with_for_update(self):
        self.locked = True
        return self

    def first(self):
        return self.value


class _UserDB:
    def __init__(self, user):
        self.query_result = _OneQuery(user)

    def query(self, model):
        assert model is lifecycle.User
        return self.query_result


def test_entitlement_is_checked_on_the_locked_authoritative_user():
    db = _UserDB(SimpleNamespace(id=7, is_active=False))

    with pytest.raises(lifecycle.LifecycleEntitlementError, match="user_not_entitled"):
        lifecycle._lock_user(db, 7)

    assert db.query_result.populated is True
    assert db.query_result.locked is True


def test_abandon_replay_restores_authoritative_plan_type(monkeypatch):
    receipt = SimpleNamespace(
        user_id=7,
        plan_id=14,
        plan_step_id=None,
        operation="abandon",
        result_status="abandoned",
    )
    plan = SimpleNamespace(id=14, user_id=7, total_days=14)

    class _CanceledStepsQuery:
        def join(self, *_args):
            return self

        def filter(self, *_args):
            return self

        def order_by(self, *_args):
            return self

        def all(self):
            return [(31,), (32,)]

    class _ReplayDB:
        def query(self, model):
            if model is lifecycle.AIPlan:
                return _OneQuery(plan)
            if model is lifecycle.AIPlanStep.id:
                return _CanceledStepsQuery()
            raise AssertionError(f"unexpected model: {model}")

    monkeypatch.setattr(lifecycle, "_lock_user", lambda *_args: object())
    monkeypatch.setattr(
        lifecycle,
        "find_lifecycle_operation",
        lambda *_args: receipt,
    )

    result, canceled_ids = lifecycle.abandon_current_plan(
        _ReplayDB(),
        user_id=7,
        source_operation_id="coach:cancel:14",
    )

    assert result.duplicate is True
    assert result.plan_type == "MEDIUM"
    assert result.effects[0].target_ids == (31, 32)
    assert canceled_ids == [31, 32]


def test_activation_replay_rejects_material_argument_drift(monkeypatch):
    receipt = SimpleNamespace(
        user_id=7,
        plan_id=14,
        plan_step_id=None,
        operation="activate",
        result_status=_activation_receipt_status("SHORT", "14:00", None),
    )
    plan = SimpleNamespace(id=14, user_id=7, status="active", total_days=7)
    user = SimpleNamespace(
        id=7,
        is_active=True,
        profile=SimpleNamespace(
            daily_time_slots={"DAY": "14:00", "EVENING": "20:30"},
            evening_slot_collected=True,
        ),
    )

    class _StepIdsQuery:
        def join(self, *_args):
            return self

        def filter(self, *_args):
            return self

        def order_by(self, *_args):
            return self

        def all(self):
            return []

    class _ReplayDB:
        def query(self, model):
            if model is lifecycle.AIPlanStep.id:
                return _StepIdsQuery()
            raise AssertionError(f"unexpected model: {model}")

    monkeypatch.setattr(lifecycle, "_lock_user", lambda *_args: user)
    monkeypatch.setattr(
        lifecycle,
        "find_lifecycle_operation",
        lambda *_args: receipt,
    )
    monkeypatch.setattr(
        "app.plan_drafts.service.create_plan_for_lifecycle",
        lambda *_args, **_kwargs: SimpleNamespace(plan=plan, duplicate=True),
    )

    with pytest.raises(
        lifecycle.LifecycleTransitionError,
        match="already recorded result",
    ):
        lifecycle.activate_plan(
            _ReplayDB(),
            user_id=7,
            plan_type="MEDIUM",
            day_time="15:00",
            evening_time="20:30",
            source_operation_id="coach:activate:7",
        )


def test_activation_exact_replay_returns_recorded_outcome_not_mutable_status(
    monkeypatch,
):
    receipt = SimpleNamespace(
        user_id=7,
        plan_id=14,
        plan_step_id=None,
        operation="activate",
        result_status=_activation_receipt_status("SHORT", "14:00", None),
    )
    plan = SimpleNamespace(id=14, user_id=7, status="completed", total_days=7)
    user = SimpleNamespace(
        id=7,
        is_active=True,
        profile=SimpleNamespace(
            daily_time_slots={"DAY": "14:00", "EVENING": "21:00"},
            evening_slot_collected=False,
        ),
    )

    class _StepIdsQuery:
        def join(self, *_args):
            return self

        def filter(self, *_args):
            return self

        def order_by(self, *_args):
            return self

        def all(self):
            return []

    class _ReplayDB:
        def query(self, model):
            if model is lifecycle.AIPlanStep.id:
                return _StepIdsQuery()
            raise AssertionError(f"unexpected model: {model}")

    monkeypatch.setattr(lifecycle, "_lock_user", lambda *_args: user)
    monkeypatch.setattr(
        lifecycle,
        "find_lifecycle_operation",
        lambda *_args: receipt,
    )
    monkeypatch.setattr(
        "app.plan_drafts.service.create_plan_for_lifecycle",
        lambda *_args, **_kwargs: SimpleNamespace(plan=plan, duplicate=True),
    )

    result = lifecycle.activate_plan(
        _ReplayDB(),
        user_id=7,
        plan_type="SHORT",
        day_time="14:00",
        evening_time=None,
        source_operation_id="coach:activate:7",
    )

    assert result.duplicate is True
    assert result.plan_id == 14
    assert result.status == "active"
    assert result.effects[0].state is lifecycle.ExternalEffectState.NOT_REQUIRED


def test_owned_step_transition_resolves_actor_inside_the_service(monkeypatch):
    actor = SimpleNamespace(id=17, is_active=True)
    captured = {}
    monkeypatch.setattr(lifecycle, "_lock_telegram_actor", lambda db, tg_id: actor)

    def _transition(db, **kwargs):
        captured.update(db=db, **kwargs)
        return lifecycle.LifecycleResult(
            user_id=17,
            plan_id=4,
            step_id=9,
            status="completed",
            operation="step_completed",
        )

    monkeypatch.setattr(lifecycle, "transition_plan_step", _transition)
    result = lifecycle.transition_owned_plan_step(
        object(),
        telegram_user_id=700,
        step_id=9,
        target_status="completed",
        source_operation_id="telegram:callback:9",
    )

    assert result.user_id == 17
    assert captured["user_id"] == 17
    assert captured["step_id"] == 9
    assert captured["source_operation_id"] == "telegram:callback:9"


def test_reconciliation_failure_is_not_success(monkeypatch):
    result = lifecycle.LifecycleResult(
        user_id=3,
        plan_id=5,
        status="active",
        operation="change_day_time",
        effects=(
            lifecycle.ExternalEffect(
                kind="reconcile_plan_schedule",
                target_ids=(10, 11),
            ),
        ),
    )
    monkeypatch.setattr(
        scheduler,
        "reconcile_plan_step_jobs",
        lambda _ids: scheduler.SchedulerReconciliation(
            attempted=2,
            succeeded=1,
            failed_ids=(11,),
        ),
    )

    reconciled = lifecycle_reconciliation.reconcile_scheduler_effects(result)

    assert reconciled.external_effects_succeeded is False
    assert reconciled.effects[0].state is lifecycle.ExternalEffectState.FAILED
    assert reconciled.effects[0].attempted == 2
    assert reconciled.effects[0].succeeded == 1


def test_scheduler_reconciliation_removes_job_with_stale_run_date(monkeypatch):
    expected = datetime(2026, 9, 12, 10, tzinfo=timezone.utc)
    stale = datetime(2026, 9, 12, 14, tzinfo=timezone.utc)
    step = SimpleNamespace(
        id=10,
        day_id=30,
        day=SimpleNamespace(plan_id=20),
        scheduled_for=expected,
    )
    plan = SimpleNamespace(status="active")
    user = SimpleNamespace(is_active=True)

    class _RowsQuery:
        def join(self, *_args):
            return self

        def filter(self, *_args):
            return self

        def all(self):
            return [(step, step.day, plan, user)]

    class _RowsDB:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def query(self, *_args):
            return _RowsQuery()

    class _Scheduler:
        def __init__(self):
            self.job = SimpleNamespace(next_run_time=stale)
            self.removed = []

        def get_job(self, _job_id):
            return self.job

        def remove_job(self, job_id):
            self.removed.append(job_id)
            self.job = None

    fake_scheduler = _Scheduler()
    monkeypatch.setattr(scheduler, "SessionLocal", _RowsDB)
    monkeypatch.setattr(scheduler, "scheduler", fake_scheduler)
    monkeypatch.setattr(scheduler, "schedule_plan_step", lambda *_args: False)

    result = scheduler.reconcile_plan_step_jobs([10])

    assert result.attempted == 1
    assert result.succeeded == 0
    assert result.failed_ids == (10,)
    assert fake_scheduler.removed == ["plan_20_day_30_step_10"]


def test_deferred_resume_semantics_are_structured_not_claimed_as_reconciled():
    result = lifecycle.LifecycleResult(
        user_id=3,
        plan_id=5,
        status="active",
        operation="resume",
        code="resume_schedule_semantics_deferred_to_wp_02_3",
        effects=(
            lifecycle.ExternalEffect(
                kind="resume_schedule_reconciliation",
                state=lifecycle.ExternalEffectState.DEFERRED,
            ),
        ),
    )

    assert result.code.endswith("wp_02_3")
    assert result.effects[0].state is lifecycle.ExternalEffectState.DEFERRED


def test_final_day_delivery_reports_zero_delivery_days_remaining():
    status = lifecycle.LifecycleStatus(
        user_id=1,
        current_mode=lifecycle.CurrentMode.ACTIVE,
        plan_id=8,
        plan_status="active",
        plan_type="SHORT",
        days_total=7,
        current_day=7,
        days_completed=6,
        delivery_days_remaining=0,
        steps_total=7,
        steps_completed=6,
        steps_remaining=1,
        deliveries_remaining=0,
    )

    payload = status.to_runtime_payload()

    assert payload["current_day"] == 7
    assert payload["days_remaining"] == 0
    assert payload["deliveries_remaining"] == 0
    assert payload["steps_remaining"] == 1


def test_expiry_writes_ignored_event_in_the_same_operation(monkeypatch):
    occurred_at = datetime(2026, 9, 12, 20, 59, 59, tzinfo=timezone.utc)
    result = lifecycle.LifecycleResult(
        user_id=1,
        plan_id=2,
        step_id=3,
        status="expired",
        operation="step_expired",
    )
    captured = {}
    monkeypatch.setattr(lifecycle, "transition_plan_step", lambda *a, **k: result)
    monkeypatch.setattr(
        telemetry,
        "write_event_operation",
        lambda db, **kwargs: captured.update(db=db, **kwargs),
    )

    assert lifecycle.expire_plan_step(
        object(),
        user_id=1,
        step_id=3,
        source_operation_id="scheduler:expiry:3:boundary",
        occurred_at=occurred_at,
    ) is result
    assert captured["event_name"] == "task_ignored"
    assert captured["source_operation_id"] == "scheduler:expiry:3:boundary"
    assert captured["occurred_at"] == occurred_at
    assert captured["properties"] == {"detection_source": "local_expiry"}


def test_legacy_pre_catalog_expiry_does_not_invent_ignored_event(monkeypatch):
    occurred_at = datetime(2026, 8, 1, 20, 59, 59, tzinfo=timezone.utc)
    result = lifecycle.LifecycleResult(
        user_id=1,
        plan_id=2,
        step_id=3,
        status="expired",
        operation="step_expired",
    )
    monkeypatch.setattr(lifecycle, "transition_plan_step", lambda *a, **k: result)

    def _reject_pre_catalog(*_args, **_kwargs):
        raise telemetry.EventChronologyError("event_catalog_not_yet_active")

    monkeypatch.setattr(telemetry, "write_event_operation", _reject_pre_catalog)

    outcome = lifecycle.expire_plan_step(
        object(),
        user_id=1,
        step_id=3,
        source_operation_id="scheduler:expiry:3:legacy",
        occurred_at=occurred_at,
    )

    assert outcome.status == "expired"
    assert outcome.details == {"ignored_event": "not_recorded_pre_catalog"}


def test_expiry_sweep_isolates_event_validation_failure(monkeypatch):
    past = datetime(2026, 8, 1, 20, 59, 59, tzinfo=timezone.utc)

    def _step(step_id):
        return SimpleNamespace(
            id=step_id,
            expires_at=past,
            scheduled_for=past,
            day=SimpleNamespace(
                plan=SimpleNamespace(
                    user_id=1,
                    user=SimpleNamespace(timezone="UTC"),
                )
            ),
        )

    candidates = [_step(1), _step(2)]

    class _CandidatesQuery:
        def join(self, *_args):
            return self

        def filter(self, *_args):
            return self

        def all(self):
            return candidates

    class _KeyboardQuery:
        def filter(self, *_args):
            return self

        def all(self):
            return []

    class _ExpiryDB:
        def __init__(self):
            self.query_count = 0
            self.commits = 0
            self.savepoints = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def query(self, _model):
            self.query_count += 1
            return _CandidatesQuery() if self.query_count == 1 else _KeyboardQuery()

        def begin_nested(self):
            self.savepoints += 1
            from contextlib import nullcontext

            return nullcontext()

        def commit(self):
            self.commits += 1

    db = _ExpiryDB()
    attempted = []

    def _expire(_db, *, step_id, **_kwargs):
        attempted.append(step_id)
        if step_id == 1:
            raise telemetry.EventValidationError("invalid legacy event")
        return lifecycle.LifecycleResult(
            user_id=1,
            plan_id=2,
            step_id=step_id,
            status="expired",
            operation="step_expired",
        )

    monkeypatch.setattr(scheduler, "SessionLocal", lambda: db)
    monkeypatch.setattr(scheduler, "expire_plan_step", _expire)
    monkeypatch.setattr(
        scheduler,
        "reconcile_expired_step_keyboards",
        lambda _ids: SimpleNamespace(failed_ids=()),
    )

    scheduler.expire_overdue_steps()

    assert attempted == [1, 2]
    assert db.savepoints == 2
    assert db.commits == 1


def test_time_change_replay_reconciles_only_currently_schedulable_steps(monkeypatch):
    past = datetime.now(timezone.utc).replace(year=2020)
    future = datetime.now(timezone.utc).replace(year=2030)
    receipt = SimpleNamespace(
        user_id=7,
        plan_id=14,
        plan_step_id=None,
        operation="change_day_time",
        result_status="15:30",
    )
    plan = SimpleNamespace(id=14, user_id=7, status="active", total_days=7)
    user = SimpleNamespace(
        id=7,
        profile=SimpleNamespace(
            daily_time_slots={"DAY": "15:30", "EVENING": "21:00"}
        ),
    )

    class _PendingQuery:
        def join(self, *_args):
            return self

        def filter(self, *_args):
            return self

        def order_by(self, *_args):
            return self

        def all(self):
            return [(1,), (2,)]

    class _ScheduledQuery(_PendingQuery):
        def all(self):
            return [(1, past), (2, future)]

    class _ReplayDB:
        def query(self, *models):
            if len(models) == 1 and models[0] is lifecycle.AIPlan:
                return _OneQuery(plan)
            if len(models) == 1 and models[0] is lifecycle.AIPlanStep.id:
                return _PendingQuery()
            if (
                len(models) == 2
                and models[0] is lifecycle.AIPlanStep.id
                and models[1] is lifecycle.AIPlanStep.scheduled_for
            ):
                return _ScheduledQuery()
            raise AssertionError(f"unexpected models: {models}")

    monkeypatch.setattr(lifecycle, "_lock_user", lambda *_args: user)
    monkeypatch.setattr(
        lifecycle,
        "find_lifecycle_operation",
        lambda *_args: receipt,
    )

    replay = lifecycle.change_delivery_time(
        _ReplayDB(),
        user_id=7,
        slot="DAY",
        hhmm="15:30",
        source_operation_id="coach:time:7",
    )

    assert replay.code == "replayed"
    assert replay.effects[0].target_ids == (2,)


def test_plan_format_and_continuation_interfaces_do_not_implement_deferred_work(
    monkeypatch,
):
    active = SimpleNamespace(id=21, status="active", total_days=7)
    completed = SimpleNamespace(id=20, status="completed", total_days=14)
    monkeypatch.setattr(lifecycle, "_lock_user", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(lifecycle, "get_current_plan", lambda *_args, **_kwargs: active)
    monkeypatch.setattr(
        lifecycle,
        "find_lifecycle_operation",
        lambda *_args, **_kwargs: None,
    )

    class _DeferredDB:
        def query(self, _model):
            return _OneQuery(completed)

        def add(self, _row):
            return None

        def flush(self):
            return None

    db = _DeferredDB()

    switch = lifecycle.request_plan_format_switch(
        db,
        user_id=1,
        target_plan_type="MEDIUM",
        source_operation_id="coach:switch:1",
    )
    assert switch.applied is False
    assert switch.code == "deferred_to_wp_02_3"

    continuation = lifecycle.prepare_continuation(
        db,
        user_id=1,
        completed_plan_id=20,
        source_operation_id="runtime:continuation:20",
    )
    assert continuation.plan_type == "MEDIUM"
    assert continuation.status == "deferred_to_wp_03_5"


def test_deferred_interfaces_reserve_durable_source_receipts(monkeypatch):
    active = SimpleNamespace(id=21, status="active", total_days=7)
    completed = SimpleNamespace(id=20, status="completed", total_days=14)
    monkeypatch.setattr(lifecycle, "_lock_user", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(lifecycle, "get_current_plan", lambda *_args, **_kwargs: active)
    monkeypatch.setattr(
        lifecycle,
        "find_lifecycle_operation",
        lambda *_args, **_kwargs: None,
    )

    class _ReceiptDB:
        def __init__(self, plan=None):
            self.plan = plan
            self.added = []
            self.flushes = 0

        def query(self, _model):
            return _OneQuery(self.plan)

        def add(self, row):
            self.added.append(row)

        def flush(self):
            self.flushes += 1

    switch_db = _ReceiptDB()
    switch = lifecycle.request_plan_format_switch(
        switch_db,
        user_id=1,
        target_plan_type="MEDIUM",
        source_operation_id="coach:switch:1",
    )
    assert switch.applied is False
    assert len(switch_db.added) == 1
    assert switch_db.added[0].plan_id == 21
    assert switch_db.added[0].operation == "switch_plan_format"
    assert switch_db.added[0].result_status == "MEDIUM"
    assert switch_db.flushes == 1

    continuation_db = _ReceiptDB(completed)
    continuation = lifecycle.prepare_continuation(
        continuation_db,
        user_id=1,
        completed_plan_id=20,
        source_operation_id="runtime:continuation:20",
    )
    assert continuation.status == "deferred_to_wp_03_5"
    assert len(continuation_db.added) == 1
    assert continuation_db.added[0].plan_id == 20
    assert continuation_db.added[0].operation == "prepare_continuation"
    assert continuation_db.added[0].result_status == "MEDIUM"
    assert continuation_db.flushes == 1


def test_deferred_interface_replays_reject_material_drift(monkeypatch):
    active = SimpleNamespace(id=21, status="active", total_days=7)
    completed = SimpleNamespace(id=22, status="completed", total_days=7)
    switch_receipt = SimpleNamespace(
        user_id=1,
        plan_id=21,
        plan_step_id=None,
        operation="switch_plan_format",
        result_status="SHORT",
    )
    continuation_receipt = SimpleNamespace(
        user_id=1,
        plan_id=20,
        plan_step_id=None,
        operation="prepare_continuation",
        result_status="MEDIUM",
    )
    monkeypatch.setattr(lifecycle, "_lock_user", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(lifecycle, "get_current_plan", lambda *_args, **_kwargs: active)

    monkeypatch.setattr(
        lifecycle,
        "find_lifecycle_operation",
        lambda *_args, **_kwargs: switch_receipt,
    )
    with pytest.raises(
        lifecycle.LifecycleTransitionError,
        match="already recorded result",
    ):
        lifecycle.request_plan_format_switch(
            object(),
            user_id=1,
            target_plan_type="MEDIUM",
            source_operation_id="coach:switch:1",
        )

    class _CompletedDB:
        def query(self, _model):
            return _OneQuery(completed)

    monkeypatch.setattr(
        lifecycle,
        "find_lifecycle_operation",
        lambda *_args, **_kwargs: continuation_receipt,
    )
    with pytest.raises(
        lifecycle.LifecycleTransitionError,
        match="already belongs to plan 20, not 22",
    ):
        lifecycle.prepare_continuation(
            _CompletedDB(),
            user_id=1,
            completed_plan_id=22,
            source_operation_id="runtime:continuation:20",
        )


def test_deferred_interface_exact_replays_return_existing_reservations(monkeypatch):
    active = SimpleNamespace(id=21, status="active", total_days=7)
    completed = SimpleNamespace(id=20, status="completed", total_days=14)
    switch_receipt = SimpleNamespace(
        user_id=1,
        plan_id=21,
        plan_step_id=None,
        operation="switch_plan_format",
        result_status="MEDIUM",
    )
    continuation_receipt = SimpleNamespace(
        user_id=1,
        plan_id=20,
        plan_step_id=None,
        operation="prepare_continuation",
        result_status="MEDIUM",
    )
    monkeypatch.setattr(lifecycle, "_lock_user", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(lifecycle, "get_current_plan", lambda *_args, **_kwargs: active)
    monkeypatch.setattr(
        lifecycle,
        "find_lifecycle_operation",
        lambda *_args, **_kwargs: switch_receipt,
    )

    switch = lifecycle.request_plan_format_switch(
        object(),
        user_id=1,
        target_plan_type="MEDIUM",
        source_operation_id="coach:switch:1",
    )
    assert switch.duplicate is True
    assert switch.applied is False
    assert switch.details == {"target_plan_type": "MEDIUM"}

    class _CompletedDB:
        def query(self, _model):
            return _OneQuery(completed)

    monkeypatch.setattr(
        lifecycle,
        "find_lifecycle_operation",
        lambda *_args, **_kwargs: continuation_receipt,
    )
    continuation = lifecycle.prepare_continuation(
        _CompletedDB(),
        user_id=1,
        completed_plan_id=20,
        source_operation_id="runtime:continuation:20",
    )
    assert continuation.duplicate is True
    assert continuation.completed_plan_id == 20


def test_live_runtime_has_no_competing_lifecycle_guard_or_ambient_activation_helper():
    app_root = Path("app")
    assert not (app_root / "plan_pause.py").exists()
    assert not (app_root / "plan_guards.py").exists()
    runtime = "\n".join(
        path.read_text(encoding="utf-8") for path in app_root.rglob("*.py")
    )
    assert "activate_plan_side_effects" not in runtime
    assert "def check_ignored_tasks" not in runtime
    assert "asyncio.create_task(send_plan_completion_message" not in runtime
    api_source = (app_root / "api.py").read_text(encoding="utf-8")
    assert "update_user_time_slots(" not in api_source
    assert "change_delivery_time(" in api_source
    internal_activation_callers = {
        path
        for path in app_root.rglob("*.py")
        if "create_plan_for_lifecycle(" in path.read_text(encoding="utf-8")
    }
    assert internal_activation_callers == {
        app_root / "lifecycle.py",
        app_root / "plan_drafts" / "service.py",
    }


def test_compatibility_manifest_maps_every_required_service_surface():
    manifest = Path(
        "docs/implementation/wp_02_2_lifecycle_service_manifest.md"
    ).read_text(encoding="utf-8")

    for method in (
        "require_lifecycle_entitlement()",
        "read_lifecycle_status()",
        "activate_plan()",
        "transition_owned_plan_step()",
        "transition_plan_step(",
        "expire_plan_step()",
        "transition_current_plan()",
        "abandon_current_plan()",
        "change_delivery_time()",
        "record_evening_time_preference()",
        "request_plan_format_switch()",
        "complete_current_plan_if_ready()",
        "prepare_continuation()",
    ):
        assert method in manifest
    assert "WP-02.3" in manifest
    assert "WP-03.5" in manifest

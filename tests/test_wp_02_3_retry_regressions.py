"""Committed-control retry and replacement-activation regressions."""

from contextlib import nullcontext
from dataclasses import replace

import pytest

from app import db as database
from app import lifecycle, lifecycle_reconciliation, scheduler
from app.plan_runtime import tools


class _Session:
    def __init__(self):
        self.commits = 0

    def commit(self):
        self.commits += 1


def _switch_result(*, with_keyboard: bool = False):
    effects = [
        lifecycle.ExternalEffect("reconcile_plan_schedule", (11,)),
        lifecycle.ExternalEffect("reconcile_plan_schedule", (23,)),
    ]
    if with_keyboard:
        effects.insert(0, lifecycle.ExternalEffect("remove_step_keyboards", (41,)))
    return lifecycle.LifecycleResult(
        user_id=7, plan_id=23, status="active", operation="switch_plan_format",
        effects=tuple(effects),
        details={
            "source_plan_id": 11,
            "switch_source_operation_id": "coach:s1",
            "total_days": 14,
        },
    )


def test_fresh_retry_routes_only_the_current_switch_receipt(monkeypatch):
    session = _Session()
    decision = _switch_result()
    captured = []
    monkeypatch.setattr(database, "SessionLocal", lambda: nullcontext(session))

    def recover(db, **kwargs):
        captured.append((db, kwargs))
        return decision

    monkeypatch.setattr(lifecycle, "recover_switch_plan_format", recover)
    monkeypatch.setattr(
        tools, "_finish_plan_format_result",
        lambda result, **_kwargs: {"status": "ok", "plan_id": result.plan_id},
    )

    response = tools.retry_switch_plan_format(
        7, switch_source_operation_id="coach:s1"
    )

    assert captured == [(session, {
        "user_id": 7, "switch_source_operation_id": "coach:s1",
    })]
    assert session.commits == 1
    assert response == {"status": "ok", "plan_id": 23}


@pytest.mark.parametrize(
    ("action", "operation", "effect_kind"),
    [
        ("pause", "pause", "reconcile_plan_schedule"),
        ("resume", "resume", "reconcile_plan_schedule"),
        ("cancel", "abandon", "cancel_step_jobs"),
        ("followup", "activate", "reconcile_plan_schedule"),
    ],
)
def test_fresh_control_retry_uses_exact_original_receipt(
    monkeypatch, action, operation, effect_kind,
):
    session = _Session()
    captured = []
    monkeypatch.setattr(database, "SessionLocal", lambda: nullcontext(session))

    def recover(db, **kwargs):
        captured.append((db, kwargs))
        return lifecycle.LifecycleResult(
            user_id=7, plan_id=23, status="active", operation=operation,
            duplicate=True,
            effects=(lifecycle.ExternalEffect(effect_kind, (23,)),),
            details={"total_days": 7},
        )

    monkeypatch.setattr(lifecycle, "recover_plan_action", recover)
    monkeypatch.setattr(
        lifecycle_reconciliation,
        "reconcile_scheduler_effects",
        lambda result: replace(
            result,
            effects=(replace(
                result.effects[0], state=lifecycle.ExternalEffectState.SUCCEEDED
            ),),
        ),
    )

    response = tools.retry_plan_action(7, action, "coach:original")

    expected_call = (session, {
        "user_id": 7,
        "action": action,
        "original_source_operation_id": "coach:original",
    })
    assert captured == [expected_call, expected_call]
    assert session.commits == 1
    assert response["status"] == "ok"
    assert response["action"] == action
    assert response["original_source_operation_id"] == "coach:original"


def test_followup_event_failure_does_not_hide_ready_schedule(monkeypatch):
    session = _Session()
    monkeypatch.setattr(database, "SessionLocal", lambda: nullcontext(session))
    monkeypatch.setattr(
        lifecycle,
        "recover_plan_action",
        lambda *_a, **_k: lifecycle.LifecycleResult(
            user_id=7, plan_id=23, status="active", operation="activate",
            duplicate=True,
            effects=(lifecycle.ExternalEffect("reconcile_plan_schedule", (23,)),),
            details={"total_days": 7},
        ),
    )
    monkeypatch.setattr(
        scheduler, "reconcile_plan_schedule",
        lambda _plan_id: scheduler.SchedulerReconciliation(1, 1),
    )
    monkeypatch.setattr(
        lifecycle_reconciliation,
        "_record_activation_event",
        lambda _result: (_ for _ in ()).throw(RuntimeError("event unavailable")),
    )

    response = tools.retry_plan_action(7, "followup", "coach:followup")

    assert response["status"] == "ok"
    assert response["activation_event_pending"] is True
    assert response["keyboard_cleanup_pending"] is False


def test_cancel_keyboard_failure_does_not_claim_job_failure(monkeypatch):
    session = _Session()
    monkeypatch.setattr(database, "SessionLocal", lambda: nullcontext(session))
    monkeypatch.setattr(
        lifecycle,
        "recover_plan_action",
        lambda *_a, **_k: lifecycle.LifecycleResult(
            user_id=7, plan_id=23, status="abandoned", operation="abandon",
            duplicate=True,
            effects=(
                lifecycle.ExternalEffect("cancel_step_jobs", (41,)),
                lifecycle.ExternalEffect("remove_step_keyboards", (41,)),
            ),
        ),
    )
    monkeypatch.setattr(
        scheduler, "reconcile_cancel_plan_step_jobs",
        lambda ids: scheduler.SchedulerReconciliation(len(ids), len(ids)),
    )
    monkeypatch.setattr(
        scheduler, "reconcile_terminal_step_keyboards",
        lambda ids: scheduler.SchedulerReconciliation(len(ids), 0, tuple(ids)),
    )

    response = tools.retry_plan_action(7, "cancel", "coach:cancel")

    assert response["status"] == "ok"
    assert response["keyboard_cleanup_pending"] is True
    assert response["activation_event_pending"] is False


@pytest.mark.parametrize(
    ("action", "operation", "effect_kind", "scheduler_name", "error_code"),
    [
        ("pause", "pause", "reconcile_plan_schedule", "reconcile_plan_schedule", "pause_reconciliation_failed"),
        ("resume", "resume", "reconcile_plan_schedule", "reconcile_plan_schedule", "resume_reconciliation_failed"),
        ("cancel", "abandon", "cancel_step_jobs", "reconcile_cancel_plan_step_jobs", "cancel_reconciliation_failed"),
        ("followup", "activate", "reconcile_plan_schedule", "reconcile_plan_schedule", "activation_reconciliation_failed"),
    ],
)
def test_exact_control_retry_repeats_failed_effect_until_proven(
    monkeypatch, action, operation, effect_kind, scheduler_name, error_code,
):
    session = _Session()
    replayed = []
    attempts = []
    monkeypatch.setattr(database, "SessionLocal", lambda: nullcontext(session))

    def recover(_db, **kwargs):
        replayed.append(kwargs["original_source_operation_id"])
        return lifecycle.LifecycleResult(
            user_id=7, plan_id=23, status="active", operation=operation,
            duplicate=True,
            effects=(lifecycle.ExternalEffect(effect_kind, (41,)),),
            details={"total_days": 7},
        )

    def reconcile(_target):
        attempts.append(action)
        if len(attempts) == 1:
            return scheduler.SchedulerReconciliation(1, 0, (41,))
        return scheduler.SchedulerReconciliation(1, 1)

    monkeypatch.setattr(lifecycle, "recover_plan_action", recover)
    monkeypatch.setattr(scheduler, scheduler_name, reconcile)
    monkeypatch.setattr(
        lifecycle_reconciliation, "_record_activation_event", lambda _result: None
    )

    first = tools.retry_plan_action(7, action, "coach:original")
    second = tools.retry_plan_action(7, action, "coach:original")

    assert first["status"] == "error"
    assert first["code"] == error_code
    assert second["status"] == "ok"
    assert replayed == ["coach:original"] * 4
    assert attempts == [action, action]
    assert session.commits == 2


def test_control_retry_rechecks_authority_after_external_proof(monkeypatch):
    session = _Session()
    calls = []
    monkeypatch.setattr(database, "SessionLocal", lambda: nullcontext(session))

    def recover(_db, **_kwargs):
        calls.append(True)
        if len(calls) == 2:
            raise lifecycle.LifecycleTransitionError("retry_action_superseded")
        return lifecycle.LifecycleResult(
            user_id=7, plan_id=23, status="paused", operation="pause",
            duplicate=True,
            effects=(lifecycle.ExternalEffect("reconcile_plan_schedule", (23,)),),
        )

    monkeypatch.setattr(lifecycle, "recover_plan_action", recover)
    monkeypatch.setattr(
        scheduler, "reconcile_plan_schedule",
        lambda _plan_id: scheduler.SchedulerReconciliation(1, 1),
    )

    response = tools.retry_plan_action(7, "pause", "coach:pause-original")

    assert response["status"] == "error"
    assert response["code"] == "superseded"
    assert response["disposition"] == "superseded"
    assert calls == [True, True]


def test_event_only_failure_keeps_switch_operational_and_retries(monkeypatch):
    session = _Session()
    monkeypatch.setattr(database, "SessionLocal", lambda: nullcontext(session))
    monkeypatch.setattr(
        scheduler, "reconcile_plan_schedule",
        lambda _plan_id: scheduler.SchedulerReconciliation(1, 1),
    )
    ready = []
    monkeypatch.setattr(
        lifecycle, "record_switch_schedule_ready",
        lambda _db, **kwargs: ready.append(kwargs["plan_id"]),
    )
    events = []

    def record(_result):
        events.append(_result.plan_id)
        if len(events) == 1:
            raise RuntimeError("event unavailable")

    monkeypatch.setattr(lifecycle_reconciliation, "_record_activation_event", record)

    first = tools._finish_plan_format_result(
        _switch_result(), pending_status="needs_evening_time"
    )
    second = tools._finish_plan_format_result(
        _switch_result(), pending_status="needs_evening_time"
    )

    assert first["status"] == second["status"] == "ok"
    assert first["jobs_reconciled"] is True
    assert first["activation_event_pending"] is True
    assert second["activation_event_pending"] is False
    assert ready == [23, 23]
    assert events == [23, 23]


def test_paused_switch_runtime_preserves_state_and_proves_both_schedules(monkeypatch):
    session = _Session()
    monkeypatch.setattr(database, "SessionLocal", lambda: nullcontext(session))
    targets = []
    monkeypatch.setattr(
        scheduler, "reconcile_plan_schedule",
        lambda plan_id: (
            targets.append(plan_id)
            or scheduler.SchedulerReconciliation(1, 1)
        ),
    )
    monkeypatch.setattr(
        lifecycle_reconciliation, "_record_activation_event", lambda _result: None
    )
    monkeypatch.setattr(
        lifecycle, "record_switch_schedule_ready", lambda *_a, **_k: None
    )

    response = tools._finish_plan_format_result(
        replace(_switch_result(), status="paused"),
        pending_status="needs_evening_time",
    )

    assert response["status"] == "ok"
    assert response["plan_status"] == "paused"
    assert response["jobs_reconciled"] is True
    assert targets == [11, 23]


def test_keyboard_failure_is_hygiene_not_switch_gate(monkeypatch):
    session = _Session()
    monkeypatch.setattr(database, "SessionLocal", lambda: nullcontext(session))
    monkeypatch.setattr(
        scheduler, "reconcile_plan_schedule",
        lambda _plan_id: scheduler.SchedulerReconciliation(1, 1),
    )
    monkeypatch.setattr(
        scheduler, "reconcile_terminal_step_keyboards",
        lambda ids: scheduler.SchedulerReconciliation(
            len(ids), 0, tuple(ids)
        ),
    )
    monkeypatch.setattr(
        lifecycle_reconciliation, "_record_activation_event", lambda _result: None
    )
    ready = []
    monkeypatch.setattr(
        lifecycle, "record_switch_schedule_ready",
        lambda _db, **kwargs: ready.append(kwargs["plan_id"]),
    )

    response = tools._finish_plan_format_result(
        _switch_result(with_keyboard=True),
        pending_status="needs_evening_time",
    )

    assert response["status"] == "ok"
    assert response["keyboard_cleanup_pending"] is True
    assert response["activation_event_pending"] is False
    assert ready == [23]


def test_schedule_failure_blocks_switch_ready_receipt_until_retry(monkeypatch):
    session = _Session()
    monkeypatch.setattr(database, "SessionLocal", lambda: nullcontext(session))
    attempts = []

    def schedule(plan_id):
        attempts.append(plan_id)
        if len(attempts) == 1:
            return scheduler.SchedulerReconciliation(1, 0, (plan_id,))
        return scheduler.SchedulerReconciliation(1, 1)

    monkeypatch.setattr(scheduler, "reconcile_plan_schedule", schedule)
    monkeypatch.setattr(
        lifecycle_reconciliation, "_record_activation_event", lambda _result: None
    )
    ready = []
    monkeypatch.setattr(
        lifecycle, "record_switch_schedule_ready",
        lambda _db, **kwargs: ready.append(kwargs["plan_id"]),
    )

    first = tools._finish_plan_format_result(
        _switch_result(), pending_status="needs_evening_time"
    )
    second = tools._finish_plan_format_result(
        _switch_result(), pending_status="needs_evening_time"
    )

    assert first["status"] == "error"
    assert first["code"] == "switch_reconciliation_failed"
    assert first["jobs_reconciled"] is False
    assert ready == [23]
    assert second["status"] == "ok"
    assert attempts == [11, 23, 11, 23]


def test_deferred_switch_does_not_emit_activation_event(monkeypatch):
    calls = []
    monkeypatch.setattr(lifecycle_reconciliation, "_record_activation_event", calls.append)
    result = lifecycle.LifecycleResult(
        user_id=7, plan_id=11, status="active", operation="switch_plan_format",
        code="needs_evening_time", applied=False,
    )
    assert lifecycle_reconciliation.reconcile_scheduler_effects(result) == result
    assert calls == []

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

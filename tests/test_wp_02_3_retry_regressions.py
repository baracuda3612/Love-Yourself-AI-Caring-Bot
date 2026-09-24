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


@pytest.mark.parametrize("operation", ["pause", "resume", "switch_plan_format", "activate"])
def test_fresh_retry_call_reconciles_durable_control_without_reapplying_decision(
    monkeypatch, operation
):
    session = _Session()
    captured = []
    effect = lifecycle.ExternalEffect("reconcile_plan_schedule", (23,))
    original = lifecycle.LifecycleResult(
        user_id=7,
        plan_id=23,
        status="active",
        operation=operation,
        duplicate=True,
        code="replayed",
        effects=(effect,),
        details={"total_days": 14},
    )
    monkeypatch.setattr(database, "SessionLocal", lambda: nullcontext(session))

    def recover(db, **kwargs):
        captured.append((db, kwargs))
        return original

    monkeypatch.setattr(lifecycle, "recover_latest_runtime_control", recover)
    monkeypatch.setattr(
        lifecycle_reconciliation,
        "reconcile_scheduler_effects",
        lambda result: replace(
            result,
            effects=(replace(effect, state=lifecycle.ExternalEffectState.SUCCEEDED),),
        ),
    )

    result = tools.retry_plan_action(7, source_operation_id="coach:fresh-retry")

    assert captured == [(session, {"user_id": 7, "source_operation_id": "coach:fresh-retry"})]
    assert session.commits == 1
    assert result == {
        "status": "ok",
        "plan_id": 23,
        "operation": operation,
        "duplicate": True,
        "disposition": "replayed",
    }


def test_retry_reports_partial_effect_without_claiming_success(monkeypatch):
    session = _Session()
    effect = lifecycle.ExternalEffect("reconcile_plan_schedule", (23,))
    original = lifecycle.LifecycleResult(
        user_id=7, plan_id=23, status="paused", operation="pause",
        duplicate=True, effects=(effect,),
    )
    monkeypatch.setattr(database, "SessionLocal", lambda: nullcontext(session))
    monkeypatch.setattr(lifecycle, "recover_latest_runtime_control", lambda *_a, **_k: original)
    monkeypatch.setattr(
        lifecycle_reconciliation,
        "reconcile_scheduler_effects",
        lambda result: replace(
            result,
            effects=(replace(effect, state=lifecycle.ExternalEffectState.FAILED),),
        ),
    )

    result = tools.retry_plan_action(7, source_operation_id="coach:fresh-retry")

    assert session.commits == 1
    assert result["status"] == "error"
    assert result["code"] == "retry_reconciliation_failed"
    assert result["persisted"] is True


@pytest.mark.parametrize("operation", ["activate", "switch_plan_format"])
def test_replacement_activation_event_retried_after_failure(monkeypatch, operation):
    effect = lifecycle.ExternalEffect("reconcile_plan_schedule", (23,))
    result = lifecycle.LifecycleResult(
        user_id=7, plan_id=23, status="active", operation=operation,
        effects=(effect,), details={"total_days": 14},
    )
    monkeypatch.setattr(
        scheduler,
        "reconcile_plan_schedule",
        lambda _plan_id: scheduler.SchedulerReconciliation(
            attempted=1, succeeded=1, failed_ids=(),
        ),
    )
    attempts = []

    def record(_result):
        attempts.append(_result.plan_id)
        if len(attempts) == 1:
            raise RuntimeError("event store temporarily unavailable")

    monkeypatch.setattr(lifecycle_reconciliation, "_record_activation_event", record)

    failed = lifecycle_reconciliation.reconcile_scheduler_effects(result)
    replayed = lifecycle_reconciliation.reconcile_scheduler_effects(result)

    assert failed.external_effects_succeeded is False
    assert failed.effects[0].error_code == "activation_event_failed"
    assert replayed.external_effects_succeeded is True
    assert attempts == [23, 23]


def test_deferred_switch_does_not_emit_activation_event(monkeypatch):
    calls = []
    monkeypatch.setattr(lifecycle_reconciliation, "_record_activation_event", calls.append)
    result = lifecycle.LifecycleResult(
        user_id=7, plan_id=11, status="active", operation="switch_plan_format",
        code="needs_evening_time", applied=False,
    )
    assert lifecycle_reconciliation.reconcile_scheduler_effects(result) == result
    assert calls == []

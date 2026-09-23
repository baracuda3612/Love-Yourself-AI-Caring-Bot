from __future__ import annotations

from contextlib import nullcontext

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app import api, lifecycle


def test_time_slot_api_rejects_retired_morning_slot():
    with pytest.raises(ValidationError):
        api.TimeSlotsPayload(DAY="14:00", MORNING="09:30")


class _DB:
    def __init__(self):
        self.commits = 0

    def commit(self):
        self.commits += 1


def test_time_slot_api_reports_superseded_values_without_reconciliation(monkeypatch):
    db = _DB()
    monkeypatch.setattr(api, "SessionLocal", lambda: nullcontext(db))

    def _superseded(_db, *, user_id, slot, hhmm, source_operation_id):
        assert user_id == 1
        assert source_operation_id == f"request-1:{slot.lower()}"
        return lifecycle.LifecycleResult(
            user_id=user_id,
            plan_id=11,
            status=hhmm,
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
                "value": hhmm,
                "authoritative_value": "16:45",
            },
        )

    monkeypatch.setattr(api, "change_delivery_time", _superseded)
    monkeypatch.setattr(
        api,
        "reconcile_scheduler_effects",
        lambda _decision: pytest.fail("superseded replay must not reconcile"),
    )

    with pytest.raises(HTTPException) as caught:
        api.set_user_time_slots(
            api.TimeSlotsPayload(
                DAY="15:30",
                EVENING="20:30",
            ),
            user_id=1,
            idempotency_key="request-1",
        )

    assert caught.value.status_code == 409
    assert caught.value.detail == {
        "code": "superseded_time_change",
        "saved": False,
        "authoritative_values": {
                "DAY": "16:45",
            "EVENING": "16:45",
        },
    }
    assert db.commits == 1


def test_paused_time_slot_api_reports_saved_but_deferred(monkeypatch):
    db = _DB()
    monkeypatch.setattr(api, "SessionLocal", lambda: nullcontext(db))

    def _deferred(_db, *, user_id, slot, hhmm, source_operation_id):
        return lifecycle.LifecycleResult(
            user_id=user_id,
            plan_id=11,
            status=hhmm,
            operation=f"change_{slot.lower()}_time",
            effects=(
                lifecycle.ExternalEffect(
                    kind="reconcile_plan_schedule",
                    target_ids=(11,),
                    state=lifecycle.ExternalEffectState.DEFERRED,
                ),
            ),
            details={"slot": slot, "value": hhmm, "updated_step_ids": ()},
        )

    monkeypatch.setattr(api, "change_delivery_time", _deferred)

    outcome = api.set_user_time_slots(
        api.TimeSlotsPayload(DAY="14:00", EVENING="20:30"),
        user_id=1,
        idempotency_key="paused-1",
    )

    assert outcome == {
        "updated_steps": 0,
        "jobs_reconciled": False,
        "schedule_state": "deferred",
    }

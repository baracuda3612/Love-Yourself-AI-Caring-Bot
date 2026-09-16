from __future__ import annotations

from contextlib import nullcontext

import pytest
from fastapi import HTTPException

from app import api, lifecycle


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
                MORNING="09:00",
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
            "MORNING": "16:45",
            "DAY": "16:45",
            "EVENING": "16:45",
        },
    }
    assert db.commits == 1

"""Deterministic A commit -> B commit -> A effects -> A reply timelines."""

from contextlib import nullcontext
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import api, db as database, lifecycle, lifecycle_reconciliation, orchestrator
from app.plan_runtime import tools


class _Session:
    def __init__(self, state):
        self.state = state

    def commit(self):
        self.state.commits += 1

    def query(self, _model):
        return self

    def filter(self, *_args):
        return self

    def with_for_update(self):
        return self

    def first(self):
        return SimpleNamespace(
            id=1,
            daily_time_slots={"DAY": self.state.day, "EVENING": self.state.evening},
            evening_slot_collected=True,
        )


def _result(operation, status, *, plan_id=11, plan_type="SHORT", slot=None):
    return lifecycle.LifecycleResult(
        user_id=1,
        plan_id=plan_id,
        plan_type=plan_type,
        status=status,
        operation=operation,
        effects=(lifecycle.ExternalEffect("reconcile_plan_schedule", (plan_id,)),),
        details={"slot": slot, "value": status} if slot else {},
    )


@pytest.mark.parametrize(
    ("tool_name", "first_status", "later_status", "first_jobs", "later_jobs"),
    [
        ("pause_plan", "paused", "active", set(), {"plan:11:day"}),
        ("resume_plan", "active", "paused", {"plan:11:day"}, set()),
    ],
)
@pytest.mark.asyncio
async def test_control_reply_uses_later_committed_state(
    monkeypatch, tool_name, first_status, later_status, first_jobs, later_jobs
):
    state = SimpleNamespace(status=first_status, jobs=set(first_jobs), commits=0)
    monkeypatch.setattr(database, "SessionLocal", lambda: nullcontext(_Session(state)))
    monkeypatch.setattr(
        lifecycle,
        "transition_current_plan",
        lambda *_args, **_kwargs: _result(tool_name.removesuffix("_plan"), first_status),
    )

    def reconcile(result):
        # A committed above. B commits the opposite control before A's effect.
        state.status = later_status
        state.commits += 1
        state.jobs = set(later_jobs)
        return replace(result, effects=(replace(
            result.effects[0], state=lifecycle.ExternalEffectState.SUCCEEDED
        ),))

    monkeypatch.setattr(lifecycle_reconciliation, "reconcile_scheduler_effects", reconcile)
    monkeypatch.setattr(
        lifecycle, "read_post_effect_truth", lambda *_args, **_kwargs:
        SimpleNamespace(code="superseded", authoritative_value=None, historical_cleanup=False),
        raising=False,
    )
    response = getattr(tools, tool_name)(1, source_operation_id="A")
    monkeypatch.setattr(orchestrator, "_build_tool_registry", lambda: {
        tool_name: lambda *_args, **_kwargs: response
    })
    copy = await orchestrator._execute_plan_tool(1, {"name": tool_name, "call_id": "A"})
    assert state.commits == 2
    assert state.jobs == later_jobs
    assert response["code"] == "superseded"
    assert "застарів" in copy


@pytest.mark.parametrize(("tool_name", "slot"), [
    ("change_day_time", "DAY"), ("change_evening_time", "EVENING")
])
def test_time_wrapper_reply_uses_later_committed_slot(monkeypatch, tool_name, slot):
    state = SimpleNamespace(day="15:30", evening="15:30", jobs={"15:30"}, commits=0)
    monkeypatch.setattr(database, "SessionLocal", lambda: nullcontext(_Session(state)))
    monkeypatch.setattr(lifecycle, "change_delivery_time", lambda *_args, **_kwargs:
                        _result(f"change_{slot.lower()}_time", "15:30", slot=slot))

    def reconcile(result):
        setattr(state, slot.lower(), "16:45")
        state.commits += 1
        state.jobs = {"16:45"}
        return replace(result, effects=(replace(
            result.effects[0], state=lifecycle.ExternalEffectState.SUCCEEDED
        ),))

    monkeypatch.setattr(lifecycle_reconciliation, "reconcile_scheduler_effects", reconcile)
    monkeypatch.setattr(lifecycle, "read_post_effect_truth", lambda *_args, **_kwargs:
                        SimpleNamespace(code="superseded", authoritative_value="16:45", historical_cleanup=False), raising=False)
    response = getattr(tools, tool_name)(1, "15:30", source_operation_id="A")
    assert state.commits == 2
    assert state.jobs == {"16:45"}
    assert response["code"] == "superseded"
    assert response[f"{slot.lower()}_time"] == "16:45"
    assert response["saved"] is True


def test_composite_time_api_rejects_old_success_after_later_commit(monkeypatch):
    state = SimpleNamespace(day="15:30", evening="20:30", jobs={"15:30"}, commits=0)
    monkeypatch.setattr(api, "SessionLocal", lambda: nullcontext(_Session(state)))
    monkeypatch.setattr(api, "change_delivery_time", lambda *_args, **_kwargs:
                        _result("change_day_time", "15:30", slot="DAY"))

    def reconcile(result):
        state.day = "16:45"
        state.commits += 1
        state.jobs = {"16:45"}
        return replace(result, effects=(replace(
            result.effects[0], state=lifecycle.ExternalEffectState.SUCCEEDED
        ),))

    monkeypatch.setattr(api, "reconcile_scheduler_effects", reconcile)
    monkeypatch.setattr(api, "read_post_effect_truth", lambda *_args, **_kwargs:
                        SimpleNamespace(code="superseded", authoritative_value="16:45", historical_cleanup=False))
    with pytest.raises(HTTPException) as caught:
        api.set_user_time_slots(api.TimeSlotsPayload(DAY="15:30"), user_id=1, idempotency_key="A")
    assert state.commits == 2
    assert state.jobs == {"16:45"}
    assert caught.value.status_code == 409
    assert caught.value.detail["code"] == "superseded_time_change"
    assert caught.value.detail["saved"] is True


def test_composite_time_api_keeps_current_slot_in_mixed_post_effect_reply(monkeypatch):
    state = SimpleNamespace(jobs={"DAY:15:30", "EVENING:20:30"}, commits=0)
    monkeypatch.setattr(api, "SessionLocal", lambda: nullcontext(_Session(state)))
    monkeypatch.setattr(api, "change_delivery_time", lambda _db, *, slot, hhmm, **_kwargs:
                        _result(f"change_{slot.lower()}_time", hhmm, slot=slot))

    def reconcile(result):
        if result.details["slot"] == "DAY":
            state.commits += 1  # B changes DAY while A reconciles.
            state.jobs = {"DAY:16:45", "EVENING:20:30"}
        return replace(result, effects=(replace(
            result.effects[0], state=lifecycle.ExternalEffectState.SUCCEEDED
        ),))

    monkeypatch.setattr(api, "reconcile_scheduler_effects", reconcile)
    monkeypatch.setattr(api, "read_post_effect_truth", lambda _db, *, result, **_kwargs:
                        SimpleNamespace(
                            code="superseded" if result.details["slot"] == "DAY" else "current",
                            authoritative_value="16:45" if result.details["slot"] == "DAY" else "20:30",
                            historical_cleanup=False,
                        ))
    with pytest.raises(HTTPException) as caught:
        api.set_user_time_slots(
            api.TimeSlotsPayload(DAY="15:30", EVENING="20:30"),
            user_id=1, idempotency_key="A",
        )
    assert state.commits == 2
    assert state.jobs == {"DAY:16:45", "EVENING:20:30"}
    assert caught.value.status_code == 409
    assert caught.value.detail["code"] == "mixed_time_slot_outcome"
    assert caught.value.detail["slots"]["DAY"] == {
        "status": "superseded", "saved": True,
        "requested_value": "15:30", "authoritative_value": "16:45",
    }
    assert caught.value.detail["slots"]["EVENING"]["status"] == "applied"


@pytest.mark.asyncio
async def test_post_effect_read_failure_reports_partial_not_success(monkeypatch):
    state = SimpleNamespace(status="paused", jobs=set(), commits=0)
    monkeypatch.setattr(database, "SessionLocal", lambda: nullcontext(_Session(state)))
    monkeypatch.setattr(lifecycle, "transition_current_plan", lambda *_args, **_kwargs:
                        _result("pause", "paused"))
    monkeypatch.setattr(lifecycle_reconciliation, "reconcile_scheduler_effects", lambda result:
                        replace(result, effects=(replace(
                            result.effects[0], state=lifecycle.ExternalEffectState.SUCCEEDED
                        ),)))
    monkeypatch.setattr(lifecycle, "read_post_effect_truth", lambda *_args, **_kwargs:
                        (_ for _ in ()).throw(RuntimeError("read unavailable")))
    response = tools.pause_plan(1, source_operation_id="A")
    monkeypatch.setattr(orchestrator, "_build_tool_registry", lambda: {
        "pause_plan": lambda *_args, **_kwargs: response
    })
    copy = await orchestrator._execute_plan_tool(1, {"name": "pause_plan", "call_id": "A"})
    assert state.jobs == set()
    assert response["code"] == "postproof_failed"
    assert response["persisted"] is True
    assert "не вдалося остаточно перевірити" in copy.lower()


@pytest.mark.asyncio
async def test_cancel_reply_describes_historical_cleanup_after_followup(monkeypatch):
    state = SimpleNamespace(current=11, jobs=set(), commits=0)
    monkeypatch.setattr(database, "SessionLocal", lambda: nullcontext(_Session(state)))
    monkeypatch.setattr(lifecycle, "abandon_current_plan", lambda *_args, **_kwargs:
                        (_result("abandon", "abandoned"), []))

    def reconcile(result):
        state.current = 12  # B's follow-up committed.
        state.commits += 1
        state.jobs = {"plan:12:day"}
        return replace(result, effects=(replace(
            result.effects[0], state=lifecycle.ExternalEffectState.SUCCEEDED
        ),))

    monkeypatch.setattr(lifecycle_reconciliation, "reconcile_scheduler_effects", reconcile)
    monkeypatch.setattr(lifecycle, "read_post_effect_truth", lambda *_args, **_kwargs:
                        SimpleNamespace(code="historical_cleanup", authoritative_value=None, historical_cleanup=True), raising=False)
    response = tools.cancel_plan(1, source_operation_id="A")
    monkeypatch.setattr(orchestrator, "_build_tool_registry", lambda: {
        "cancel_plan": lambda *_args, **_kwargs: response
    })
    copy = await orchestrator._execute_plan_tool(1, {"name": "cancel_plan", "call_id": "A"})
    assert state.commits == 2
    assert state.jobs == {"plan:12:day"}
    assert response["historical_cleanup"] is True
    assert "поточний план не змінено" in copy


@pytest.mark.asyncio
async def test_followup_reply_rejects_later_pause(monkeypatch):
    state = SimpleNamespace(status="active", jobs={"plan:12:day"}, commits=0)
    monkeypatch.setattr(database, "SessionLocal", lambda: nullcontext(_Session(state)))
    monkeypatch.setattr(tools, "_load_user_and_profile", lambda *_args, **_kwargs:
                        (SimpleNamespace(id=1), SimpleNamespace(daily_time_slots={"DAY": "14:00"})))
    monkeypatch.setattr(lifecycle, "activate_plan", lambda *_args, **_kwargs:
                        _result("activate", "active", plan_id=12))

    def reconcile(result):
        state.status = "paused"  # B's pause committed before A's effect.
        state.commits += 1
        state.jobs.clear()
        return replace(result, effects=(replace(
            result.effects[0], state=lifecycle.ExternalEffectState.SUCCEEDED
        ),))

    monkeypatch.setattr(lifecycle_reconciliation, "reconcile_scheduler_effects", reconcile)
    monkeypatch.setattr(lifecycle, "read_post_effect_truth", lambda *_args, **_kwargs:
                        SimpleNamespace(code="superseded", authoritative_value=None, historical_cleanup=False), raising=False)
    response = tools.create_followup_plan(1, "SHORT", source_operation_id="A")
    monkeypatch.setattr(orchestrator, "_build_tool_registry", lambda: {
        "create_followup_plan": lambda *_args, **_kwargs: response
    })
    copy = await orchestrator._execute_plan_tool(1, {"name": "create_followup_plan", "call_id": "A"})
    assert state.commits == 2
    assert state.jobs == set()
    assert response["code"] == "superseded"
    assert "застарів" in copy

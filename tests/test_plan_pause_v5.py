from types import SimpleNamespace

import pytest

from app import plan_pause
from app.lifecycle import LifecycleTransitionError


def test_pause_delegates_to_authoritative_plan_transition(monkeypatch):
    captured = {}
    expected = SimpleNamespace(status="paused", duplicate=False)

    def fake_transition(db, **kwargs):
        captured.update(kwargs)
        return expected

    monkeypatch.setattr(plan_pause, "transition_current_plan", fake_transition)
    result = plan_pause.pause_plan(object(), 7, source_operation_id="callback:1")

    assert result is expected
    assert captured == {
        "user_id": 7,
        "operation": "pause",
        "source_operation_id": "callback:1",
    }


def test_pause_maps_stale_state_to_honest_error(monkeypatch):
    def reject(*_args, **_kwargs):
        raise LifecycleTransitionError("pause requires ['active'], got completed")

    monkeypatch.setattr(plan_pause, "transition_current_plan", reject)
    with pytest.raises(plan_pause.PlanNotActiveError, match="got completed"):
        plan_pause.pause_plan(object(), 7, source_operation_id="callback:2")


def test_pause_maps_duplicate_paused_state(monkeypatch):
    def reject(*_args, **_kwargs):
        raise LifecycleTransitionError("pause requires ['active'], got paused")

    monkeypatch.setattr(plan_pause, "transition_current_plan", reject)
    with pytest.raises(plan_pause.PlanAlreadyPausedError):
        plan_pause.pause_plan(object(), 7, source_operation_id="callback:3")


def test_resume_delegates_without_profile_or_user_mirror(monkeypatch):
    captured = {}
    expected = SimpleNamespace(status="active", duplicate=False)

    def fake_transition(db, **kwargs):
        captured.update(kwargs)
        return expected

    monkeypatch.setattr(plan_pause, "transition_current_plan", fake_transition)
    result = plan_pause.resume_plan(object(), 9, source_operation_id="callback:4")

    assert result is expected
    assert captured == {
        "user_id": 9,
        "operation": "resume",
        "source_operation_id": "callback:4",
    }


def test_resume_rejects_non_paused_plan(monkeypatch):
    def reject(*_args, **_kwargs):
        raise LifecycleTransitionError("resume requires ['paused'], got active")

    monkeypatch.setattr(plan_pause, "transition_current_plan", reject)
    with pytest.raises(plan_pause.PlanNotPausedError, match="got active"):
        plan_pause.resume_plan(object(), 9, source_operation_id="callback:5")

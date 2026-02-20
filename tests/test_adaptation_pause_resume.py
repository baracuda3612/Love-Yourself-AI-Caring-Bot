from unittest.mock import MagicMock, patch

import pytest

from app.adaptation_executor import AdaptationExecutor
from app.adaptation_types import AdaptationIntent, AdaptationNotEligibleError


def _make_plan(status: str, plan_id: int = 1, user_id: int = 42) -> MagicMock:
    plan = MagicMock()
    plan.id = plan_id
    plan.status = status
    plan.user_id = user_id
    return plan


def _make_db(plan: MagicMock) -> MagicMock:
    db = MagicMock()
    db.query.return_value.options.return_value.filter.return_value.first.return_value = plan
    return db


def test_pause_updates_status_logs_event_cancels_jobs():
    plan = _make_plan("active")
    db = _make_db(plan)
    result = MagicMock()
    result.canceled_step_ids = [10, 11, 12]
    result.rescheduled_step_ids = []

    with patch("app.adaptation_executor.apply_plan_adaptation", return_value=result) as mock_apply, \
         patch("app.adaptation_executor.cancel_plan_step_jobs") as mock_cancel, \
         patch("app.adaptation_executor.log_user_event") as mock_log:

        step_ids = AdaptationExecutor().execute(db, plan.id, AdaptationIntent.PAUSE_PLAN)

    assert plan.status == "paused"
    call_payload = mock_apply.call_args[1]["adaptation_payload"]
    assert call_payload["adaptation_type"] == "pause"
    mock_cancel.assert_called_once_with([10, 11, 12])
    mock_log.assert_called_once()
    log_kwargs = mock_log.call_args[1]
    assert log_kwargs["event_type"] == "plan_paused"
    assert step_ids == []  # pause не повертає ids для reschedule


def test_pause_raises_when_already_paused():
    plan = _make_plan("paused")
    db = _make_db(plan)

    with patch("app.adaptation_executor.apply_plan_adaptation") as mock_apply:
        with pytest.raises(AdaptationNotEligibleError) as exc_info:
            AdaptationExecutor().execute(db, plan.id, AdaptationIntent.PAUSE_PLAN)

    assert exc_info.value.reason == "already_paused"
    mock_apply.assert_not_called()


def test_pause_skips_cancel_when_no_future_steps():
    plan = _make_plan("active")
    db = _make_db(plan)
    result = MagicMock()
    result.canceled_step_ids = []
    result.rescheduled_step_ids = []

    with patch("app.adaptation_executor.apply_plan_adaptation", return_value=result), \
         patch("app.adaptation_executor.cancel_plan_step_jobs") as mock_cancel, \
         patch("app.adaptation_executor.log_user_event"):

        AdaptationExecutor().execute(db, plan.id, AdaptationIntent.PAUSE_PLAN)

    mock_cancel.assert_not_called()


def test_resume_updates_status_logs_event_returns_step_ids():
    plan = _make_plan("paused")
    db = _make_db(plan)
    result = MagicMock()
    result.canceled_step_ids = []
    result.rescheduled_step_ids = [20, 21, 22]

    with patch("app.adaptation_executor.apply_plan_adaptation", return_value=result) as mock_apply, \
         patch("app.adaptation_executor.log_user_event") as mock_log:

        step_ids = AdaptationExecutor().execute(db, plan.id, AdaptationIntent.RESUME_PLAN)

    assert plan.status == "active"  # не "resumed" — саме "active"
    call_payload = mock_apply.call_args[1]["adaptation_payload"]
    assert call_payload["adaptation_type"] == "resume"
    mock_log.assert_called_once()
    log_kwargs = mock_log.call_args[1]
    assert log_kwargs["event_type"] == "plan_resumed"
    assert step_ids == [20, 21, 22]  # повертає для reschedule після commit


def test_resume_raises_when_not_paused():
    plan = _make_plan("active")
    db = _make_db(plan)

    with patch("app.adaptation_executor.apply_plan_adaptation") as mock_apply:
        with pytest.raises(AdaptationNotEligibleError) as exc_info:
            AdaptationExecutor().execute(db, plan.id, AdaptationIntent.RESUME_PLAN)

    assert exc_info.value.reason == "not_paused"
    mock_apply.assert_not_called()


def test_resume_does_not_call_reschedule_directly():
    """reschedule_plan_steps must NOT be called inside executor — only after commit in orchestrator."""
    plan = _make_plan("paused")
    db = _make_db(plan)
    result = MagicMock()
    result.canceled_step_ids = []
    result.rescheduled_step_ids = [20, 21]

    with patch("app.adaptation_executor.apply_plan_adaptation", return_value=result), \
         patch("app.adaptation_executor.log_user_event"), \
         patch("app.adaptation_executor.reschedule_plan_steps") as mock_reschedule:

        AdaptationExecutor().execute(db, plan.id, AdaptationIntent.RESUME_PLAN)

    mock_reschedule.assert_not_called()

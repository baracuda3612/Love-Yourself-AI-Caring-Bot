from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.adaptation_executor import AdaptationExecutor
from app.adaptation_types import AdaptationIntent, AdaptationNotEligibleError
from app.db import AIPlanStep, AIPlanVersion


def _make_plan(*, slots, load, status="active", days=None, focus="somatic"):
    return SimpleNamespace(
        id=1,
        user_id=42,
        status=status,
        preferred_time_slots=list(slots),
        load=load,
        days=days or [],
        focus=focus,
    )


def _make_plan_db(plan):
    db = MagicMock()
    plan_query = MagicMock()
    plan_query.options.return_value.filter.return_value.first.return_value = plan

    content_query = MagicMock()
    content_query.filter.return_value.order_by.return_value.first.return_value = None

    def _query(model):
        if getattr(model, "__name__", "") == "AIPlan":
            return plan_query
        return content_query

    db.query.side_effect = _query
    return db, content_query


def test_reduce_cancels_correct_slot_steps():
    morning_step = SimpleNamespace(
        id=11,
        time_slot="MORNING",
        is_completed=False,
        skipped=False,
        canceled_by_adaptation=False,
        scheduled_for="x",
    )
    evening_step = SimpleNamespace(
        id=12,
        time_slot="EVENING",
        is_completed=False,
        skipped=False,
        canceled_by_adaptation=False,
        scheduled_for="x",
    )
    plan = _make_plan(slots=["MORNING", "EVENING"], load="MID")
    db, _ = _make_plan_db(plan)

    with patch("app.plan_adaptations._iter_future_steps", return_value=[(None, morning_step), (None, evening_step)]), \
         patch("app.adaptation_executor.cancel_plan_step_jobs"), \
         patch("app.adaptation_executor.log_user_event"):
        AdaptationExecutor().execute(
            db,
            plan.id,
            AdaptationIntent.REDUCE_DAILY_LOAD,
            params={"slot_to_remove": "MORNING"},
        )

    assert morning_step.canceled_by_adaptation is True
    assert morning_step.skipped is False
    assert evening_step.canceled_by_adaptation is False
    assert plan.load == "LITE"
    assert plan.preferred_time_slots == ["EVENING"]


def test_reduce_uses_canceled_not_skipped():
    step = SimpleNamespace(
        id=77,
        time_slot="MORNING",
        is_completed=False,
        skipped=False,
        canceled_by_adaptation=False,
        scheduled_for="x",
    )
    plan = _make_plan(slots=["MORNING", "DAY"], load="MID")
    db, _ = _make_plan_db(plan)

    with patch("app.plan_adaptations._iter_future_steps", return_value=[(None, step)]), \
         patch("app.adaptation_executor.cancel_plan_step_jobs"), \
         patch("app.adaptation_executor.log_user_event"):
        AdaptationExecutor().execute(
            db,
            plan.id,
            AdaptationIntent.REDUCE_DAILY_LOAD,
            params={"slot_to_remove": "MORNING"},
        )

    assert step.skipped is False
    assert step.canceled_by_adaptation is True


def test_reduce_raises_slot_not_in_plan():
    plan = _make_plan(slots=["MORNING", "EVENING"], load="MID")
    db, _ = _make_plan_db(plan)

    with pytest.raises(AdaptationNotEligibleError) as exc:
        AdaptationExecutor().execute(
            db,
            plan.id,
            AdaptationIntent.REDUCE_DAILY_LOAD,
            params={"slot_to_remove": "DAY"},
        )

    assert exc.value.reason == "slot_not_in_plan"


def test_reduce_raises_at_minimum():
    plan = _make_plan(slots=["MORNING"], load="LITE")
    db, _ = _make_plan_db(plan)

    with pytest.raises(AdaptationNotEligibleError) as exc:
        AdaptationExecutor().execute(
            db,
            plan.id,
            AdaptationIntent.REDUCE_DAILY_LOAD,
            params={"slot_to_remove": "MORNING"},
        )

    assert exc.value.reason == "already_at_minimum_load"


def test_increase_lite_to_mid_adds_step_to_each_day():
    day = SimpleNamespace(
        id=9,
        steps=[
            SimpleNamespace(
                exercise_id=1,
                difficulty="EASY",
                is_completed=False,
                skipped=False,
                canceled_by_adaptation=False,
            )
        ],
    )
    plan = _make_plan(slots=["MORNING"], load="LITE", days=[day])
    db, content_query = _make_plan_db(plan)
    content_query.filter.return_value.order_by.return_value.first.return_value = SimpleNamespace(id=2)

    added_steps = []

    def _add(obj):
        if isinstance(obj, AIPlanStep):
            obj.id = 500 + len(added_steps)
            added_steps.append(obj)

    db.add.side_effect = _add

    with patch("app.adaptation_executor.log_user_event"):
        AdaptationExecutor().execute(
            db,
            plan.id,
            AdaptationIntent.INCREASE_DAILY_LOAD,
            params={"slot_to_add": "EVENING"},
        )

    assert len(added_steps) == 1
    assert added_steps[0].time_slot == "EVENING"
    assert plan.load == "MID"
    assert plan.preferred_time_slots == ["MORNING", "EVENING"]


def test_increase_mid_to_intensive_auto_assigns():
    day = SimpleNamespace(
        id=5,
        steps=[
            SimpleNamespace(
                exercise_id=10,
                difficulty="EASY",
                is_completed=False,
                skipped=False,
                canceled_by_adaptation=False,
            )
        ],
    )
    plan = _make_plan(slots=["MORNING", "EVENING"], load="MID", days=[day])
    db, content_query = _make_plan_db(plan)
    content_query.filter.return_value.order_by.return_value.first.return_value = SimpleNamespace(id=11)

    added_steps = []

    def _add(obj):
        if isinstance(obj, AIPlanStep):
            obj.id = 800 + len(added_steps)
            added_steps.append(obj)

    db.add.side_effect = _add

    with patch("app.adaptation_executor.log_user_event"):
        AdaptationExecutor().execute(db, plan.id, AdaptationIntent.INCREASE_DAILY_LOAD, params=None)

    assert added_steps[0].time_slot == "DAY"
    assert plan.load == "INTENSIVE"
    assert plan.preferred_time_slots == ["MORNING", "DAY", "EVENING"]


def test_increase_raises_at_maximum():
    plan = _make_plan(slots=["MORNING", "DAY", "EVENING"], load="INTENSIVE")
    db, _ = _make_plan_db(plan)

    with pytest.raises(AdaptationNotEligibleError) as exc:
        AdaptationExecutor().execute(db, plan.id, AdaptationIntent.INCREASE_DAILY_LOAD, params=None)

    assert exc.value.reason == "already_at_maximum_load"


def test_reduce_creates_plan_version():
    step = SimpleNamespace(
        id=1,
        time_slot="MORNING",
        is_completed=False,
        skipped=False,
        canceled_by_adaptation=False,
        scheduled_for="x",
    )
    plan = _make_plan(slots=["MORNING", "DAY"], load="MID")
    db, _ = _make_plan_db(plan)

    added_versions = []

    def _add(obj):
        if isinstance(obj, AIPlanVersion):
            added_versions.append(obj)

    db.add.side_effect = _add

    with patch("app.plan_adaptations._iter_future_steps", return_value=[(None, step)]), \
         patch("app.adaptation_executor.cancel_plan_step_jobs"), \
         patch("app.adaptation_executor.log_user_event"):
        AdaptationExecutor().execute(
            db,
            plan.id,
            AdaptationIntent.REDUCE_DAILY_LOAD,
            params={"slot_to_remove": "MORNING"},
        )

    assert added_versions
    version = added_versions[0]
    assert version.applied_adaptation_type == "REDUCE_DAILY_LOAD"
    assert version.diff["slot_removed"] == "MORNING"
    assert version.diff["canceled_step_ids"] == [1]
    assert version.diff["new_load"] == "LITE"


def test_increase_creates_plan_version():
    day = SimpleNamespace(
        id=5,
        steps=[
            SimpleNamespace(
                exercise_id=12,
                difficulty="EASY",
                is_completed=False,
                skipped=False,
                canceled_by_adaptation=False,
            )
        ],
    )
    plan = _make_plan(slots=["MORNING"], load="LITE", days=[day])
    db, content_query = _make_plan_db(plan)
    content_query.filter.return_value.order_by.return_value.first.return_value = SimpleNamespace(id=99)

    added_versions = []

    def _add(obj):
        if isinstance(obj, AIPlanStep):
            obj.id = 321
        if isinstance(obj, AIPlanVersion):
            added_versions.append(obj)

    db.add.side_effect = _add

    with patch("app.adaptation_executor.log_user_event"):
        AdaptationExecutor().execute(
            db,
            plan.id,
            AdaptationIntent.INCREASE_DAILY_LOAD,
            params={"slot_to_add": "EVENING"},
        )

    assert added_versions
    version = added_versions[0]
    assert version.applied_adaptation_type == "INCREASE_DAILY_LOAD"
    assert version.diff["slot_added"] == "EVENING"
    assert version.diff["added_step_ids"] == [321]
    assert version.diff["new_load"] == "MID"


def test_increase_returns_added_step_ids_for_reschedule():
    """execute() must return added_ids so orchestrator can reschedule after commit."""
    day = SimpleNamespace(
        id=9,
        steps=[
            SimpleNamespace(
                exercise_id=1,
                difficulty="EASY",
                is_completed=False,
                skipped=False,
                canceled_by_adaptation=False,
            )
        ],
    )
    plan = _make_plan(slots=["MORNING"], load="LITE", days=[day])
    db, content_query = _make_plan_db(plan)
    content_query.filter.return_value.order_by.return_value.first.return_value = SimpleNamespace(id=2)

    added_steps = []

    def _add(obj):
        if isinstance(obj, AIPlanStep):
            obj.id = 999
            added_steps.append(obj)

    db.add.side_effect = _add

    with patch("app.adaptation_executor.log_user_event"):
        result = AdaptationExecutor().execute(
            db,
            plan.id,
            AdaptationIntent.INCREASE_DAILY_LOAD,
            params={"slot_to_add": "EVENING"},
        )

    assert result == [999]  # повернуто для post-commit reschedule

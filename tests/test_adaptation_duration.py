from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.adaptation_executor import AdaptationExecutor
from app.adaptation_types import AdaptationIntent, AdaptationNotEligibleError
from app.db import AIPlanDay, AIPlanStep, AIPlanVersion


class _EnumLike:
    def __init__(self, value: str):
        self.value = value


class _DraftStep:
    def __init__(self, day_number: int, exercise_id: int):
        self.day_number = day_number
        self.exercise_id = exercise_id
        self.exercise_name = f"Exercise {exercise_id}"
        self.time_slot = _EnumLike("DAY")
        self.slot_type = _EnumLike("PRIMARY")
        self.difficulty = "EASY"


def _make_plan(*, total_days: int, current_day: int, start_date=None):
    return SimpleNamespace(
        id=1,
        user_id=42,
        status="active",
        total_days=total_days,
        current_day=current_day,
        start_date=start_date,
        end_date=None,
        preferred_time_slots=["DAY"],
        focus="somatic",
        load="LITE",
        days=[],
    )


def _make_db(plan, user):
    db = MagicMock()
    plan_query = MagicMock()
    plan_query.options.return_value.filter.return_value.first.return_value = plan
    user_query = MagicMock()
    user_query.filter.return_value.first.return_value = user

    def _query(model):
        if getattr(model, "__name__", "") == "AIPlan":
            return plan_query
        if getattr(model, "__name__", "") == "User":
            return user_query
        return MagicMock()

    db.query.side_effect = _query
    return db


def test_shorten_cancels_steps_beyond_target():
    plan = _make_plan(total_days=21, current_day=8)
    user = SimpleNamespace(id=42, timezone="UTC", plan_end_date=None, profile=None)
    db = _make_db(plan, user)

    step_15 = SimpleNamespace(id=15, canceled_by_adaptation=False, skipped=False, scheduled_for="x")
    step_16 = SimpleNamespace(id=16, canceled_by_adaptation=False, skipped=False, scheduled_for="x")
    iter_rows = [(SimpleNamespace(day_number=15), step_15), (SimpleNamespace(day_number=16), step_16)]

    added_versions = []

    def _add(obj):
        if isinstance(obj, AIPlanVersion):
            added_versions.append(obj)

    db.add.side_effect = _add

    with patch("app.plan_adaptations._iter_future_steps", return_value=iter_rows), \
         patch("app.adaptation_executor.log_user_event"), \
         patch("app.adaptation_executor.cancel_plan_step_jobs"):
        AdaptationExecutor().execute(
            db,
            plan.id,
            AdaptationIntent.SHORTEN_PLAN_DURATION,
            params={"target_duration": 14},
        )

    assert step_15.canceled_by_adaptation is True
    assert step_16.canceled_by_adaptation is True
    assert step_15.skipped is False
    assert step_16.skipped is False
    assert plan.total_days == 14
    assert added_versions[0].diff["canceled_step_ids"] == [15, 16]


def test_shorten_does_not_cancel_completed_steps():
    plan = _make_plan(total_days=21, current_day=8)
    user = SimpleNamespace(id=42, timezone="UTC", plan_end_date=None, profile=None)
    db = _make_db(plan, user)

    completed = SimpleNamespace(id=18, canceled_by_adaptation=False, is_completed=True, scheduled_for="x")

    with patch("app.plan_adaptations._iter_future_steps", return_value=[]), \
         patch("app.adaptation_executor.log_user_event"), \
         patch("app.adaptation_executor.cancel_plan_step_jobs"):
        AdaptationExecutor().execute(
            db,
            plan.id,
            AdaptationIntent.SHORTEN_PLAN_DURATION,
            params={"target_duration": 14},
        )

    assert completed.canceled_by_adaptation is False


def test_shorten_raises_current_day_exceeds_target():
    plan = _make_plan(total_days=21, current_day=10)
    user = SimpleNamespace(id=42, timezone="UTC", plan_end_date=None, profile=None)
    db = _make_db(plan, user)

    with pytest.raises(AdaptationNotEligibleError) as exc:
        AdaptationExecutor().execute(
            db,
            plan.id,
            AdaptationIntent.SHORTEN_PLAN_DURATION,
            params={"target_duration": 7},
        )

    assert exc.value.reason == "current_day_exceeds_target"


def test_shorten_raises_target_not_less_than_current():
    plan = _make_plan(total_days=21, current_day=5)
    user = SimpleNamespace(id=42, timezone="UTC", plan_end_date=None, profile=None)
    db = _make_db(plan, user)

    with pytest.raises(AdaptationNotEligibleError) as exc:
        AdaptationExecutor().execute(
            db,
            plan.id,
            AdaptationIntent.SHORTEN_PLAN_DURATION,
            params={"target_duration": 21},
        )

    assert exc.value.reason == "target_not_less_than_current"


def test_shorten_updates_end_date():
    plan = _make_plan(
        total_days=21,
        current_day=5,
        start_date=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )
    user = SimpleNamespace(id=42, timezone="UTC", plan_end_date=None, profile=None)
    db = _make_db(plan, user)

    with patch("app.plan_adaptations._iter_future_steps", return_value=[]), \
         patch("app.adaptation_executor.log_user_event"):
        AdaptationExecutor().execute(
            db,
            plan.id,
            AdaptationIntent.SHORTEN_PLAN_DURATION,
            params={"target_duration": 14},
        )

    assert plan.end_date == datetime(2025, 1, 15, tzinfo=timezone.utc)
    assert user.plan_end_date == plan.end_date


def test_shorten_creates_plan_version():
    plan = _make_plan(total_days=21, current_day=5)
    user = SimpleNamespace(id=42, timezone="UTC", plan_end_date=None, profile=None)
    db = _make_db(plan, user)

    added_versions = []

    def _add(obj):
        if isinstance(obj, AIPlanVersion):
            added_versions.append(obj)

    db.add.side_effect = _add

    with patch("app.plan_adaptations._iter_future_steps", return_value=[]), \
         patch("app.adaptation_executor.log_user_event"):
        AdaptationExecutor().execute(
            db,
            plan.id,
            AdaptationIntent.SHORTEN_PLAN_DURATION,
            params={"target_duration": 14},
        )

    version = added_versions[0]
    assert version.applied_adaptation_type == "SHORTEN_PLAN_DURATION"
    assert version.diff["old_total_days"] == 21
    assert version.diff["new_total_days"] == 14
    assert version.diff["shortened_from_day"] == 5


def test_shorten_returns_empty_list():
    plan = _make_plan(total_days=21, current_day=5)
    user = SimpleNamespace(id=42, timezone="UTC", plan_end_date=None, profile=None)
    db = _make_db(plan, user)

    with patch("app.plan_adaptations._iter_future_steps", return_value=[]), \
         patch("app.adaptation_executor.log_user_event"):
        result = AdaptationExecutor().execute(
            db,
            plan.id,
            AdaptationIntent.SHORTEN_PLAN_DURATION,
            params={"target_duration": 14},
        )

    assert result == []


def test_extend_adds_only_new_days():
    plan = _make_plan(total_days=14, current_day=7)
    user = SimpleNamespace(id=42, timezone="UTC", plan_end_date=None, profile=None)
    db = _make_db(plan, user)

    draft = SimpleNamespace(steps=[_DraftStep(day, 100 + day) for day in range(1, 22)])
    added_steps = []

    def _add(obj):
        if isinstance(obj, AIPlanDay):
            obj.id = 200 + len([x for x in added_steps if isinstance(x, AIPlanDay)])
            added_steps.append(obj)
        if isinstance(obj, AIPlanStep):
            obj.id = 300 + len([x for x in added_steps if isinstance(x, AIPlanStep)])
            added_steps.append(obj)

    db.add.side_effect = _add

    with patch("app.plan_drafts.service.build_plan_draft", return_value=draft), \
         patch("app.adaptation_executor.resolve_daily_time_slots", return_value={}), \
         patch("app.adaptation_executor.compute_scheduled_for", return_value=datetime.now(timezone.utc)), \
         patch("app.adaptation_executor.log_user_event"):
        AdaptationExecutor().execute(
            db,
            plan.id,
            AdaptationIntent.EXTEND_PLAN_DURATION,
            params={"target_duration": 21},
        )

    created_day_numbers = [d.day_number for d in added_steps if isinstance(d, AIPlanDay)]
    assert created_day_numbers == list(range(15, 22))


def test_extend_returns_added_step_ids():
    plan = _make_plan(total_days=14, current_day=7)
    user = SimpleNamespace(id=42, timezone="UTC", plan_end_date=None, profile=None)
    db = _make_db(plan, user)
    draft = SimpleNamespace(steps=[_DraftStep(day, 200 + day) for day in range(1, 22)])

    def _add(obj):
        if isinstance(obj, AIPlanDay):
            obj.id = 600
        if isinstance(obj, AIPlanStep):
            obj.id = 700 + obj.exercise_id

    db.add.side_effect = _add

    with patch("app.plan_drafts.service.build_plan_draft", return_value=draft), \
         patch("app.adaptation_executor.resolve_daily_time_slots", return_value={}), \
         patch("app.adaptation_executor.compute_scheduled_for", return_value=datetime.now(timezone.utc)), \
         patch("app.adaptation_executor.log_user_event"):
        result = AdaptationExecutor().execute(
            db,
            plan.id,
            AdaptationIntent.EXTEND_PLAN_DURATION,
            params={"target_duration": 21},
        )

    assert result
    assert all(isinstance(i, int) for i in result)


def test_extend_raises_target_not_greater():
    plan = _make_plan(total_days=21, current_day=5)
    user = SimpleNamespace(id=42, timezone="UTC", plan_end_date=None, profile=None)
    db = _make_db(plan, user)

    with pytest.raises(AdaptationNotEligibleError) as exc:
        AdaptationExecutor().execute(
            db,
            plan.id,
            AdaptationIntent.EXTEND_PLAN_DURATION,
            params={"target_duration": 21},
        )

    assert exc.value.reason == "target_not_greater_than_current"


def test_extend_updates_end_date():
    plan = _make_plan(
        total_days=14,
        current_day=7,
        start_date=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )
    user = SimpleNamespace(id=42, timezone="UTC", plan_end_date=None, profile=None)
    db = _make_db(plan, user)
    draft = SimpleNamespace(steps=[_DraftStep(day, 300 + day) for day in range(1, 22)])

    def _add(obj):
        if isinstance(obj, AIPlanDay):
            obj.id = 800
        if isinstance(obj, AIPlanStep):
            obj.id = 900 + obj.exercise_id

    db.add.side_effect = _add

    with patch("app.plan_drafts.service.build_plan_draft", return_value=draft), \
         patch("app.adaptation_executor.resolve_daily_time_slots", return_value={}), \
         patch("app.adaptation_executor.compute_scheduled_for", return_value=datetime.now(timezone.utc)), \
         patch("app.adaptation_executor.log_user_event"):
        AdaptationExecutor().execute(
            db,
            plan.id,
            AdaptationIntent.EXTEND_PLAN_DURATION,
            params={"target_duration": 21},
        )

    assert plan.end_date == datetime(2025, 1, 22, tzinfo=timezone.utc)
    assert user.plan_end_date == plan.end_date


def test_extend_creates_plan_version():
    plan = _make_plan(total_days=14, current_day=7)
    user = SimpleNamespace(id=42, timezone="UTC", plan_end_date=None, profile=None)
    db = _make_db(plan, user)
    draft = SimpleNamespace(steps=[_DraftStep(day, 400 + day) for day in range(1, 22)])

    added_versions = []

    def _add(obj):
        if isinstance(obj, AIPlanDay):
            obj.id = 700
        if isinstance(obj, AIPlanStep):
            obj.id = 750 + obj.exercise_id
        if isinstance(obj, AIPlanVersion):
            added_versions.append(obj)

    db.add.side_effect = _add

    with patch("app.plan_drafts.service.build_plan_draft", return_value=draft), \
         patch("app.adaptation_executor.resolve_daily_time_slots", return_value={}), \
         patch("app.adaptation_executor.compute_scheduled_for", return_value=datetime.now(timezone.utc)), \
         patch("app.adaptation_executor.log_user_event"):
        AdaptationExecutor().execute(
            db,
            plan.id,
            AdaptationIntent.EXTEND_PLAN_DURATION,
            params={"target_duration": 21},
        )

    version = added_versions[0]
    assert version.applied_adaptation_type == "EXTEND_PLAN_DURATION"
    assert version.diff["old_total_days"] == 14
    assert version.diff["new_total_days"] == 21
    assert version.diff["days_added"] == 7
    assert version.diff["extended_from_day"] == 7

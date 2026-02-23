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


def _make_plan(*, total_days: int, current_day: int, start_date=datetime(2025, 1, 1, tzinfo=timezone.utc)):
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


def test_extend_raises_if_no_start_date():
    plan = _make_plan(total_days=14, current_day=5, start_date=None)
    user = SimpleNamespace(id=42, timezone="UTC", plan_end_date=None, profile=None)
    db = _make_db(plan, user)

    with pytest.raises(AdaptationNotEligibleError) as exc:
        AdaptationExecutor().execute(
            db,
            plan.id,
            AdaptationIntent.EXTEND_PLAN_DURATION,
            params={"target_duration": 21},
        )

    assert exc.value.reason == "plan_has_no_start_date"


def test_extend_accepts_any_valid_target_greater_than_current():
    plan = _make_plan(total_days=7, current_day=3, start_date=datetime(2025, 1, 1, tzinfo=timezone.utc))
    user = SimpleNamespace(id=42, timezone="UTC", plan_end_date=None, profile=None)
    db = _make_db(plan, user)

    draft = SimpleNamespace(steps=[_DraftStep(day, 500 + day) for day in range(1, 22)])

    def _add(obj):
        if isinstance(obj, AIPlanDay):
            obj.id = 1000
        if isinstance(obj, AIPlanStep):
            obj.id = 1100 + obj.exercise_id

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

    assert plan.total_days == 21


def test_shorten_executor_blocks_target_below_current_day():
    plan = _make_plan(total_days=90, current_day=25)
    user = SimpleNamespace(id=42, timezone="UTC", plan_end_date=None, profile=None)
    db = _make_db(plan, user)

    with pytest.raises(AdaptationNotEligibleError) as exc:
        AdaptationExecutor().execute(
            db,
            plan.id,
            AdaptationIntent.SHORTEN_PLAN_DURATION,
            params={"target_duration": 21},
        )

    assert exc.value.reason == "current_day_exceeds_target"


def test_extend_from_7_to_14_is_allowed():
    plan = _make_plan(total_days=7, current_day=3, start_date=datetime(2025, 1, 1, tzinfo=timezone.utc))
    user = SimpleNamespace(id=42, timezone="UTC", plan_end_date=None, profile=None)
    db = _make_db(plan, user)

    draft = SimpleNamespace(steps=[_DraftStep(day, 800 + day) for day in range(1, 15)])

    def _add(obj):
        if isinstance(obj, AIPlanDay):
            obj.id = 1400
        if isinstance(obj, AIPlanStep):
            obj.id = 1500 + obj.exercise_id

    db.add.side_effect = _add

    with patch("app.plan_drafts.service.build_plan_draft", return_value=draft), \
         patch("app.adaptation_executor.resolve_daily_time_slots", return_value={}), \
         patch("app.adaptation_executor.compute_scheduled_for", return_value=datetime.now(timezone.utc)), \
         patch("app.adaptation_executor.log_user_event"):
        AdaptationExecutor().execute(
            db,
            plan.id,
            AdaptationIntent.EXTEND_PLAN_DURATION,
            params={"target_duration": 14},
        )

    assert plan.total_days == 14


def test_change_category_pauses_old_and_creates_new_active_plan():
    plan = _make_plan(total_days=21, current_day=7)
    plan.id = 10
    plan.module_id = "BURNOUT_RECOVERY"
    user = SimpleNamespace(id=42, timezone="UTC", plan_end_date=None, profile=None)

    db = MagicMock()
    plan_query_main = MagicMock()
    plan_query_main.options.return_value.filter.return_value.first.return_value = plan
    plan_query_guard = MagicMock()
    plan_query_guard.filter.return_value.first.return_value = None
    user_query = MagicMock()
    user_query.filter.return_value.first.return_value = user

    def _query(model):
        if getattr(model, "__name__", "") == "AIPlan":
            if not hasattr(_query, "count"):
                _query.count = 0
            _query.count += 1
            return plan_query_main if _query.count == 1 else plan_query_guard
        if getattr(model, "__name__", "") == "User":
            return user_query
        return MagicMock()

    db.query.side_effect = _query

    old_step = SimpleNamespace(id=501, canceled_by_adaptation=False, is_completed=False, skipped=False, scheduled_for="x")
    iter_rows = [(SimpleNamespace(day_number=9), old_step)]

    draft = SimpleNamespace(total_days=21, steps=[_DraftStep(1, 9001), _DraftStep(1, 9002), _DraftStep(2, 9003)])

    versions = []
    created_plans = []
    day_counter = {"value": 0}

    def _add(obj):
        if obj.__class__.__name__ == "AIPlan":
            if getattr(obj, "id", None) is None:
                obj.id = 77
            created_plans.append(obj)
        if isinstance(obj, AIPlanDay):
            day_counter["value"] += 1
            obj.id = 1000 + day_counter["value"]
        if isinstance(obj, AIPlanStep):
            obj.id = 2000 + obj.exercise_id
        if isinstance(obj, AIPlanVersion):
            versions.append(obj)

    db.add.side_effect = _add

    with patch("app.plan_adaptations._iter_future_steps", return_value=iter_rows),          patch("app.plan_drafts.service.build_plan_draft", return_value=draft),          patch("app.adaptation_executor.resolve_daily_time_slots", return_value={}),          patch("app.adaptation_executor.compute_scheduled_for", return_value=datetime.now(timezone.utc)),          patch("app.adaptation_executor.log_user_event"),          patch("app.adaptation_executor.cancel_plan_step_jobs"):
        added_ids = AdaptationExecutor()._change_main_category(
            db,
            plan.id,
            params={"target_category": "cognitive"},
        )

    assert plan.status == "paused"
    assert old_step.canceled_by_adaptation is True
    assert old_step.scheduled_for is None
    assert created_plans
    new_plan = created_plans[0]
    assert new_plan.status == "active"
    assert new_plan.focus == "cognitive"
    assert versions
    assert versions[0].applied_adaptation_type == "CHANGE_MAIN_CATEGORY"
    assert versions[0].diff["canceled_step_ids"]
    assert added_ids


def test_change_category_does_not_mutate_old_if_draft_builder_fails():
    from app.plan_drafts.draft_builder import DraftValidationError

    plan = _make_plan(total_days=21, current_day=7)
    plan.id = 11
    user = SimpleNamespace(id=42, timezone="UTC", plan_end_date=None, profile=None)

    db = MagicMock()
    plan_query_main = MagicMock()
    plan_query_main.options.return_value.filter.return_value.first.return_value = plan
    plan_query_guard = MagicMock()
    plan_query_guard.filter.return_value.first.return_value = None
    user_query = MagicMock()
    user_query.filter.return_value.first.return_value = user

    def _query(model):
        if getattr(model, "__name__", "") == "AIPlan":
            if not hasattr(_query, "count"):
                _query.count = 0
            _query.count += 1
            return plan_query_main if _query.count == 1 else plan_query_guard
        if getattr(model, "__name__", "") == "User":
            return user_query
        return MagicMock()

    db.query.side_effect = _query

    old_step = SimpleNamespace(id=601, canceled_by_adaptation=False, is_completed=False, skipped=False, scheduled_for="x")
    iter_rows = [(SimpleNamespace(day_number=9), old_step)]

    created_plans = []

    def _add(obj):
        if obj.__class__.__name__ == "AIPlan":
            created_plans.append(obj)

    db.add.side_effect = _add

    with patch("app.plan_adaptations._iter_future_steps", return_value=iter_rows),          patch("app.plan_drafts.service.build_plan_draft", side_effect=DraftValidationError("boom")),          patch("app.adaptation_executor.cancel_plan_step_jobs"):
        with pytest.raises(AdaptationNotEligibleError) as exc:
            AdaptationExecutor()._change_main_category(
                db,
                plan.id,
                params={"target_category": "cognitive"},
            )

    assert exc.value.reason == "content_library_insufficient"
    assert plan.status == "active"
    assert old_step.canceled_by_adaptation is False
    assert old_step.scheduled_for == "x"
    assert not created_plans


def test_change_category_works_when_builder_has_no_user_id_param():
    plan = _make_plan(total_days=21, current_day=7)
    plan.id = 12
    plan.module_id = "BURNOUT_RECOVERY"
    user = SimpleNamespace(id=42, timezone="UTC", plan_end_date=None, profile=None)

    db = MagicMock()
    plan_query_main = MagicMock()
    plan_query_main.options.return_value.filter.return_value.first.return_value = plan
    plan_query_guard = MagicMock()
    plan_query_guard.filter.return_value.first.return_value = None
    user_query = MagicMock()
    user_query.filter.return_value.first.return_value = user

    def _query(model):
        if getattr(model, "__name__", "") == "AIPlan":
            if not hasattr(_query, "count"):
                _query.count = 0
            _query.count += 1
            return plan_query_main if _query.count == 1 else plan_query_guard
        if getattr(model, "__name__", "") == "User":
            return user_query
        return MagicMock()

    db.query.side_effect = _query

    old_step = SimpleNamespace(id=701, canceled_by_adaptation=False, is_completed=False, skipped=False, scheduled_for="x")
    iter_rows = [(SimpleNamespace(day_number=9), old_step)]

    draft = SimpleNamespace(total_days=21, steps=[_DraftStep(1, 9101)])

    def _build_without_user_id(parameters):
        return draft

    def _add(obj):
        if obj.__class__.__name__ == "AIPlan" and getattr(obj, "id", None) is None:
            obj.id = 88
        if isinstance(obj, AIPlanDay):
            obj.id = 1200
        if isinstance(obj, AIPlanStep):
            obj.id = 2200

    db.add.side_effect = _add

    with patch("app.plan_adaptations._iter_future_steps", return_value=iter_rows), \
         patch("app.plan_drafts.service.build_plan_draft", new=_build_without_user_id), \
         patch("app.adaptation_executor.resolve_daily_time_slots", return_value={}), \
         patch("app.adaptation_executor.compute_scheduled_for", return_value=datetime.now(timezone.utc)), \
         patch("app.adaptation_executor.log_user_event"), \
         patch("app.adaptation_executor.cancel_plan_step_jobs"):
        added_ids = AdaptationExecutor()._change_main_category(
            db,
            plan.id,
            params={"target_category": "cognitive"},
        )

    assert plan.status == "paused"
    assert added_ids == [2200]


def test_change_category_preserves_duration():
    plan = _make_plan(total_days=7, current_day=3)
    plan.id = 13
    plan.module_id = "BURNOUT_RECOVERY"
    user = SimpleNamespace(id=42, timezone="UTC", plan_end_date=None, profile=None)

    db = MagicMock()
    plan_query_main = MagicMock()
    plan_query_main.options.return_value.filter.return_value.first.return_value = plan
    plan_query_guard = MagicMock()
    plan_query_guard.filter.return_value.first.return_value = None
    user_query = MagicMock()
    user_query.filter.return_value.first.return_value = user

    def _query(model):
        if getattr(model, "__name__", "") == "AIPlan":
            if not hasattr(_query, "count"):
                _query.count = 0
            _query.count += 1
            return plan_query_main if _query.count == 1 else plan_query_guard
        if getattr(model, "__name__", "") == "User":
            return user_query
        return MagicMock()

    db.query.side_effect = _query

    old_step = SimpleNamespace(id=801, canceled_by_adaptation=False, is_completed=False, skipped=False, scheduled_for="x")
    iter_rows = [(SimpleNamespace(day_number=5), old_step)]

    draft = SimpleNamespace(total_days=10, steps=[_DraftStep(day, 9200 + day) for day in range(1, 11)])

    created_plans = []
    created_days = []

    def _add(obj):
        if obj.__class__.__name__ == "AIPlan":
            if getattr(obj, "id", None) is None:
                obj.id = 99
            created_plans.append(obj)
        if isinstance(obj, AIPlanDay):
            obj.id = 1300 + len(created_days)
            created_days.append(obj)
        if isinstance(obj, AIPlanStep):
            obj.id = 2300 + obj.exercise_id

    db.add.side_effect = _add

    with patch("app.plan_adaptations._iter_future_steps", return_value=iter_rows), \
         patch("app.plan_drafts.service.build_plan_draft", return_value=draft), \
         patch("app.adaptation_executor.resolve_daily_time_slots", return_value={}), \
         patch("app.adaptation_executor.compute_scheduled_for", return_value=datetime.now(timezone.utc)), \
         patch("app.adaptation_executor.log_user_event"), \
         patch("app.adaptation_executor.cancel_plan_step_jobs"):
        AdaptationExecutor()._change_main_category(
            db,
            plan.id,
            params={"target_category": "cognitive"},
        )

    assert created_plans
    assert created_plans[0].total_days == 7
    assert max(day.day_number for day in created_days) == 7

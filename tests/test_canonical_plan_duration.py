from types import SimpleNamespace

import pytest

from app.adaptation_executor import AdaptationExecutor
from app.adaptation_types import AdaptationNotEligibleError
from app.plan_drafts.service import DraftValidationError, build_plan_draft
from app.plan_duration import InvalidDurationError, assert_canonical_total_days


class _QueryStub:
    def __init__(self, result):
        self._result = result

    def options(self, *args, **kwargs):
        return self

    def filter(self, *args, **kwargs):
        return self

    def first(self):
        return self._result


class _DBStub:
    def __init__(self, results):
        self._results = list(results)

    def query(self, *args, **kwargs):
        return _QueryStub(self._results.pop(0))



def test_no_10_day_plan_possible():
    with pytest.raises(DraftValidationError):
        build_plan_draft(
            {
                "duration": 10,
                "focus": "somatic",
                "load": "LITE",
                "preferred_time_slots": ["MORNING"],
            },
            user_id="1",
        )

    with pytest.raises(InvalidDurationError):
        assert_canonical_total_days(10)

    plan = SimpleNamespace(
        id=1,
        user_id=42,
        status="active",
        focus="somatic",
        preferred_time_slots=["MORNING"],
        load="LITE",
        total_days=10,
    )
    user = SimpleNamespace(id=42, profile={}, timezone="UTC")
    db = _DBStub([plan, user, None])

    with pytest.raises(AdaptationNotEligibleError, match="invalid_plan_duration"):
        AdaptationExecutor()._change_main_category(db, 1, {"target_category": "rest"})

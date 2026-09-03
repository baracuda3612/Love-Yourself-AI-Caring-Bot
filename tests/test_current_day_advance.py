from __future__ import annotations

from types import SimpleNamespace

from app import lifecycle
from app.ux.task_notification import maybe_advance_current_day


class _Query:
    def __init__(self, value):
        self.value = value

    def filter(self, *_args, **_kwargs):
        return self

    def first(self):
        return self.value


class _DB:
    def __init__(self, plan):
        self.plan = plan

    def query(self, _model):
        return _Query(self.plan)


def test_reports_progress_past_day_without_writing_legacy_mirror(monkeypatch):
    plan = SimpleNamespace(id=1, current_day=None, total_days=14)
    monkeypatch.setattr(lifecycle, "derive_current_day", lambda *_args: 4)

    assert maybe_advance_current_day(_DB(plan), 1, 3) is True
    assert plan.current_day is None


def test_reports_no_progress_when_derived_day_has_not_passed(monkeypatch):
    plan = SimpleNamespace(id=1, current_day=12, total_days=14)
    monkeypatch.setattr(lifecycle, "derive_current_day", lambda *_args: 3)

    assert maybe_advance_current_day(_DB(plan), 1, 3) is False
    # A stale mirror is ignored and remains untouched.
    assert plan.current_day == 12


def test_missing_plan_is_noop(monkeypatch):
    monkeypatch.setattr(
        lifecycle,
        "derive_current_day",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not derive")),
    )
    assert maybe_advance_current_day(_DB(None), 1, 3) is False


def test_plan_without_duration_is_noop(monkeypatch):
    plan = SimpleNamespace(id=1, current_day=None, total_days=None)
    monkeypatch.setattr(
        lifecycle,
        "derive_current_day",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not derive")),
    )
    assert maybe_advance_current_day(_DB(plan), 1, 3) is False

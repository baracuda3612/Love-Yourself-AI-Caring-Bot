import ast
from datetime import datetime, timezone
from pathlib import Path
import re

import pytest

from app.lifecycle import (
    CurrentMode,
    LifecycleInvariantError,
    derive_current_mode_from_facts,
)
from app.ai import extract_tool_call


@pytest.mark.parametrize(
    ("stage", "completed_at", "plan_status", "expected"),
    [
        ("START", None, None, CurrentMode.ONBOARDING),
        ("GOALS", None, None, CurrentMode.ONBOARDING),
        (None, None, None, CurrentMode.ONBOARDING),
        ("COMPLETED", None, None, CurrentMode.NO_ACTIVE_PLAN),
        ("GOALS", datetime(2026, 9, 2, tzinfo=timezone.utc), None, CurrentMode.NO_ACTIVE_PLAN),
        ("START", None, "active", CurrentMode.ACTIVE),
        ("COMPLETED", None, "active", CurrentMode.ACTIVE),
        ("COMPLETED", None, "paused", CurrentMode.ACTIVE_PAUSED),
    ],
)
def test_current_mode_matrix(stage, completed_at, plan_status, expected):
    assert (
        derive_current_mode_from_facts(
            onboarding_stage=stage,
            onboarding_completed_at=completed_at,
            current_plan_status=plan_status,
        )
        is expected
    )


def test_current_mode_rejects_terminal_plan_as_current():
    with pytest.raises(LifecycleInvariantError, match="non-current status"):
        derive_current_mode_from_facts(
            onboarding_stage="COMPLETED",
            onboarding_completed_at=None,
            current_plan_status="completed",
        )


def test_responses_function_call_preserves_stable_source_id():
    item = type(
        "FunctionCall",
        (),
        {
            "type": "function_call",
            "name": "pause_plan",
            "arguments": "{}",
            "call_id": "call_stable_123",
            "id": "item_fallback",
        },
    )()
    response = type("Response", (), {"output": [item]})()

    assert extract_tool_call(response) == {
        "name": "pause_plan",
        "arguments": {},
        "call_id": "call_stable_123",
    }


def test_runtime_has_no_legacy_lifecycle_assignments():
    forbidden = ("current_state", "plan_end_date", "is_paused")
    for path in Path("app").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        for attribute in forbidden:
            assert re.search(rf"\.{attribute}\s*=(?!=)", source) is None, (
                f"legacy lifecycle write in {path}: {attribute}"
            )


def test_runtime_has_no_legacy_lifecycle_attribute_reads():
    legacy_attributes = {
        "current_state",
        "plan_end_date",
        "is_paused",
        "current_mode",
        "current_day",
        "is_completed",
        "skipped",
    }
    for path in Path("app").rglob("*.py"):
        if path == Path("app/db.py"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        violations = [
            node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and node.attr in legacy_attributes
        ]
        assert violations == [], f"legacy lifecycle read in {path}: {violations}"


def test_authoritative_status_assignments_are_confined_to_lifecycle_boundary():
    assignment = re.compile(r"\b(?:plan|step)\.(?:status|step_status)\s*=(?!=)")
    for path in Path("app").rglob("*.py"):
        if path == Path("app/lifecycle.py"):
            continue
        assert assignment.search(path.read_text(encoding="utf-8")) is None, (
            f"direct lifecycle status write outside app/lifecycle.py: {path}"
        )


def test_migration_contains_required_authority_guards():
    source = Path(
        "migrations/alembic/versions/20260902_plan_centric_lifecycle.py"
    ).read_text(encoding="utf-8")

    required = {
        "ux_ai_plans_one_current_per_user",
        "uq_ai_plans_user_cycle",
        "uq_ai_plan_days_plan_day",
        "uq_ai_plan_steps_day_order",
        "ck_ai_plans_chronology",
        "ck_ai_plan_steps_terminal_timestamp",
        "uq_plan_lifecycle_operation_source",
        "ct_ai_plans_aggregate_exists",
        "multiple current plans require evidence-based remediation",
        "SET LOCAL TIME ZONE 'UTC'",
        "apscheduler_jobs",
    }
    for marker in required:
        assert marker in source


def test_compatibility_manifest_names_every_legacy_authority_and_owner():
    source = Path(
        "docs/implementation/wp_01_3_lifecycle_compatibility_manifest.md"
    ).read_text(encoding="utf-8")

    for marker in (
        "users.current_state",
        "user_profiles.is_paused",
        "users.plan_end_date",
        "ai_plans.end_date",
        "ai_plans.current_mode",
        "ai_plans.current_day",
        "ai_plan_steps.is_completed",
        "generated_plan_object",
        "SCHEDULE_ADJUSTMENT",
        "apscheduler_jobs",
        "WP-02.1",
        "WP-08.1",
    ):
        assert marker in source

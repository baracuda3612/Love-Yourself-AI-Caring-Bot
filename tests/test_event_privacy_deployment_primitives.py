from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import BigInteger, Integer

from app import db as database
from app.telemetry import EventValidationError, _canonical_dimensions, _validate_properties


MIGRATION_PATH = Path(
    "migrations/alembic/versions/20260905_event_privacy_deployment.py"
)


def _migration_module():
    spec = importlib.util.spec_from_file_location("wp_01_4_migration", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_runtime_requires_the_event_privacy_schema_head() -> None:
    assert database.EXPECTED_ALEMBIC_REVISION == "20260905_event_privacy"
    source = MIGRATION_PATH.read_text(encoding="utf-8")
    assert 'down_revision = "20260902_plan_lifecycle"' in source
    assert "apscheduler_jobs" not in source


def test_catalogue_separates_behavior_from_operational_events() -> None:
    catalogue = {
        name: kind for name, kind, _schema in _migration_module().EVENT_CATALOGUE
    }

    assert catalogue["user_message"] == "user_behavior"
    assert catalogue["task_completed"] == "user_behavior"
    assert catalogue["task_skipped"] == "user_behavior"
    assert catalogue["task_delivered"] == "operational"
    assert catalogue["task_delivery_failed"] == "operational"
    assert catalogue["task_ignored"] == "operational"
    assert catalogue["plan_completed"] == "operational"


def test_event_identity_columns_are_explicit_and_legacy_columns_are_nullable() -> None:
    columns = database.UserEvent.__table__.c

    assert isinstance(columns.plan_id.type, Integer)
    assert isinstance(columns.plan_step_id.type, Integer)
    assert columns.exercise_id.type.python_type is str
    assert isinstance(columns.deployment_id.type, BigInteger)
    assert columns.content_version.nullable is True
    assert columns.environment.nullable is True
    for legacy_name in (
        "event_type",
        "timestamp",
        "plan_execution_id",
        "step_id",
        "context",
    ):
        assert columns[legacy_name].nullable is True


def test_on_demand_authority_is_not_created_early() -> None:
    assert "on_demand_exercise_requests" not in database.Base.metadata.tables
    assert "on_demand_request_id" not in database.UserEvent.__table__.c
    assert "on_demand_request_id" not in database.FeedbackEvent.__table__.c
    source = MIGRATION_PATH.read_text(encoding="utf-8")
    assert 'op.create_table(\n        "on_demand_exercise_requests"' not in source


def test_properties_are_allow_listed_bounded_and_free_text_safe() -> None:
    assert _validate_properties(
        {"message_length": 12}, {"message_length": "integer"}
    ) == {"message_length": 12}

    with pytest.raises(EventValidationError, match="not allow-listed"):
        _validate_properties(
            {"message_length": 12, "username": "someone"},
            {"message_length": "integer"},
        )
    with pytest.raises(EventValidationError, match="wrong catalogue type"):
        _validate_properties(
            {"message_length": "twelve"}, {"message_length": "integer"}
        )


def test_aggregate_dimension_identity_is_stable_and_order_independent() -> None:
    first, first_key = _canonical_dimensions(
        {"event_name": "task_completed", "environment": "testnet"}
    )
    second, second_key = _canonical_dimensions(
        {"environment": "testnet", "event_name": "task_completed"}
    )

    assert first == second
    assert first_key == second_key
    assert len(first_key) == 64

    with pytest.raises(EventValidationError, match="not allow-listed"):
        _canonical_dimensions({"username": "personal-identity"})
    with pytest.raises(EventValidationError, match="coarse scalar"):
        _canonical_dimensions({"event_name": ["task_completed"]})


def test_event_ownership_uses_composite_foreign_keys() -> None:
    foreign_key_columns = {
        tuple(element.parent.name for element in constraint.elements)
        for constraint in database.UserEvent.__table__.foreign_key_constraints
    }

    assert ("deployment_id", "organization_id") in foreign_key_columns
    assert (
        "deployment_enrollment_id",
        "user_id",
        "deployment_id",
    ) in foreign_key_columns
    assert ("plan_id", "user_id") in foreign_key_columns
    assert ("exercise_id", "content_version") in foreign_key_columns


def test_database_guards_immutable_facts_and_pinned_notice() -> None:
    source = MIGRATION_PATH.read_text(encoding="utf-8")

    assert "canonical user events are immutable" in source
    assert "event catalogue definitions are immutable" in source
    assert "privacy notice version content is immutable" in source
    assert "acknowledged notice is not pinned to deployment" in source
    assert "aggregate records are immutable" in source


def test_every_live_event_write_supplies_stable_operation_and_source() -> None:
    event_writer_files = (
        Path("app/telegram.py"),
        Path("app/scheduler.py"),
        Path("app/orchestrator.py"),
        Path("app/plan_finalization.py"),
    )
    calls: list[tuple[Path, ast.Call]] = []
    for path in event_writer_files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        calls.extend(
            (path, node)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "log_user_event"
        )

    assert calls
    for path, call in calls:
        keywords = {keyword.arg for keyword in call.keywords}
        assert "event_source" in keywords, path
        assert "source_operation_id" in keywords, path


def test_legacy_event_authorities_have_no_runtime_reader_or_writer() -> None:
    telemetry_source = Path("app/telemetry.py").read_text(encoding="utf-8")
    for legacy_model in (
        "PlanInstance",
        "PlanExecutionWindow",
        "TaskStats",
        "FailureSignal",
    ):
        assert legacy_model not in telemetry_source
    assert "_ensure_content_stub" not in telemetry_source
    assert "plan_execution_id=" not in telemetry_source


def test_personal_contribution_has_no_event_join_key() -> None:
    columns = database.AggregateRecord.__table__.c

    assert "event_id" not in columns
    assert "user_id" in columns
    assert "source_operation_id" in columns
    assert "sealed_at" in columns
    assert "supersedes_record_id" in columns

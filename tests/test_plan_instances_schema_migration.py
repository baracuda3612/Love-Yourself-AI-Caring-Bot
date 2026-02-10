from __future__ import annotations

import sqlite3
from pathlib import Path


MIGRATION_PATH = Path("migrations/alembic/versions/20260210_add_plan_instances_versions.py")


def _bootstrap_plan_instances_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE plan_instances (
            id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            blueprint_id TEXT,
            initial_parameters TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def test_missing_version_columns_break_select_like_orm_projection() -> None:
    conn = sqlite3.connect(":memory:")
    _bootstrap_plan_instances_schema(conn)

    conn.execute(
        "INSERT INTO plan_instances (id, user_id, blueprint_id, initial_parameters) VALUES (?, ?, ?, ?)",
        ("pi-1", 1, "burnout_v1", "{}"),
    )

    try:
        conn.execute(
            "SELECT id, user_id, blueprint_id, initial_parameters, contract_version, schema_version FROM plan_instances"
        ).fetchall()
        raised = False
    except sqlite3.OperationalError:
        raised = True

    assert raised is True


def test_added_version_columns_allow_projection_and_defaults() -> None:
    conn = sqlite3.connect(":memory:")
    _bootstrap_plan_instances_schema(conn)

    conn.execute("ALTER TABLE plan_instances ADD COLUMN contract_version TEXT NOT NULL DEFAULT 'v1'")
    conn.execute("ALTER TABLE plan_instances ADD COLUMN schema_version TEXT NOT NULL DEFAULT 'v1'")

    conn.execute(
        "INSERT INTO plan_instances (id, user_id, blueprint_id, initial_parameters) VALUES (?, ?, ?, ?)",
        ("pi-2", 2, "burnout_v1", "{}"),
    )

    rows = conn.execute(
        "SELECT contract_version, schema_version FROM plan_instances WHERE id = ?",
        ("pi-2",),
    ).fetchall()

    assert rows == [("v1", "v1")]


def test_migration_declares_required_upgrade_steps() -> None:
    text = MIGRATION_PATH.read_text(encoding="utf-8")

    assert "revision = \"20260210_add_plan_instances_versions\"" in text
    assert "down_revision = None" in text
    assert '"contract_version"' in text
    assert '"schema_version"' in text
    assert "UPDATE plan_instances SET contract_version = 'v1' WHERE contract_version IS NULL" in text
    assert "UPDATE plan_instances SET schema_version = 'v1' WHERE schema_version IS NULL" in text

def test_scheduler_telemetry_logging_is_guarded_by_exception_handler() -> None:
    source = Path("app/scheduler.py").read_text(encoding="utf-8")

    assert "log_user_event(" in source
    assert "logger.exception(\"Failed to log scheduler telemetry.\")" in source


def test_log_user_event_calls_plan_instance_resolution_path() -> None:
    source = Path("app/telemetry.py").read_text(encoding="utf-8")

    assert "instance = _ensure_plan_instance(db, user_id, plan_instance_id)" in source


def test_startup_schema_audit_has_critical_assertion_for_missing_columns() -> None:
    source = Path("app/db.py").read_text(encoding="utf-8")

    assert "def audit_startup_schema()" in source
    assert "Startup schema audit failed: missing columns on %s: %s" in source
    assert "raise AssertionError(" in source
    assert '"plan_instances": {"contract_version", "schema_version", "initial_parameters"}' in source


def test_main_runs_startup_schema_audit_before_polling() -> None:
    source = Path("app/main.py").read_text(encoding="utf-8")

    assert "from app.db import audit_startup_schema, init_db" in source
    assert "audit_startup_schema()" in source

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

import app.db as database


BASELINE_PATH = Path(
    "migrations/alembic/versions/20260827_physical_schema_baseline.py"
)
LIFECYCLE_PATH = Path(
    "migrations/alembic/versions/20260902_plan_centric_lifecycle.py"
)
EVENT_PRIVACY_PATH = Path(
    "migrations/alembic/versions/20260905_event_privacy_deployment.py"
)


def test_baseline_is_the_single_authoritative_root() -> None:
    versions = sorted(Path("migrations/alembic/versions").glob("*.py"))

    assert versions == [BASELINE_PATH, LIFECYCLE_PATH, EVENT_PRIVACY_PATH]
    source = BASELINE_PATH.read_text(encoding="utf-8")
    assert 'revision = "20260827_schema_baseline"' in source
    assert "down_revision = None" in source
    lifecycle_source = LIFECYCLE_PATH.read_text(encoding="utf-8")
    assert 'revision = "20260902_plan_lifecycle"' in lifecycle_source
    assert 'down_revision = "20260827_schema_baseline"' in lifecycle_source
    event_privacy_source = EVENT_PRIVACY_PATH.read_text(encoding="utf-8")
    assert 'revision = "20260905_event_privacy"' in event_privacy_source
    assert 'down_revision = "20260902_plan_lifecycle"' in event_privacy_source


def test_baseline_owns_application_tables_but_not_scheduler_table() -> None:
    source = BASELINE_PATH.read_text(encoding="utf-8")
    expected_tables = {
        "users",
        "chat_history",
        "content_library",
        "user_profiles",
        "user_facts",
        "ai_plans",
        "ai_plan_days",
        "ai_plan_steps",
        "ai_plan_versions",
        "plan_drafts",
        "plan_draft_steps",
        "plan_instances",
        "plan_execution_windows",
        "user_events",
        "task_stats",
        "failure_signals",
        "user_daily_logs",
    }

    for table_name in expected_tables:
        assert f'        "{table_name}",' in source
    assert 'op.create_table(\n        "apscheduler_jobs"' not in source
    assert "APScheduler owns that table" in source


def test_startup_fails_closed_without_alembic_authority(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = create_engine("sqlite://")
    monkeypatch.setattr(database, "engine", engine)

    with pytest.raises(database.SchemaVersionError, match="not under Alembic authority"):
        database.audit_startup_schema()


def test_startup_accepts_only_the_expected_revision(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(64) NOT NULL)"))
        connection.execute(
            text("INSERT INTO alembic_version (version_num) VALUES (:revision)"),
            {"revision": database.EXPECTED_ALEMBIC_REVISION},
        )
    monkeypatch.setattr(database, "engine", engine)

    database.audit_startup_schema()


def test_startup_rejects_an_incompatible_revision(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(64) NOT NULL)"))
        connection.execute(
            text("INSERT INTO alembic_version (version_num) VALUES ('unexpected_revision')")
        )
    monkeypatch.setattr(database, "engine", engine)

    with pytest.raises(database.SchemaVersionError, match="revision is incompatible"):
        database.audit_startup_schema()


def test_application_startup_is_read_only() -> None:
    main_source = Path("app/main.py").read_text(encoding="utf-8")
    db_source = Path("app/db.py").read_text(encoding="utf-8")

    assert "init_db" not in main_source
    assert "create_all" not in main_source
    assert "Base.metadata.create_all" not in db_source
    assert "audit_startup_schema()" in main_source


def test_alembic_environment_does_not_require_application_secrets() -> None:
    source = Path("migrations/alembic/env.py").read_text(encoding="utf-8")

    assert 'os.environ.get("DATABASE_URL"' in source
    assert "from app.config" not in source
    assert "from app.db" not in source


def test_runtime_image_includes_all_forward_revisions() -> None:
    dockerignore = Path(".dockerignore").read_text(encoding="utf-8")

    assert "!migrations/alembic/versions/*.py" in dockerignore
    assert "!migrations/alembic/versions/20260827_physical_schema_baseline.py" not in dockerignore

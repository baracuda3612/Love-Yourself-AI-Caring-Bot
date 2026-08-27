"""Rehearse the Alembic ledger against a temporary disposable PostgreSQL DB."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import psycopg2
from psycopg2 import sql

from scripts.inspect_database_schema import inspect_database


EXPECTED_REVISION = "20260827_schema_baseline"
EXPECTED_APPLICATION_TABLES = {
    "ai_plan_days",
    "ai_plan_steps",
    "ai_plan_versions",
    "ai_plans",
    "chat_history",
    "content_library",
    "failure_signals",
    "plan_draft_steps",
    "plan_drafts",
    "plan_execution_windows",
    "plan_instances",
    "task_stats",
    "user_daily_logs",
    "user_events",
    "user_facts",
    "user_profiles",
    "users",
}
EXPECTED_ENUMS = {
    "difficulty_level",
    "engagementstatus",
    "fact_category",
    "factcategory",
    "plan_module",
    "plan_status",
    "plan_status_enum",
    "planmodule",
    "sender_role",
    "senderrole",
    "step_type",
    "steptype",
}


def _database_url(base_url: str, database_name: str) -> str:
    parsed = urlsplit(base_url)
    return urlunsplit((parsed.scheme, parsed.netloc, f"/{database_name}", parsed.query, ""))


def _assert_inventory(inventory: dict[str, object]) -> None:
    tables = {row["table_name"] for row in inventory["tables"]}  # type: ignore[index]
    assert tables == EXPECTED_APPLICATION_TABLES | {"alembic_version"}
    assert "apscheduler_jobs" not in tables
    assert inventory["alembic_versions"] == [EXPECTED_REVISION]
    assert sum(inventory["row_counts"].values()) == 1  # type: ignore[union-attr]
    enum_names = {row["enum_name"] for row in inventory["enums"]}  # type: ignore[index]
    assert enum_names == EXPECTED_ENUMS


def _schema_rows(inventory: dict[str, object], key: str) -> set[tuple[object, ...]]:
    fields = {
        "columns": (
            "table_name",
            "column_name",
            "data_type",
            "udt_name",
            "character_maximum_length",
            "numeric_precision",
            "numeric_scale",
            "datetime_precision",
            "is_nullable",
            "column_default",
        ),
        "constraints": (
            "table_name",
            "constraint_name",
            "constraint_type",
            "definition",
        ),
        "indexes": ("table_name", "index_name", "definition"),
        "enums": ("enum_name", "sort_order", "value"),
    }[key]
    rows = set()
    for row in inventory[key]:  # type: ignore[index]
        if row.get("table_name") in {"alembic_version", "apscheduler_jobs"}:
            continue
        values = [row[field] for field in fields]
        if key == "columns" and values[-1] in {"'0'::real", "0"}:
            values[-1] = "0"
        rows.add(tuple(values))
    return rows


def _assert_reference(inventory: dict[str, object], reference_path: Path) -> None:
    reference = json.loads(reference_path.read_text(encoding="utf-8"))
    for key in ("columns", "constraints", "indexes", "enums"):
        actual_rows = _schema_rows(inventory, key)
        reference_rows = _schema_rows(reference, key)
        assert actual_rows == reference_rows, (
            f"{key} differ from inspected schema; "
            f"missing={sorted(reference_rows - actual_rows, key=str)!r}; "
            f"unexpected={sorted(actual_rows - reference_rows, key=str)!r}"
        )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--reference",
        type=Path,
        help="Compare the migrated schema to a content-free inspector JSON file.",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    admin_url = os.environ.get(
        "MIGRATION_TEST_ADMIN_URL",
        "postgresql+psycopg2://love_yourself_test:love_yourself_test@127.0.0.1:55432/love_yourself_test",
    ).replace("postgresql+psycopg2://", "postgresql://", 1)
    database_name = f"ly_migration_{uuid4().hex}"
    target_url = _database_url(admin_url, database_name)

    admin_connection = psycopg2.connect(admin_url, application_name="wp01_migration_harness")
    admin_connection.autocommit = True
    try:
        with admin_connection.cursor() as cursor:
            cursor.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))

        environment = os.environ.copy()
        environment["DATABASE_URL"] = target_url
        subprocess.run(
            [sys.executable, "-m", "alembic", "-c", "alembic.ini", "upgrade", "head"],
            cwd=Path(__file__).resolve().parents[1],
            env=environment,
            check=True,
        )

        target_connection = psycopg2.connect(
            target_url,
            application_name="wp01_migration_verifier",
        )
        try:
            inventory = inspect_database(target_connection)
            _assert_inventory(inventory)
            if args.reference:
                _assert_reference(inventory, args.reference)
        finally:
            target_connection.close()
    finally:
        with admin_connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (database_name,),
            )
            cursor.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(database_name)))
        admin_connection.close()

    print("Alembic migration rehearsal passed on a disposable PostgreSQL database.")


if __name__ == "__main__":
    main()

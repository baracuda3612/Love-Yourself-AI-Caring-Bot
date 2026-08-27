"""Emit a content-free PostgreSQL inventory for WP-01.1.

The inspector uses a database-level read-only transaction and exports only
schema metadata and aggregate counts. It never selects row identifiers or
personal/content-bearing values.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any

import psycopg2
from psycopg2 import sql
from psycopg2.extras import RealDictCursor


AGGREGATED_LEGACY_COLUMNS = (
    ("users", "current_state"),
    ("ai_plans", "status"),
    ("ai_plan_steps", "step_status"),
    ("plan_drafts", "status"),
)


def _static_select(cursor: Any, statement: str, params: Sequence[Any] | None = None) -> list[dict[str, Any]]:
    normalized = " ".join(statement.lstrip().upper().split())
    if not (normalized.startswith("SELECT ") or normalized.startswith("WITH ")):
        raise ValueError("Inspector statements must be read-only SELECT/WITH queries")
    cursor.execute(statement, params)
    return [dict(row) for row in cursor.fetchall()]


def _table_count(cursor: Any, table_name: str) -> int:
    cursor.execute(
        sql.SQL("SELECT COUNT(*) AS row_count FROM {}.{}").format(
            sql.Identifier("public"),
            sql.Identifier(table_name),
        )
    )
    return int(cursor.fetchone()["row_count"])


def _null_counts(cursor: Any, table_name: str, nullable_columns: Sequence[str]) -> dict[str, int]:
    if not nullable_columns:
        return {}

    expressions = [
        sql.SQL("COUNT(*) FILTER (WHERE {} IS NULL) AS {}").format(
            sql.Identifier(column_name),
            sql.Identifier(column_name),
        )
        for column_name in nullable_columns
    ]
    cursor.execute(
        sql.SQL("SELECT {} FROM {}.{}").format(
            sql.SQL(", ").join(expressions),
            sql.Identifier("public"),
            sql.Identifier(table_name),
        )
    )
    return {name: int(value) for name, value in dict(cursor.fetchone()).items()}


def _grouped_counts(cursor: Any, table_name: str, column_name: str) -> list[dict[str, Any]]:
    cursor.execute(
        sql.SQL(
            "SELECT {column}::text AS value, COUNT(*) AS row_count "
            "FROM {schema}.{table} GROUP BY {column} ORDER BY {column} NULLS FIRST"
        ).format(
            column=sql.Identifier(column_name),
            schema=sql.Identifier("public"),
            table=sql.Identifier(table_name),
        )
    )
    return [
        {"value": row["value"], "row_count": int(row["row_count"])}
        for row in cursor.fetchall()
    ]


def inspect_database(connection: Any) -> dict[str, Any]:
    """Return a content-free inventory using one read-only transaction."""

    connection.set_session(readonly=True, autocommit=False)
    with connection.cursor(cursor_factory=RealDictCursor) as cursor:
        cursor.execute("SET LOCAL statement_timeout = '30s'")
        cursor.execute("SET LOCAL lock_timeout = '3s'")

        tables = _static_select(
            cursor,
            """
            SELECT c.relname AS table_name,
                   CASE WHEN c.relname = 'apscheduler_jobs'
                        THEN 'scheduler' ELSE 'application' END AS migration_owner
            FROM pg_catalog.pg_class AS c
            JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public'
              AND c.relkind IN ('p', 'r')
            ORDER BY c.relname
            """,
        )
        table_names = [row["table_name"] for row in tables]

        columns = _static_select(
            cursor,
            """
            SELECT table_name,
                   ordinal_position,
                   column_name,
                   data_type,
                   udt_schema,
                   udt_name,
                   character_maximum_length,
                   numeric_precision,
                   numeric_scale,
                   datetime_precision,
                   is_nullable,
                   column_default
            FROM information_schema.columns
            WHERE table_schema = 'public'
            ORDER BY table_name, ordinal_position
            """,
        )
        columns_by_table: dict[str, list[dict[str, Any]]] = {name: [] for name in table_names}
        for column in columns:
            columns_by_table.setdefault(column["table_name"], []).append(column)

        enums = _static_select(
            cursor,
            """
            SELECT t.typname AS enum_name,
                   e.enumsortorder AS sort_order,
                   e.enumlabel AS value
            FROM pg_catalog.pg_type AS t
            JOIN pg_catalog.pg_enum AS e ON e.enumtypid = t.oid
            JOIN pg_catalog.pg_namespace AS n ON n.oid = t.typnamespace
            WHERE n.nspname = 'public'
            ORDER BY t.typname, e.enumsortorder
            """,
        )
        constraints = _static_select(
            cursor,
            """
            SELECT c.relname AS table_name,
                   con.conname AS constraint_name,
                   con.contype AS constraint_type,
                   pg_get_constraintdef(con.oid, true) AS definition
            FROM pg_catalog.pg_constraint AS con
            JOIN pg_catalog.pg_class AS c ON c.oid = con.conrelid
            JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public'
            ORDER BY c.relname, con.conname
            """,
        )
        indexes = _static_select(
            cursor,
            """
            SELECT tablename AS table_name,
                   indexname AS index_name,
                   indexdef AS definition
            FROM pg_catalog.pg_indexes
            WHERE schemaname = 'public'
            ORDER BY tablename, indexname
            """,
        )

        row_counts = {table_name: _table_count(cursor, table_name) for table_name in table_names}
        null_counts = {
            table_name: _null_counts(
                cursor,
                table_name,
                [
                    column["column_name"]
                    for column in columns_by_table.get(table_name, [])
                    if column["is_nullable"] == "YES"
                ],
            )
            for table_name in table_names
        }

        available_columns = {
            (column["table_name"], column["column_name"])
            for column in columns
        }
        legacy_value_counts = {
            f"{table_name}.{column_name}": _grouped_counts(cursor, table_name, column_name)
            for table_name, column_name in AGGREGATED_LEGACY_COLUMNS
            if (table_name, column_name) in available_columns
        }

        scheduler_jobs: dict[str, Any] = {"owned_by": "APScheduler", "present": False}
        if "apscheduler_jobs" in table_names:
            scheduler_jobs.update(
                _static_select(
                    cursor,
                    """
                    SELECT true AS present,
                           COUNT(*) AS row_count,
                           COUNT(*) FILTER (WHERE next_run_time IS NULL) AS paused_count,
                           COUNT(*) FILTER (
                               WHERE next_run_time < EXTRACT(EPOCH FROM CURRENT_TIMESTAMP)
                           ) AS past_due_count,
                           COUNT(*) FILTER (
                               WHERE next_run_time >= EXTRACT(EPOCH FROM CURRENT_TIMESTAMP)
                           ) AS future_count
                    FROM public.apscheduler_jobs
                    """,
                )[0]
            )
            for key in ("row_count", "paused_count", "past_due_count", "future_count"):
                scheduler_jobs[key] = int(scheduler_jobs[key])

        alembic_versions: list[str] = []
        if "alembic_version" in table_names:
            alembic_versions = [
                row["version_num"]
                for row in _static_select(
                    cursor,
                    "SELECT version_num FROM public.alembic_version ORDER BY version_num",
                )
            ]

        result = {
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "inspection_mode": "read-only schema metadata and aggregate counts; no row content",
            "server_version": connection.server_version,
            "alembic_versions": alembic_versions,
            "tables": tables,
            "columns": columns,
            "enums": enums,
            "constraints": constraints,
            "indexes": indexes,
            "row_counts": row_counts,
            "null_counts": null_counts,
            "legacy_value_counts": legacy_value_counts,
            "scheduler_jobs": scheduler_jobs,
        }

    connection.rollback()
    return result


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url-file",
        type=Path,
        help="Read DATABASE_URL from a chmod-600 env file instead of the process environment.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Write JSON to this path instead of stdout.",
    )
    return parser.parse_args()


def _database_url(args: argparse.Namespace) -> str:
    if args.database_url_file:
        for line in args.database_url_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("DATABASE_URL="):
                return line.removeprefix("DATABASE_URL=").strip()
        return ""
    return os.environ.get("DATABASE_URL", "").strip()


def main() -> None:
    args = _parse_args()
    database_url = _database_url(args)
    if not database_url:
        raise SystemExit("DATABASE_URL is required")

    connection = psycopg2.connect(
        database_url,
        connect_timeout=10,
        application_name="wp01_read_only_inspector",
    )
    try:
        inventory = inspect_database(connection)
    finally:
        connection.close()

    rendered = json.dumps(inventory, ensure_ascii=False, indent=2, sort_keys=True, default=str)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)


if __name__ == "__main__":
    main()

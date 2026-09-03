"""Rehearse the Alembic ledger against a temporary disposable PostgreSQL DB."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Barrier
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import psycopg2
from psycopg2 import sql

from scripts.inspect_database_schema import inspect_database


EXPECTED_REVISION = "20260902_plan_lifecycle"
EXPECTED_APPLICATION_TABLES = {
    "ai_plan_days",
    "ai_plan_steps",
    "ai_plan_versions",
    "ai_plans",
    "chat_history",
    "content_library",
    "failure_signals",
    "onboarding_progress",
    "plan_draft_steps",
    "plan_drafts",
    "plan_execution_windows",
    "plan_instances",
    "plan_lifecycle_operations",
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
    "legacy_plan_status",
    "plan_status",
    "plan_status_enum",
    "plan_step_status",
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
    enum_names = {row["enum_name"] for row in inventory["enums"]}  # type: ignore[index]
    assert enum_names == EXPECTED_ENUMS


def _run_alembic(target_url: str, revision: str, *, check: bool = True) -> subprocess.CompletedProcess:
    environment = os.environ.copy()
    environment["DATABASE_URL"] = target_url
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", "upgrade", revision],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        check=check,
        capture_output=not check,
        text=not check,
    )


def _seed_legacy_rows(connection, *, duplicate_current: bool = False) -> None:
    """Insert content-free lifecycle shapes at the authoritative baseline."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO content_library (
              id, content_version, internal_name, category, difficulty,
              energy_cost, logic_tags, content_payload, is_active
            ) VALUES ('migration-seed', 1, 'seed', 'seed', 1, 'low', '{}', '{}', true)
            """
        )
        cursor.execute(
            """
            INSERT INTO users (tg_id, current_state, timezone, is_active)
            VALUES (9000001, %s, 'Europe/Kyiv', true)
            RETURNING id
            """,
            ("ACTIVE" if duplicate_current else "IDLE_FINISHED",),
        )
        user_id = cursor.fetchone()[0]
        plan_count = 2 if duplicate_current else 1
        for plan_offset in range(plan_count):
            cursor.execute(
                """
                INSERT INTO ai_plans (
                  user_id, title, module_id, status, start_date, created_at,
                  activated_at, total_days, preferred_time_slots
                ) VALUES (
                  %s, %s, 'burnout_recovery', %s,
                  '2026-08-01 09:00:00+00',
                  %s::timestamptz, '1999-01-01 00:00:00', 7, '[]'
                ) RETURNING id
                """,
                (
                    user_id,
                    f"seed-{plan_offset}",
                    "active" if duplicate_current else "completed",
                    f"2026-08-{1 + plan_offset:02d} 09:00:00+00",
                ),
            )
            plan_id = cursor.fetchone()[0]
            for day_number in range(1, 8):
                cursor.execute(
                    "INSERT INTO ai_plan_days (plan_id, day_number) VALUES (%s, %s) RETURNING id",
                    (plan_id, day_number),
                )
                day_id = cursor.fetchone()[0]
                if duplicate_current:
                    status = "pending"
                    is_completed = False
                    skipped = False
                    completed_at = None
                elif day_number == 2:
                    status = "skipped"
                    is_completed = False
                    skipped = True
                    completed_at = None
                elif day_number == 3:
                    status = "expired"
                    is_completed = False
                    skipped = False
                    completed_at = None
                elif day_number == 4:
                    # The boolean proves completion, but no timestamp may be
                    # invented; the migration preserves this as version 0.
                    status = "pending"
                    is_completed = True
                    skipped = False
                    completed_at = None
                else:
                    # Legacy boolean/timestamp is stronger evidence than the
                    # deliberately stale pending string on these rows.
                    status = "pending"
                    is_completed = True
                    skipped = False
                    completed_at = f"2026-08-{day_number:02d} 15:00:00+00"
                cursor.execute(
                    """
                    INSERT INTO ai_plan_steps (
                      day_id, title, order_in_day, scheduled_for, is_completed,
                      completed_at, skipped, exercise_id, time_slot, slot_type,
                      step_status, expires_at, mechanic
                    ) VALUES (
                      %s, %s, 0, %s::timestamptz, %s, %s::timestamptz, %s,
                      'migration-seed', 'DAY', 'CORE', %s,
                      %s::timestamptz, 'switch'
                    )
                    """,
                    (
                        day_id,
                        f"day-{day_number}",
                        f"2026-08-{day_number:02d} 14:00:00+00",
                        is_completed,
                        completed_at,
                        skipped,
                        status,
                        f"2026-08-{day_number:02d} 23:00:00+00",
                    ),
                )
    connection.commit()


def _assert_seeded_backfill(connection) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT status::text, cycle_number,
                   activated_at = '2026-08-01 09:00:00+00'::timestamptz,
                   current_mode IS NULL AND current_day IS NULL AND end_date IS NULL
            FROM ai_plans WHERE title = 'seed-0'
            """
        )
        assert cursor.fetchone() == ("completed", 1, True, True)
        cursor.execute(
            "SELECT current_state IS NULL AND plan_end_date IS NULL FROM users"
        )
        assert cursor.fetchone()[0] is True
        cursor.execute(
            "SELECT stage, completed_at FROM onboarding_progress"
        )
        assert cursor.fetchone() == ("COMPLETED", None)
        cursor.execute(
            """
            SELECT step_status::text, terminal_at IS NOT NULL, version
            FROM ai_plan_steps
            JOIN ai_plan_days ON ai_plan_days.id = ai_plan_steps.day_id
            WHERE ai_plan_days.day_number IN (1,2,3,4)
            ORDER BY ai_plan_days.day_number
            """
        )
        assert cursor.fetchall() == [
            ("completed", True, 1),
            ("skipped", False, 0),
            ("expired", True, 1),
            ("completed", False, 0),
        ]
        cursor.execute(
            "SELECT bool_and(is_completed IS NULL AND skipped IS NULL "
            "AND completed_at IS NULL) FROM ai_plan_steps"
        )
        assert cursor.fetchone()[0] is True
        cursor.execute(
            "SELECT count(*) FROM pg_indexes WHERE indexname = 'ux_ai_plans_one_current_per_user'"
        )
        assert cursor.fetchone()[0] == 1


def _assert_failed_upgrade_rolled_back(connection) -> None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT version_num FROM alembic_version")
        assert cursor.fetchone()[0] == "20260827_schema_baseline"
        cursor.execute("SELECT to_regclass('public.onboarding_progress')")
        assert cursor.fetchone()[0] is None
        cursor.execute(
            "SELECT count(*) FROM pg_type WHERE typname = 'legacy_plan_status'"
        )
        assert cursor.fetchone()[0] == 0


def _seed_authoritative_plan(
    connection,
    *,
    tg_id: int,
    step_status: str = "pending",
) -> tuple[int, int, int]:
    terminal_at = "2026-09-01 12:00:00+00" if step_status in {
        "completed",
        "skipped",
        "expired",
        "canceled",
    } else None
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO users (tg_id, timezone, is_active) "
            "VALUES (%s, 'Europe/Kyiv', true) RETURNING id",
            (tg_id,),
        )
        user_id = cursor.fetchone()[0]
        cursor.execute(
            "INSERT INTO onboarding_progress (user_id, stage, started_at) "
            "VALUES (%s, 'COMPLETED', now())",
            (user_id,),
        )
        cursor.execute(
            """
            INSERT INTO ai_plans (
              user_id, title, module_id, status, cycle_number,
              activated_at, start_date, total_days, preferred_time_slots, version
            ) VALUES (
              %s, 'concurrency-seed', 'burnout_recovery', 'active', 1,
              '2026-09-01 09:00:00+00', '2026-09-01 09:00:00+00', 7, '[]', 1
            ) RETURNING id
            """,
            (user_id,),
        )
        plan_id = cursor.fetchone()[0]
        first_step_id = 0
        for day_number in range(1, 8):
            cursor.execute(
                "INSERT INTO ai_plan_days (plan_id, day_number) "
                "VALUES (%s, %s) RETURNING id",
                (plan_id, day_number),
            )
            day_id = cursor.fetchone()[0]
            cursor.execute(
                """
                INSERT INTO ai_plan_steps (
                  day_id, title, order_in_day, exercise_id, mechanic,
                  step_status, terminal_at, version, slot_type
                ) VALUES (%s, %s, 0, 'migration-seed', 'switch',
                          %s, %s::timestamptz, 1, 'CORE')
                RETURNING id
                """,
                (day_id, f"concurrency-day-{day_number}", step_status, terminal_at),
            )
            step_id = cursor.fetchone()[0]
            if not first_step_id:
                first_step_id = step_id
    connection.commit()
    return user_id, plan_id, first_step_id


def _seed_activation_draft(connection, *, tg_id: int) -> tuple[int, str]:
    draft_id = str(uuid4())
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO users (tg_id, timezone, is_active) "
            "VALUES (%s, 'Europe/Kyiv', true) RETURNING id",
            (tg_id,),
        )
        user_id = cursor.fetchone()[0]
        cursor.execute(
            "INSERT INTO onboarding_progress (user_id, stage, started_at) "
            "VALUES (%s, 'START', now())",
            (user_id,),
        )
        cursor.execute(
            """
            INSERT INTO plan_drafts (
              id, user_id, status, duration, draft_data,
              total_days, total_steps, is_valid
            ) VALUES (%s::uuid, %s, 'DRAFT', 'SHORT', '{}', 7, 7, true)
            """,
            (draft_id, user_id),
        )
        for day_number in range(1, 8):
            cursor.execute(
                """
                INSERT INTO plan_draft_steps (
                  id, draft_id, day_number, exercise_id, mechanic, time_slot
                ) VALUES (%s::uuid, %s::uuid, %s, 'migration-seed', 'switch', 'DAY')
                """,
                (str(uuid4()), draft_id, day_number),
            )
    connection.commit()
    return user_id, draft_id


def _assert_lifecycle_concurrency(target_url: str) -> None:
    """Exercise row locks, idempotency receipts, and invariant winners."""
    sqlalchemy_url = target_url.replace(
        "postgresql://",
        "postgresql+psycopg2://",
        1,
    )
    os.environ.setdefault("ENVIRONMENT", "dev")
    os.environ.setdefault("BOT_TOKEN", "123456:LOCAL_MIGRATION_TEST")
    os.environ["DATABASE_URL"] = sqlalchemy_url
    os.environ.setdefault("OPENAI_API_KEY", "local-migration-test")

    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker

    from app.db import AIPlanDay, AIPlanStep, PlanDraftRecord
    from app.lifecycle import (
        LifecycleTransitionError,
        abandon_current_plan,
        complete_current_plan_if_ready,
        transition_current_plan,
        transition_plan_step,
    )
    from app.plan_finalization import finalize_plan

    connection = psycopg2.connect(
        target_url,
        application_name="wp01_lifecycle_concurrency_seed",
    )
    try:
        user_id, draft_id = _seed_activation_draft(
            connection,
            tg_id=9000002,
        )
        completed_user_id, completed_plan_id, _ = _seed_authoritative_plan(
            connection,
            tg_id=9000003,
            step_status="completed",
        )
    finally:
        connection.close()

    engine = create_engine(sqlalchemy_url, pool_size=4, max_overflow=0)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        activation_barrier = Barrier(2)

        def activation_retry() -> tuple[int, bool]:
            with Session() as session:
                draft = (
                    session.query(PlanDraftRecord)
                    .filter(PlanDraftRecord.id == draft_id)
                    .first()
                )
                assert draft is not None
                activation_barrier.wait()
                activation = finalize_plan(
                    session,
                    user_id,
                    draft,
                    activation_time_utc=datetime(2026, 9, 1, 9, tzinfo=timezone.utc),
                    source_operation_id="concurrent-activation",
                )
                session.commit()
                return activation.plan.id, activation.duplicate

        with ThreadPoolExecutor(max_workers=2) as pool:
            activation_plan_ids = list(
                pool.map(lambda _index: activation_retry(), range(2))
            )
        assert len({result[0] for result in activation_plan_ids}) == 1
        assert sorted(result[1] for result in activation_plan_ids) == [False, True]
        plan_id = activation_plan_ids[0][0]
        with Session() as session:
            step_ids = [
                row[0]
                for row in (
                    session.query(AIPlanStep.id)
                    .join(AIPlanDay, AIPlanDay.id == AIPlanStep.day_id)
                    .filter(AIPlanDay.plan_id == plan_id)
                    .order_by(AIPlanStep.id)
                    .limit(2)
                    .all()
                )
            ]
            assert len(step_ids) == 2
            step_id = step_ids[0]

        pause_barrier = Barrier(2)

        def pause_retry() -> bool:
            with Session.begin() as session:
                pause_barrier.wait()
                result = transition_current_plan(
                    session,
                    user_id=user_id,
                    operation="pause",
                    source_operation_id="concurrent-pause",
                )
                return result.duplicate

        with ThreadPoolExecutor(max_workers=2) as pool:
            pause_results = list(pool.map(lambda _index: pause_retry(), range(2)))
        assert sorted(pause_results) == [False, True]

        with Session.begin() as session:
            try:
                transition_current_plan(
                    session,
                    user_id=user_id,
                    operation="resume",
                    source_operation_id="concurrent-pause",
                )
            except LifecycleTransitionError as exc:
                assert "already belongs to pause" in str(exc)
            else:
                raise AssertionError("cross-operation source reuse unexpectedly accepted")

        with Session.begin() as session:
            resumed = transition_current_plan(
                session,
                user_id=user_id,
                operation="resume",
                source_operation_id="resume-after-concurrent-pause",
            )
            assert resumed.status == "active"

        # The partial unique index is immediate and rejects a second current
        # aggregate before any application ordering could choose a winner.
        with Session() as session:
            try:
                session.execute(
                    text(
                        "INSERT INTO ai_plans ("
                        "user_id,title,module_id,status,cycle_number,activated_at,"
                        "start_date,total_days,preferred_time_slots,version"
                        ") VALUES ("
                        ":user_id,'duplicate','burnout_recovery','active',2,now(),"
                        "now(),7,'[]',1)"
                    ),
                    {"user_id": user_id},
                )
                session.flush()
            except Exception as exc:
                session.rollback()
                assert "ux_ai_plans_one_current_per_user" in str(exc)
            else:
                raise AssertionError("second current plan unexpectedly inserted")

        winner_barrier = Barrier(2)

        def terminal_attempt(target_status: str) -> tuple[str, str]:
            with Session() as session:
                winner_barrier.wait()
                try:
                    result = transition_plan_step(
                        session,
                        user_id=user_id,
                        step_id=step_id,
                        target_status=target_status,
                        source_operation_id=f"terminal-{target_status}",
                    )
                    session.commit()
                    return "won", result.status
                except LifecycleTransitionError:
                    session.rollback()
                    return "rejected", target_status

        with ThreadPoolExecutor(max_workers=2) as pool:
            terminal_results = list(
                pool.map(terminal_attempt, ("completed", "skipped"))
            )
        assert [result[0] for result in terminal_results].count("won") == 1
        assert [result[0] for result in terminal_results].count("rejected") == 1

        winning_status = next(result[1] for result in terminal_results if result[0] == "won")
        with Session.begin() as session:
            retry = transition_plan_step(
                session,
                user_id=user_id,
                step_id=step_id,
                target_status=winning_status,
                source_operation_id=f"terminal-{winning_status}",
            )
            assert retry.duplicate is True
        with Session.begin() as session:
            try:
                transition_plan_step(
                    session,
                    user_id=user_id,
                    step_id=step_ids[1],
                    target_status=winning_status,
                    source_operation_id=f"terminal-{winning_status}",
                )
            except LifecycleTransitionError as exc:
                assert "already belongs to plan step" in str(exc)
            else:
                raise AssertionError("cross-step source reuse unexpectedly accepted")

        with Session.begin() as session:
            abandoned, canceled_ids = abandon_current_plan(
                session,
                user_id=user_id,
                source_operation_id="abandon-plan",
            )
            assert abandoned.status == "abandoned"
            assert len(canceled_ids) == 6
        with Session.begin() as session:
            abandoned_retry, canceled_ids = abandon_current_plan(
                session,
                user_id=user_id,
                source_operation_id="abandon-plan",
            )
            assert abandoned_retry.duplicate is True
            assert canceled_ids == []

        with Session.begin() as session:
            completed = complete_current_plan_if_ready(
                session,
                user_id=completed_user_id,
                source_operation_id="complete-plan",
            )
            assert completed is not None and completed.status == "completed"
        with Session.begin() as session:
            completed_retry = complete_current_plan_if_ready(
                session,
                user_id=completed_user_id,
                source_operation_id="complete-plan",
            )
            assert completed_retry is not None and completed_retry.duplicate is True

        with engine.connect() as verification:
            receipt_count = verification.execute(
                text(
                    "SELECT count(*) FROM plan_lifecycle_operations "
                    "WHERE user_id IN (:user_id, :completed_user_id)"
                ),
                {
                    "user_id": user_id,
                    "completed_user_id": completed_user_id,
                },
            ).scalar_one()
            assert receipt_count == 6
            plan_row = verification.execute(
                text(
                    "SELECT status::text, abandoned_at IS NOT NULL FROM ai_plans "
                    "WHERE id = :plan_id"
                ),
                {"plan_id": plan_id},
            ).one()
            assert plan_row == ("abandoned", True)
            open_steps = verification.execute(
                text(
                    "SELECT count(*) FROM ai_plan_steps s "
                    "JOIN ai_plan_days d ON d.id=s.day_id "
                    "WHERE d.plan_id=:plan_id "
                    "AND s.step_status IN ('pending','delivered')"
                ),
                {"plan_id": plan_id},
            ).scalar_one()
            assert open_steps == 0
            assert verification.execute(
                text("SELECT status::text FROM ai_plans WHERE id=:plan_id"),
                {"plan_id": completed_plan_id},
            ).scalar_one() == "completed"

        def assert_rejected(statement: str, marker: str, **params: object) -> None:
            with engine.connect() as rejection:
                transaction = rejection.begin()
                try:
                    rejection.execute(text(statement), params)
                    transaction.commit()
                except Exception as exc:
                    transaction.rollback()
                    assert marker in str(exc), str(exc)
                else:
                    raise AssertionError(f"database unexpectedly accepted: {statement}")

        assert_rejected(
            "UPDATE ai_plans SET status='draft' WHERE id=:plan_id",
            "invalid input value for enum plan_status",
            plan_id=plan_id,
        )
        assert_rejected(
            "UPDATE ai_plan_steps SET step_status='done' WHERE id=:step_id",
            "invalid input value for enum plan_step_status",
            step_id=step_ids[1],
        )
        assert_rejected(
            "UPDATE ai_plans SET abandoned_at=activated_at - interval '1 second' "
            "WHERE id=:plan_id",
            "ck_ai_plans_chronology",
            plan_id=plan_id,
        )
        assert_rejected(
            "UPDATE ai_plan_steps SET terminal_at=NULL WHERE id=:step_id",
            "ck_ai_plan_steps_terminal_timestamp",
            step_id=step_ids[1],
        )
        assert_rejected(
            "UPDATE ai_plan_days SET day_number=8 "
            "WHERE plan_id=:plan_id AND day_number=7",
            "out-of-range day",
            plan_id=plan_id,
        )
        assert_rejected(
            "UPDATE ai_plan_steps SET step_status='pending', terminal_at=NULL, "
            "version=version+1 WHERE id=:step_id",
            "contains an open step",
            step_id=step_ids[1],
        )
    finally:
        engine.dispose()


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
    resume_database_name = f"ly_migration_resume_{uuid4().hex}"
    target_url = _database_url(admin_url, database_name)
    resume_target_url = _database_url(admin_url, resume_database_name)

    admin_connection = psycopg2.connect(admin_url, application_name="wp01_migration_harness")
    admin_connection.autocommit = True
    try:
        with admin_connection.cursor() as cursor:
            cursor.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
            cursor.execute(
                sql.SQL("CREATE DATABASE {}").format(sql.Identifier(resume_database_name))
            )

        _run_alembic(target_url, "20260827_schema_baseline")
        target_connection = psycopg2.connect(
            target_url,
            application_name="wp01_migration_verifier",
        )
        try:
            if args.reference:
                _assert_reference(inspect_database(target_connection), args.reference)
            _seed_legacy_rows(target_connection)
        finally:
            target_connection.close()

        _run_alembic(target_url, "head")
        _run_alembic(target_url, "head")
        target_connection = psycopg2.connect(
            target_url,
            application_name="wp01_migration_verifier",
        )
        try:
            _assert_inventory(inspect_database(target_connection))
            _assert_seeded_backfill(target_connection)
        finally:
            target_connection.close()
        _assert_lifecycle_concurrency(target_url)

        _run_alembic(resume_target_url, "20260827_schema_baseline")
        resume_connection = psycopg2.connect(
            resume_target_url,
            application_name="wp01_migration_resume_verifier",
        )
        try:
            _seed_legacy_rows(resume_connection, duplicate_current=True)
        finally:
            resume_connection.close()

        failed_upgrade = _run_alembic(resume_target_url, "head", check=False)
        assert failed_upgrade.returncode != 0, "ambiguous legacy state unexpectedly migrated"
        assert "multiple current plans require evidence-based remediation" in (
            failed_upgrade.stderr or ""
        )
        resume_connection = psycopg2.connect(
            resume_target_url,
            application_name="wp01_migration_resume_verifier",
        )
        try:
            _assert_failed_upgrade_rolled_back(resume_connection)
            with resume_connection.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM ai_plans WHERE id = "
                    "(SELECT max(id) FROM ai_plans WHERE status IN ('active', 'paused'))"
                )
            resume_connection.commit()
        finally:
            resume_connection.close()

        _run_alembic(resume_target_url, "head")
        _run_alembic(resume_target_url, "head")
        resume_connection = psycopg2.connect(
            resume_target_url,
            application_name="wp01_migration_resume_verifier",
        )
        try:
            _assert_inventory(inspect_database(resume_connection))
        finally:
            resume_connection.close()
    finally:
        with admin_connection.cursor() as cursor:
            for disposable_database in (database_name, resume_database_name):
                cursor.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = %s AND pid <> pg_backend_pid()",
                    (disposable_database,),
                )
                cursor.execute(
                    sql.SQL("DROP DATABASE IF EXISTS {}").format(
                        sql.Identifier(disposable_database)
                    )
                )
        admin_connection.close()

    print("Alembic migration rehearsal passed on a disposable PostgreSQL database.")


if __name__ == "__main__":
    main()

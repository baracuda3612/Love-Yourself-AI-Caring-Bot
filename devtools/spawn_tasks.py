"""Dev-only CLI to spawn or reschedule plan steps for scheduler testing.

Usage examples:
  python -m devtools.spawn_tasks create \
    --user-id 42 \
    --plan-id 7 \
    --count 20 \
    --interval 2 \
    --start now

  python -m devtools.spawn_tasks create \
    --user-id 42 \
    --plan-id 7 \
    --count 50 \
    --interval 1 \
    --start "2026-02-08T20:00"

  python -m devtools.spawn_tasks reschedule \
    --user-id 42 \
    --plan-id 7 \
    --step-ids 101 102 103 \
    --interval 1 \
    --start "2026-02-08T20:00"

Parameters:
  --user-id: Active user id (required).
  --plan-id: Active plan id (required).
  --start: Base datetime (ISO-8601, "now", or "now+<seconds>").
  --interval: Interval in seconds between steps.
  --count: Number of steps to create (create subcommand only).
  --title/--description: Optional overrides for step content.
  --delay: Delivery delay (seconds) when start="now" or "now+<seconds>".
  --utc: Interpret naive datetimes as UTC instead of user timezone.
  --wait-seconds: Keep the process alive for a while to allow local delivery.

  python -m devtools.spawn_tasks schedule-custom \
    --user-id 42 \
    --title "Test task" \
    --description "Manual override task" \
    --datetime "2026-02-11T18:55:00"
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Iterable, Sequence

import pytz

from devtools.common import require_dev_environment

logger = logging.getLogger(__name__)

_NOW_PATTERN = re.compile(r"^now(?:\+(?P<seconds>\d+))?$")


def _normalize_timezone(name: str | None) -> pytz.BaseTzInfo:
    try:
        return pytz.timezone(name or "UTC")
    except pytz.UnknownTimeZoneError:
        return pytz.timezone("UTC")


def _parse_start(value: str, user_timezone: str | None, delay_seconds: int, use_utc: bool) -> datetime:
    match = _NOW_PATTERN.match(value)
    if match:
        base_tz = pytz.UTC if use_utc else _normalize_timezone(user_timezone)
        base = datetime.now(base_tz)
        extra = int(match.group("seconds") or 0)
        if delay_seconds:
            extra += delay_seconds
        return base + timedelta(seconds=extra)

    if value.endswith("Z"):
        value = value.replace("Z", "+00:00")

    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        tz = pytz.UTC if use_utc else _normalize_timezone(user_timezone)
        parsed = tz.localize(parsed)
    return parsed


def _ensure_scheduler_loop() -> None:
    from app import scheduler as plan_scheduler

    plan_scheduler.init_scheduler()

    if plan_scheduler._event_loop is not None:
        return

    loop = asyncio.new_event_loop()

    def _run_loop() -> None:
        asyncio.set_event_loop(loop)
        loop.run_forever()

    thread = threading.Thread(target=_run_loop, daemon=True)
    thread.start()
    plan_scheduler._event_loop = loop


def _load_active_plan(db, user_id: int, plan_id: int):
    from app.db import AIPlan

    return (
        db.query(AIPlan)
        .filter(AIPlan.id == plan_id, AIPlan.user_id == user_id)
        .first()
    )


def _resolve_plan_day(plan) -> tuple:
    existing_days = list(plan.days)
    if existing_days:
        preferred = next((day for day in existing_days if day.day_number == plan.current_day), None)
        if preferred:
            return preferred, False
        return max(existing_days, key=lambda day: day.day_number), False

    from app.db import AIPlanDay

    day = AIPlanDay(plan_id=plan.id, day_number=1, focus_theme="devtools")
    return day, True


def _next_order_in_day(day) -> int:
    if not day.steps:
        return 1
    return max(step.order_in_day for step in day.steps) + 1


def _format_step_title(title: str | None, index: int) -> str:
    if title:
        return title
    return f"Dev Task {index + 1}"


def _format_step_description(description: str | None) -> str:
    if description:
        return description
    return "Spawned by devtools for scheduler testing."


def _create_steps(
    *,
    user_id: int,
    plan_id: int,
    base_time: datetime,
    interval_seconds: int,
    count: int,
    title: str | None,
    description: str | None,
) -> int:
    from app.db import AIPlanStep, SessionLocal
    from app.scheduler import can_deliver_tasks, schedule_plan_step

    with SessionLocal() as db:
        plan = _load_active_plan(db, user_id, plan_id)
        if not plan:
            raise ValueError("active plan not found for user")
        user = plan.user
        if not user or not user.is_active or not can_deliver_tasks(user):
            raise ValueError("user is not ACTIVE")
        if plan.status != "active":
            raise ValueError("plan is not active")

        day, is_new_day = _resolve_plan_day(plan)
        if is_new_day:
            db.add(day)
            db.flush()

        base_utc = base_time.astimezone(pytz.UTC)
        order = _next_order_in_day(day)
        steps: list[AIPlanStep] = []

        for i in range(count):
            scheduled_for = base_utc + timedelta(seconds=i * interval_seconds)
            step = AIPlanStep(
                day_id=day.id,
                title=_format_step_title(title, i),
                description=_format_step_description(description),
                order_in_day=order + i,
                time_slot="DAY",
                scheduled_for=scheduled_for,
            )
            steps.append(step)
        db.add_all(steps)
        db.flush()

        _ensure_scheduler_loop()

        scheduled = 0
        for step in steps:
            if schedule_plan_step(step, user):
                scheduled += 1

        db.commit()

    logger.info("Created %s plan steps (scheduled %s).", count, scheduled)
    return count


def _reschedule_steps(
    *,
    user_id: int,
    plan_id: int,
    step_ids: Sequence[int],
    base_time: datetime,
    interval_seconds: int,
) -> int:
    from app.db import AIPlan, AIPlanDay, AIPlanStep, SessionLocal, User
    from app.scheduler import can_deliver_tasks, schedule_plan_step

    if not step_ids:
        raise ValueError("step_ids are required for reschedule")

    with SessionLocal() as db:
        rows = (
            db.query(AIPlanStep, AIPlanDay, AIPlan, User)
            .join(AIPlanDay, AIPlanDay.id == AIPlanStep.day_id)
            .join(AIPlan, AIPlan.id == AIPlanDay.plan_id)
            .join(User, User.id == AIPlan.user_id)
            .filter(AIPlan.id == plan_id, User.id == user_id, AIPlanStep.id.in_(step_ids))
            .all()
        )

        if not rows:
            raise ValueError("no matching steps found to reschedule")

        base_utc = base_time.astimezone(pytz.UTC)
        _ensure_scheduler_loop()

        updated = 0
        scheduled = 0
        for index, (step, _, plan, user) in enumerate(rows):
            if plan.status != "active":
                continue
            if not user or not user.is_active or not can_deliver_tasks(user):
                continue
            if step.is_completed or step.skipped:
                continue

            step.scheduled_for = base_utc + timedelta(seconds=index * interval_seconds)
            updated += 1
            if schedule_plan_step(step, user):
                scheduled += 1

        db.commit()

    logger.info("Rescheduled %s plan steps (scheduled %s).", updated, scheduled)
    return updated


def _parse_step_ids(values: Iterable[str]) -> list[int]:
    if not values:
        return []
    return [int(value) for value in values]


def _parse_custom_datetime(value: str) -> datetime:
    parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S")
    return parsed.replace(tzinfo=timezone.utc)


def _load_or_create_active_plan(db, user) -> tuple:
    from app.db import AIPlan, AIPlanDay

    plan = (
        db.query(AIPlan)
        .filter(AIPlan.user_id == user.id, AIPlan.status == "active")
        .order_by(AIPlan.id.desc())
        .first()
    )
    is_new_plan = False

    if plan is None:
        plan = AIPlan(
            user_id=user.id,
            title="[DEV] Temporary manual scheduling plan",
            goal_description="Created by devtools schedule-custom manual override.",
            status="active",
            current_day=1,
        )
        db.add(plan)
        db.flush()
        is_new_plan = True

    day, is_new_day = _resolve_plan_day(plan)
    if is_new_day:
        db.add(day)
        db.flush()

    return plan, day, is_new_plan


def _schedule_custom_step(*, user_id: int, title: str, description: str, scheduled_for: datetime) -> tuple[int, str]:
    from app.db import AIPlanStep, SessionLocal, User
    from app.scheduler import _generate_step_job_id, can_deliver_tasks, schedule_plan_step

    with SessionLocal() as db:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            raise ValueError("user not found")
        if not user.is_active or not can_deliver_tasks(user):
            raise ValueError("user is not ACTIVE")

        _, day, _ = _load_or_create_active_plan(db, user)

        step = AIPlanStep(
            day_id=day.id,
            title=title,
            description=description,
            is_completed=False,
            skipped=False,
            scheduled_for=scheduled_for.astimezone(pytz.UTC),
            step_type="action",
            difficulty="easy",
            order_in_day=0,
            time_slot="DAY",
        )
        db.add(step)
        db.flush()

        _ensure_scheduler_loop()
        schedule_plan_step(step, user)
        job_id = _generate_step_job_id(user.id, step)

        db.commit()

    return step.id, job_id


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Spawn plan steps for scheduler testing.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create", help="Create new scheduled plan steps")
    create.add_argument("--user-id", type=int, required=True)
    create.add_argument("--plan-id", type=int, required=True)
    create.add_argument("--count", type=int, required=True)
    create.add_argument("--interval", type=int, default=60)
    create.add_argument("--start", required=True)
    create.add_argument("--title")
    create.add_argument("--description")
    create.add_argument("--delay", type=int, default=5)
    create.add_argument("--utc", action="store_true")
    create.add_argument("--wait-seconds", type=int, default=0)

    reschedule = subparsers.add_parser("reschedule", help="Reschedule existing plan steps")
    reschedule.add_argument("--user-id", type=int, required=True)
    reschedule.add_argument("--plan-id", type=int, required=True)
    reschedule.add_argument("--step-ids", nargs="+", required=True)
    reschedule.add_argument("--interval", type=int, default=60)
    reschedule.add_argument("--start", required=True)
    reschedule.add_argument("--delay", type=int, default=5)
    reschedule.add_argument("--utc", action="store_true")
    reschedule.add_argument("--wait-seconds", type=int, default=0)

    schedule_custom = subparsers.add_parser(
        "schedule-custom",
        help="Schedule a single step for a user at an exact UTC datetime",
    )
    schedule_custom.add_argument("--user-id", type=int, required=True)
    schedule_custom.add_argument("--title", required=True)
    schedule_custom.add_argument("--description", required=True)
    schedule_custom.add_argument("--datetime", required=True)
    schedule_custom.add_argument("--wait-seconds", type=int, default=0)

    return parser


def main() -> None:
    try:
        require_dev_environment()
        parser = build_parser()
        args = parser.parse_args()

        if args.command == "create":
            from app.db import SessionLocal

            with SessionLocal() as db:
                from app.db import User

                user = db.query(User).filter(User.id == args.user_id).first()
                if not user:
                    raise ValueError("user not found")
                timezone_name = user.timezone
            base_time = _parse_start(args.start, timezone_name, args.delay, args.utc)
            _create_steps(
                user_id=args.user_id,
                plan_id=args.plan_id,
                base_time=base_time,
                interval_seconds=args.interval,
                count=args.count,
                title=args.title,
                description=args.description,
            )
        elif args.command == "reschedule":
            from app.db import SessionLocal

            with SessionLocal() as db:
                from app.db import User

                user = db.query(User).filter(User.id == args.user_id).first()
                if not user:
                    raise ValueError("user not found")
                timezone_name = user.timezone
            base_time = _parse_start(args.start, timezone_name, args.delay, args.utc)
            _reschedule_steps(
                user_id=args.user_id,
                plan_id=args.plan_id,
                step_ids=_parse_step_ids(args.step_ids),
                base_time=base_time,
                interval_seconds=args.interval,
            )
        elif args.command == "schedule-custom":
            scheduled_for = _parse_custom_datetime(args.datetime)
            step_id, job_id = _schedule_custom_step(
                user_id=args.user_id,
                title=args.title,
                description=args.description,
                scheduled_for=scheduled_for,
            )
            print(f"Scheduled task for user {args.user_id} at {scheduled_for.strftime('%Y-%m-%dT%H:%M:%S')} UTC")
            print(f"Step ID: {step_id}")
            print(f"Job ID: {job_id}")
        else:
            parser.error("Unknown command")

        if args.wait_seconds:
            logger.info("Waiting %s seconds for local delivery...", args.wait_seconds)
            time.sleep(args.wait_seconds)
    except Exception as exc:  # noqa: BLE001 - CLI entrypoint
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()

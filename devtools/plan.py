import argparse
import asyncio
import sys
from typing import Any, Dict, Optional

from devtools.common import (
    build_plan_context,
    build_plan_payload,
    difficulty_average,
    extract_steps_from_plan_context,
    gather_exercise_ids,
    load_avg_steps_per_day,
    require_dev_environment,
    summarize_step_counts,
)


def _load_active_plan_context(user_id: int) -> Dict[str, Any]:
    from app.db import SessionLocal
    from devtools.common import load_active_plan, load_plan_with_steps, load_user

    with SessionLocal() as db:
        user = load_user(db, user_id)
        if not user:
            raise ValueError(f"user_id {user_id} not found")
        active_plan = load_active_plan(db, user_id)
        if not active_plan:
            raise ValueError("active plan not found")
        plan = load_plan_with_steps(db, active_plan.id)
        if not plan:
            raise ValueError("active plan not found")
    return {
        "user": user,
        "plan": plan,
        "plan_context": build_plan_context(plan),
    }


def _inspect(user_id: int) -> None:
    data = _load_active_plan_context(user_id)
    plan = data["plan"]
    plan_context = data["plan_context"]

    days, steps = summarize_step_counts(plan_context)
    exercise_ids = sorted(set(gather_exercise_ids(plan_context)))

    print("Plan Snapshot Inspection")
    print(f"  user_id: {user_id}")
    print(f"  plan_id: {plan.id}")
    print(f"  adaptation_version: {plan.adaptation_version}")
    print(f"  number_of_days: {days}")
    print(f"  number_of_steps: {steps}")
    print(f"  exercise_ids: {exercise_ids}")


async def _call_plan_agent(payload: Dict[str, Any]) -> Dict[str, Any]:
    from app.ai_plans import plan_agent

    return await plan_agent(payload)


def _format_metric(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    return f"{value:.2f}"


def _dry_run_adaptation(user_id: int, adaptation_type: str) -> None:
    data = _load_active_plan_context(user_id)
    user = data["user"]
    plan = data["plan"]
    plan_context = data["plan_context"]

    payload = build_plan_payload(
        current_state="ADAPTATION_FLOW",
        plan_context=plan_context,
        adaptation_type=adaptation_type,
        duration_days=len(plan.days) or 21,
        focus="Mixed",
        load=user.current_load,
        goal=plan.goal_description or "burnout_recovery",
    )

    response = asyncio.run(_call_plan_agent(payload))

    reply_text = response.get("reply_text")
    transition_signal = response.get("transition_signal")
    generated_plan_object = response.get("generated_plan_object")

    after_context = plan_context
    if generated_plan_object:
        after_context = generated_plan_object

    before_steps = extract_steps_from_plan_context(plan_context)
    after_steps = extract_steps_from_plan_context(after_context)

    before_days, before_total_steps = summarize_step_counts(plan_context)
    after_days, after_total_steps = summarize_step_counts(after_context)

    load_before = load_avg_steps_per_day(plan_context)
    load_after = load_avg_steps_per_day(after_context)

    diff_exercise_ids = sorted(
        set(gather_exercise_ids(after_context)) - set(gather_exercise_ids(plan_context))
    )

    difficulty_before = difficulty_average(before_steps)
    difficulty_after = difficulty_average(after_steps)

    print("Plan Adaptation Dry Run")
    print(f"  user_id: {user_id}")
    print(f"  adaptation_type: {adaptation_type}")
    print(f"  reply_text: {reply_text}")
    print(f"  transition_signal: {transition_signal}")
    print("  diff_summary:")
    print(f"    steps: {before_total_steps} -> {after_total_steps}")
    print(f"    days: {before_days} -> {after_days}")
    print(
        "    load_avg_steps_per_day: "
        f"{_format_metric(load_before)} -> {_format_metric(load_after)}"
    )
    print(
        "    difficulty_avg: "
        f"{_format_metric(difficulty_before)} -> {_format_metric(difficulty_after)}"
    )
    print(f"    new_exercise_ids: {diff_exercise_ids}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Plan devtools")
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect = subparsers.add_parser("inspect", help="Inspect active plan snapshot")
    inspect.add_argument("--user-id", type=int, required=True)

    dry_run = subparsers.add_parser(
        "dry-run-adaptation",
        help="Dry-run a plan adaptation via Plan Agent",
    )
    dry_run.add_argument("--user-id", type=int, required=True)
    dry_run.add_argument("--adaptation-type", required=True)

    return parser


def main() -> None:
    try:
        require_dev_environment()
        parser = build_parser()
        args = parser.parse_args()

        if args.command == "inspect":
            _inspect(args.user_id)
        elif args.command == "dry-run-adaptation":
            _dry_run_adaptation(args.user_id, args.adaptation_type)
        else:
            parser.error("Unknown command")
    except Exception as exc:  # noqa: BLE001 - CLI entrypoint
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()

import argparse
import asyncio
import sys
from typing import Any, Dict

from devtools.common import (
    build_plan_context,
    build_plan_payload,
    require_dev_environment,
    summarize_step_counts,
)


def _set_state(db: Any, user: Any, state: str) -> None:
    user.current_state = state
    db.commit()
    print(f"[SMOKE] state -> {state}")


async def _call_plan_agent(payload: Dict[str, Any]) -> Dict[str, Any]:
    from app.ai_plans import plan_agent

    return await plan_agent(payload)


def _persist_generated_plan(db: Any, user: Any, plan_payload: Dict[str, Any]) -> None:
    from app.orchestrator import _persist_generated_plan

    _persist_generated_plan(db, user, plan_payload)
    db.commit()


def _generate_plan(db: Any, user: Any) -> None:
    payload = build_plan_payload(
        current_state="PLAN_FLOW:FINALIZATION",
        plan_context=None,
        adaptation_type=None,
        duration_days=21,
        focus="Somatic",
        load=user.current_load,
        execution_policy=user.execution_policy,
        goal="burnout_recovery",
    )
    response = asyncio.run(_call_plan_agent(payload))
    generated_plan = response.get("generated_plan_object")
    if not generated_plan:
        raise RuntimeError("plan agent did not return generated_plan_object")
    _persist_generated_plan(db, user, generated_plan)
    print("[SMOKE] plan generated")


def _apply_structural_adaptation(db: Any, user: Any, adaptation_type: str) -> None:
    from devtools.common import load_active_plan, load_plan_with_steps

    active_plan = load_active_plan(db, user.id)
    if not active_plan:
        raise RuntimeError("active plan not found")
    plan = load_plan_with_steps(db, active_plan.id)
    if not plan:
        raise RuntimeError("active plan not found")
    plan_context = build_plan_context(plan)

    payload = build_plan_payload(
        current_state="ADAPTATION_FLOW",
        plan_context=plan_context,
        adaptation_type=adaptation_type,
        duration_days=len(plan.days) or 21,
        focus="Mixed",
        load=user.current_load,
        execution_policy=user.execution_policy,
        goal=plan.goal_description or "burnout_recovery",
    )
    response = asyncio.run(_call_plan_agent(payload))
    generated_plan = response.get("generated_plan_object")
    if not generated_plan:
        raise RuntimeError("adaptation did not return generated_plan_object")
    _persist_generated_plan(db, user, generated_plan)
    print(f"[SMOKE] adaptation applied: {adaptation_type}")


def _apply_execution_adaptation(db: Any, user: Any, adaptation_type: str) -> None:
    from app.plan_adaptations import apply_plan_adaptation
    from devtools.common import load_active_plan

    active_plan = load_active_plan(db, user.id)
    if not active_plan:
        raise RuntimeError("active plan not found")
    apply_plan_adaptation(db, active_plan.id, {"adaptation_type": adaptation_type})
    db.commit()
    print(f"[SMOKE] execution adaptation applied: {adaptation_type}")


def _print_final_summary(db: Any, user: Any) -> None:
    from devtools.common import load_active_plan, load_plan_with_steps

    active_plan = load_active_plan(db, user.id)
    if not active_plan:
        raise RuntimeError("active plan not found")
    plan = load_plan_with_steps(db, active_plan.id)
    plan_context = build_plan_context(plan)
    days, steps = summarize_step_counts(plan_context)

    print("[SMOKE] Final state summary")
    print(f"  user_id: {user.id}")
    print(f"  current_state: {user.current_state}")
    print(f"  plan_id: {plan.id}")
    print(f"  plan_status: {plan.status}")
    print(f"  execution_policy: {plan.execution_policy}")
    print(f"  adaptation_version: {plan.adaptation_version}")
    print(f"  days: {days}")
    print(f"  steps: {steps}")


def main() -> None:
    try:
        require_dev_environment()
        parser = argparse.ArgumentParser(description="Run FSM + plan smoke flow")
        parser.add_argument("--user-id", type=int, required=True)
        args = parser.parse_args()

        from app.db import SessionLocal
        from devtools.common import load_user

        with SessionLocal() as db:
            user = load_user(db, args.user_id)
            if not user:
                raise RuntimeError(f"user_id {args.user_id} not found")

            _set_state(db, user, "PLAN_FLOW:DATA_COLLECTION")
            _set_state(db, user, "PLAN_FLOW:FINALIZATION")
            _generate_plan(db, user)

            _set_state(db, user, "ACTIVE_CONFIRMATION")
            _set_state(db, user, "ACTIVE")

            _set_state(db, user, "ADAPTATION_FLOW")
            _apply_structural_adaptation(db, user, "REDUCE_DAILY_LOAD")

            _set_state(db, user, "ADAPTATION_FLOW")
            _apply_execution_adaptation(db, user, "pause")
            _set_state(db, user, "ACTIVE_PAUSED")

            _set_state(db, user, "ADAPTATION_FLOW")
            _apply_execution_adaptation(db, user, "resume")
            _set_state(db, user, "ACTIVE")

            _print_final_summary(db, user)
    except Exception as exc:  # noqa: BLE001 - CLI entrypoint
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()

import os
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple


def require_dev_environment() -> None:
    env_value = os.getenv("ENV")
    if env_value != "dev":
        raise RuntimeError(
            f"devtools are only available when ENV=dev (current ENV={env_value!r})"
        )


def load_user(db: Any, user_id: int) -> Any:
    from app.db import User

    return db.query(User).filter(User.id == user_id).first()


def load_active_plan(db: Any, user_id: int) -> Any:
    from app.db import AIPlan

    return (
        db.query(AIPlan)
        .filter(AIPlan.user_id == user_id, AIPlan.status == "active")
        .order_by(AIPlan.created_at.desc())
        .first()
    )


def load_plan_with_steps(db: Any, plan_id: int) -> Any:
    from sqlalchemy.orm import selectinload

    from app.db import AIPlan, AIPlanDay

    return (
        db.query(AIPlan)
        .options(selectinload(AIPlan.days).selectinload(AIPlanDay.steps))
        .filter(AIPlan.id == plan_id)
        .first()
    )


@dataclass(frozen=True)
class StepSnapshot:
    exercise_id: Optional[str]
    difficulty: Optional[str]
    time_slot: Optional[str]


def build_plan_context(plan: Any) -> Dict[str, Any]:
    schedule: List[Dict[str, Any]] = []
    for day in sorted(plan.days, key=lambda item: item.day_number):
        steps: List[Dict[str, Any]] = []
        for step in sorted(day.steps, key=lambda item: item.order_in_day):
            step_type = step.step_type.value if hasattr(step.step_type, "value") else str(step.step_type)
            difficulty = step.difficulty.value if hasattr(step.difficulty, "value") else str(step.difficulty)
            steps.append(
                {
                    "exercise_id": step.exercise_id,
                    "title": step.title,
                    "description": step.description,
                    "step_type": step_type,
                    "difficulty": difficulty,
                    "time_slot": step.time_slot,
                }
            )
        schedule.append(
            {
                "day_number": day.day_number,
                "focus_theme": day.focus_theme,
                "steps": steps,
            }
        )
    module_id = plan.module_id.value if hasattr(plan.module_id, "value") else str(plan.module_id)
    return {
        "title": plan.title,
        "module_id": module_id,
        "reasoning": plan.goal_description or "",
        "duration_days": len(plan.days),
        "schedule": schedule,
        "milestones": [],
        "plan_id": plan.id,
        "adaptation_version": plan.adaptation_version,
    }


def extract_steps_from_plan_context(plan_context: Dict[str, Any]) -> List[StepSnapshot]:
    steps: List[StepSnapshot] = []
    for day in plan_context.get("schedule") or []:
        for step in day.get("steps") or []:
            steps.append(
                StepSnapshot(
                    exercise_id=step.get("exercise_id"),
                    difficulty=step.get("difficulty"),
                    time_slot=step.get("time_slot"),
                )
            )
    return steps


def summarize_step_counts(plan_context: Dict[str, Any]) -> Tuple[int, int]:
    days = plan_context.get("schedule") or []
    step_total = sum(len(day.get("steps") or []) for day in days)
    return len(days), step_total


def gather_exercise_ids(plan_context: Dict[str, Any]) -> List[str]:
    ids: List[str] = []
    for day in plan_context.get("schedule") or []:
        for step in day.get("steps") or []:
            exercise_id = step.get("exercise_id")
            if exercise_id:
                ids.append(exercise_id)
    return ids


def difficulty_average(steps: Iterable[StepSnapshot]) -> Optional[float]:
    mapping = {"easy": 1, "medium": 2, "hard": 3}
    values: List[int] = []
    for step in steps:
        if not step.difficulty:
            continue
        key = str(step.difficulty).lower()
        if key in mapping:
            values.append(mapping[key])
    if not values:
        return None
    return sum(values) / len(values)


def load_avg_steps_per_day(plan_context: Dict[str, Any]) -> Optional[float]:
    days, steps = summarize_step_counts(plan_context)
    if days <= 0:
        return None
    return steps / days


def build_plan_payload(
    *,
    current_state: str,
    plan_context: Optional[Dict[str, Any]],
    adaptation_type: Optional[str],
    duration_days: int,
    focus: str,
    load: str,
    goal: str,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "contract_version": "v1",
        "current_state": current_state,
        "plan_parameters": {
            "duration": "STANDARD" if duration_days >= 21 else "SHORT",
            "duration_days": duration_days,
            "focus": focus,
            "load": load,
        },
        "user_policy": {
            "load": load,
            "duration_days": duration_days,
        },
        "plan_context": plan_context,
        "previous_plan_context": None,
        "telemetry": None,
        "functional_snapshot": None,
        "goal": goal,
    }
    if adaptation_type:
        payload["adaptation_request"] = {"type": adaptation_type}
        payload["adaptation_type"] = adaptation_type
    return payload

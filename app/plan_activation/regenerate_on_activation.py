"""Regenerate plan steps based on activation time."""
# ⚠️ FROZEN LOGIC (activation anchor / finalization)
#
# Цей код свідомо ЗАМОРОЖЕНИЙ.
#
# Чому:
# - Логіка прив’язки Day 1 до дати активації має багато UX- та бізнес-edge-кейсів
#   (частково пропущені слоти, очікування користувача vs календар, preview ≠ activation).
# - Повна регенерація плану на активації виявилась надто складною для поточного етапу
#   і ламала стабільність фіналізації.
# - Поточне рішення — мінімальне і безпечне: якщо Day 1 слот уже в минулому,
#   план стартує з наступного дня, без мутації draft.
#
# Що зараз гарантується:
# - Draft preview НЕ змінюється після підтвердження
# - Finalization детермінована
# - Нема регенерації, тільки зсув anchor_date (today / tomorrow)
#
# Як РОЗМОРОЖУВАТИ:
# 1. Написати інтеграційні тести:
#    - single slot (EVENING only)
#    - multi-slot (MORNING + EVENING)
#    - activation before first slot / between slots / after all slots
# 2. Зафіксувати UX-очікування (що користувач бачить у preview vs коли реально стартує)
# 3. Вирішити: 
#    - або повна регенерація плану при activation
#    - або стабільний draft + окремий activation-view
# 4. Тільки після цього міняти цю логіку.
#
# До того моменту — НЕ ЧІПАТИ.
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone

import pytz

from app.db import PlanDraftRecord
from app.plan_drafts.draft_builder import ContentLibrary
from app.plan_drafts.plan_types import Duration, Focus, Load, PlanParameters, UserPolicy
from app.plan_drafts.rules import (
    calculate_category_distribution,
    get_daily_slot_structure,
    get_difficulty_for_week,
    get_time_slot_for_slot_type,
    get_total_days,
    select_exercise_with_fallback,
)
from app.plan_drafts.service import CONTENT_LIBRARY_PATH
from app.time_slots import normalize_time_slot


@dataclass(frozen=True)
class RegeneratedPlanStep:
    day_number: int
    exercise_id: str
    slot_type: str
    time_slot: str
    category: str
    difficulty: int


@dataclass(frozen=True)
class RegeneratedPlan:
    total_days: int
    steps: list[RegeneratedPlanStep]
    plan_start_utc: datetime

    @property
    def total_steps(self) -> int:
        return len(self.steps)


_FIXED_TIME_SLOTS: dict[str, time] = {
    "MORNING": time(hour=9, minute=30),
    "DAY": time(hour=14, minute=0),
    "EVENING": time(hour=21, minute=0),
}


def _normalize_timezone(name: str | None) -> pytz.BaseTzInfo:
    try:
        return pytz.timezone(name or "UTC")
    except pytz.UnknownTimeZoneError:
        return pytz.UTC


def _localize_slot_datetime(
    *,
    base_date: datetime,
    slot_time: time,
    tz: pytz.BaseTzInfo,
) -> datetime:
    naive = datetime.combine(base_date.date(), slot_time)
    try:
        return tz.localize(naive)
    except pytz.NonExistentTimeError:
        return tz.localize(naive + timedelta(hours=1))
    except pytz.AmbiguousTimeError:
        return tz.localize(naive, is_dst=False)


def _infer_preferred_slots(draft: PlanDraftRecord) -> list[str]:
    slots: list[str] = []
    seen: set[str] = set()
    for step in draft.steps or []:
        if not step.time_slot:
            continue
        normalized = normalize_time_slot(step.time_slot)
        if normalized in seen:
            continue
        seen.add(normalized)
        slots.append(normalized)
    return slots


def _is_in_cooldown(exercise_last_used: dict[str, int], exercise_id: str, day: int, cooldown_days: int) -> bool:
    if exercise_id not in exercise_last_used:
        return False
    last_used = exercise_last_used[exercise_id]
    days_since = day - last_used
    return days_since <= cooldown_days


def _pick_category_for_slot(distribution: dict[str, int], focus: Focus) -> str:
    available = {k: v for k, v in distribution.items() if v > 0}
    if not available:
        return focus.value
    if focus.value in available:
        return focus.value
    return sorted(available.items(), key=lambda item: (-item[1], item[0]))[0][0]


def regenerate_plan_for_activation(
    *,
    draft: PlanDraftRecord,
    activation_time_utc: datetime,
    user_timezone: str,
) -> RegeneratedPlan:
    if activation_time_utc.tzinfo is None:
        raise ValueError("activation_time_not_timezone_aware")

    tz = _normalize_timezone(user_timezone)
    activation_time_utc = activation_time_utc.astimezone(timezone.utc)
    activation_local = activation_time_utc.astimezone(tz)

    preferred_slots = _infer_preferred_slots(draft)
    if not preferred_slots:
        preferred_slots = list(_FIXED_TIME_SLOTS.keys())

    available_today: list[str] = []
    for slot in preferred_slots:
        slot_time = _FIXED_TIME_SLOTS.get(slot)
        if not slot_time:
            continue
        slot_dt = _localize_slot_datetime(
            base_date=activation_local,
            slot_time=slot_time,
            tz=tz,
        )
        if slot_dt > activation_local:
            available_today.append(slot)

    day1_is_today = bool(available_today)
    day1_date_utc = activation_time_utc if day1_is_today else activation_time_utc + timedelta(days=1)
    try:
        load_enum = Load(draft.load)
        duration_enum = Duration(draft.duration)
        focus_enum = Focus(draft.focus)
    except ValueError as exc:
        raise ValueError("draft_parameters_invalid") from exc
    day1_slot_structure = get_daily_slot_structure(load_enum)
    slots_per_day = len(day1_slot_structure)
    available_today_sorted = sorted(
        available_today,
        key=lambda slot: _FIXED_TIME_SLOTS.get(slot, time.max),
    )
    if day1_is_today:
        day1_time_slots = available_today_sorted[:slots_per_day]
        day1_slot_types = day1_slot_structure[: len(day1_time_slots)]
    else:
        day1_time_slots = []
        day1_slot_types = day1_slot_structure

    total_days = get_total_days(duration_enum)
    total_slots = total_days * slots_per_day
    category_distribution = calculate_category_distribution(focus_enum, total_slots)

    try:
        library = ContentLibrary(str(CONTENT_LIBRARY_PATH))
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise ValueError("content_library_unavailable") from exc
    available_exercises = library.get_active_exercises()
    if not available_exercises:
        raise ValueError("content_library_empty")

    params = PlanParameters(
        duration=duration_enum,
        focus=focus_enum,
        load=load_enum,
        user_policy=UserPolicy(preferred_time_slots=list(preferred_slots)),
    )

    exercise_last_used: dict[str, int] = {}
    steps: list[RegeneratedPlanStep] = []
    day_number = 1
    seed_suffix = str(draft.id)

    while len(steps) < total_slots:
        week_number = ((day_number - 1) // 7) + 1
        max_difficulty = get_difficulty_for_week(week_number, params.duration)
        if day_number == 1 and day1_is_today:
            slot_types = day1_slot_types
        else:
            slot_types = day1_slot_structure

        for slot_index, slot_type in enumerate(slot_types):
            if len(steps) >= total_slots:
                break
            category = _pick_category_for_slot(category_distribution, params.focus)
            available_exercises_now = [
                e
                for e in available_exercises
                if not _is_in_cooldown(exercise_last_used, e.id, day_number, e.cooldown_days)
            ]
            exercise = select_exercise_with_fallback(
                available_exercises_now,
                preferred_category=category,
                slot_type=slot_type,
                max_difficulty=max_difficulty,
                params=params,
                seed_suffix=seed_suffix,
            )
            if not exercise:
                exercise = select_exercise_with_fallback(
                    available_exercises,
                    preferred_category=category,
                    slot_type=slot_type,
                    max_difficulty=max_difficulty,
                    params=params,
                    seed_suffix=seed_suffix,
                )
            if not exercise:
                raise ValueError("exercise_selection_failed")

            if day_number == 1 and day1_is_today:
                time_slot = day1_time_slots[slot_index]
            else:
                time_slot = get_time_slot_for_slot_type(
                    slot_type,
                    params.user_policy.preferred_time_slots if params.user_policy else None,
                ).value

            steps.append(
                RegeneratedPlanStep(
                    day_number=day_number,
                    exercise_id=exercise.id,
                    slot_type=slot_type.value,
                    time_slot=time_slot,
                    category=exercise.category,
                    difficulty=exercise.difficulty,
                )
            )

            if category in category_distribution:
                category_distribution[category] -= 1
            exercise_last_used[exercise.id] = day_number

        day_number += 1

    regenerated_total_days = max(step.day_number for step in steps) if steps else 0
    return RegeneratedPlan(
        total_days=regenerated_total_days,
        steps=steps,
        plan_start_utc=day1_date_utc,
    )

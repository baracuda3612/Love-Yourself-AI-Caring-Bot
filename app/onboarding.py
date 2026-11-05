"""Fixed Telegram setup flow. Call mutations under the access-checked user lock."""

from dataclasses import dataclass
from hashlib import sha256
from html import escape
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.active_days import ACTIVE_DAYS_DEFAULT, resolve_active_days
from app.db import AIPlan, UserProfile
from app.lifecycle import (
    CurrentMode,
    activate_plan,
    derive_current_mode,
    ensure_onboarding_progress,
    find_lifecycle_operation,
    get_current_plan,
    recover_current_plan_activation,
)
from app.time_slots import TimeSlotError, canonicalize_hhmm

INTRO = (
    "LY Workday допомагає зробити коротку перерву серед робочого дня.\n"
    "У вибраний тобою час бот запропонує одну просту дію, щоб ненадовго перемкнути увагу з роботи на себе.\n"
    "Тридцять секунд, щоб перервати робочий потік і відчути свій стан."
)
TIME_PROMPT = (
    "Коли отримувати нагадування про перерву?\n"
    "Рекомендуємо другу половину твого робочого дня.\n"
    "Введи час у форматі <b>ГГ:ХХ</b>, наприклад <b>14:30</b>."
)
SETUP_STAGES = {"START", "TIME", "CONFIRM", "SETTINGS", "EDIT_TIME", "EDIT_DAYS", "EDIT_ZONE"}
FIRST_PLAN_OPERATION = "onboarding:first-plan"
FALLBACK = "Продовжимо налаштування розкладу."
DAYS = dict(zip(
    ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"),
    ("понеділок", "вівторок", "середа", "четвер", "п’ятниця", "субота", "неділя"),
))
OFFSETS = {
    offset: f"Etc/GMT{-offset:+d}" if offset else "Etc/UTC"
    for offset in range(-12, 15)
}


@dataclass(frozen=True)
class SetupScreen:
    text: str
    keyboard: InlineKeyboardMarkup | None = None


def setup_progress(db, user):
    if derive_current_mode(db, user.id) is not CurrentMode.ONBOARDING:
        return None
    progress = ensure_onboarding_progress(db, user.id)
    allowed = SETUP_STAGES | {f"STOPPED_{stage}" for stage in SETUP_STAGES}
    if progress.stage not in allowed:
        progress.stage = "START"
        progress.draft_days = None
        progress.revision += 1
    stage = progress.stage.removeprefix("STOPPED_")
    if stage not in {"START", "TIME"}:
        try:
            _accepted_time(user)
        except (TimeSlotError, AttributeError):
            progress.stage = "TIME"
            progress.draft_days = None
            progress.revision += 1
    if progress.stage.removeprefix("STOPPED_") == "EDIT_DAYS" and progress.draft_days is None:
        days = resolve_active_days(user.profile)
        progress.draft_days = sum(1 << i for i, day in enumerate(DAYS) if day in days)
    return progress


def _zone_available(name):
    if not name:
        return False
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def accept_timezone(user, value):
    """Select the deployment default or an explicit supported override."""
    old_value = user.timezone_override
    old_explicit = user.timezone_override_explicit
    user.timezone = value
    if not _zone_available(user.timezone):
        user.timezone_override = old_value
        user.timezone_override_explicit = old_explicit
        return False
    return True


def zone_label(name):
    if name in {"Europe/Kyiv", "Europe/Kiev"}:
        return "Київ"
    for offset, zone in OFFSETS.items():
        if zone == name:
            return _offset_label(offset)
    return name or "не вибрано"


def _offset_label(offset):
    return f"UTC{'−' if offset < 0 else '+'}{abs(offset):02d}:00"


def _summary(user):
    days = resolve_active_days(user.profile)
    days_text = "понеділок–п’ятниця" if days == ACTIVE_DAYS_DEFAULT else ", ".join(DAYS[d] for d in days)
    return (
        f"Час нагадування: <b>{escape(_accepted_time(user))}</b>\n"
        f"Дні: <b>{days_text}</b>\n"
        f"Часовий пояс: <b>{escape(zone_label(user.timezone))}</b>"
    )


def _accepted_time(user):
    return canonicalize_hhmm((user.profile.daily_time_slots or {}).get("DAY"))


def _confirm_action(user):
    schedule = (_accepted_time(user), resolve_active_days(user.profile), user.timezone)
    fingerprint = sha256(repr(schedule).encode()).hexdigest()[:16]
    return f"confirm_{fingerprint}"


def weekday_rows(mask, button):
    return [[button(
        f"{'✓ ' if mask & (1 << i) else ''}{label}", f"day_{i}"
    )] for i, label in enumerate(DAYS.values())]


def zone_picker(user, button, page=None):
    body = (
        f"Поточний часовий пояс: <b>{escape(zone_label(user.timezone))}</b>.\n"
        "Фіксований зсув UTC не змінюється при переведенні годинника."
    )
    if page is None:
        offset = next((n for n, zone in OFFSETS.items() if zone == user.timezone), None)
        page = (offset + 12) // 9 if offset is not None else 1
    offsets = list(OFFSETS)[9 * page:9 * (page + 1)]
    rows = [[button(_offset_label(n), f"zone_{n}") for n in offsets[i:i + 3]]
            for i in range(0, 9, 3)]
    arrows = []
    if page > 0:
        arrows.append(button("‹", f"page_{page - 1}"))
    if page < 2:
        arrows.append(button("›", f"page_{page + 1}"))
    rows.append(arrows)
    rows.append([button("Стандартний часовий пояс", "default_zone")])
    return body, rows


def render(user, progress, *, hint="", page=None):
    def button(label, action):
        return InlineKeyboardButton(
            text=label, callback_data=f"setup:{progress.revision}:{action}"
        )

    cancel = [button("Скасувати зміну", "cancel")]
    stage = progress.stage
    if stage.startswith("STOPPED_"):
        body = "Налаштування призупинено. Розклад ще не створено."
        rows = [[button("Продовжити налаштування", "resume")]]
    elif stage == "TIME":
        body, rows = TIME_PROMPT, []
    elif stage == "EDIT_TIME":
        body = (
            f"Поточний час: <b>{_accepted_time(user)}</b>.\n"
            "Введи новий час у форматі <b>ГГ:ХХ</b>, наприклад <b>15:30</b>, і надішли повідомлення."
        )
        rows = [cancel]
    elif stage == "CONFIRM":
        body = _summary(user)
        rows = [[button("Підтвердити", _confirm_action(user)),
                 button("Змінити налаштування", "settings")]]
    elif stage == "SETTINGS":
        body = "Що змінити?"
        rows = [[button(label, action)] for label, action in (
            ("Час нагадування", "time"), ("Дні надсилання", "days"),
            ("Часовий пояс", "zone"), ("Назад до підтвердження", "cancel"),
        )]
    elif stage == "EDIT_DAYS":
        body = "Вибери дні надсилання."
        rows = weekday_rows(progress.draft_days, button)
        rows.extend([[button("Застосувати", "apply")], cancel])
    elif stage == "EDIT_ZONE":
        body, rows = zone_picker(user, button, page)
        rows.append(cancel)
    else:
        body, rows = INTRO, [[button("Налаштувати час", "start")]]
    return SetupScreen(
        f"{hint}\n\n{body}" if hint else body,
        InlineKeyboardMarkup(inline_keyboard=rows) if rows else None,
    )


def handle_text(user, progress, text):
    if progress.stage not in {"TIME", "EDIT_TIME"}:
        return render(user, progress, hint=FALLBACK)
    try:
        hhmm = canonicalize_hhmm(text)
    except TimeSlotError:
        return render(user, progress, hint="Не вдалося прочитати час.")
    if user.profile is None:
        user.profile = UserProfile()
    profile = user.profile
    profile.daily_time_slots = {**(profile.daily_time_slots or {}), "DAY": hhmm}
    if progress.stage == "TIME":
        profile.active_days = ACTIVE_DAYS_DEFAULT.copy()
    progress.stage = "CONFIRM"
    progress.revision += 1
    return render(user, progress)


def handle_action(db, user, progress, revision, action):
    """Return the next screen or the canonical activation result."""
    stale = "Ця кнопка вже неактуальна."
    if revision != progress.revision:
        return render(user, progress, hint=stale), None

    stage = progress.stage
    next_stage = stage
    if action == "resume" and stage.startswith("STOPPED_"):
        next_stage = stage.removeprefix("STOPPED_")
    elif action == "start" and stage == "START":
        next_stage = "TIME"
    elif action == "restart":
        next_stage = "START"
        progress.draft_days = None
    elif stage == "CONFIRM" and action == "settings":
        next_stage = "SETTINGS"
    elif stage == "SETTINGS" and action in {"time", "days", "zone"}:
        next_stage = {"time": "EDIT_TIME", "days": "EDIT_DAYS", "zone": "EDIT_ZONE"}[action]
        if action == "days":
            accepted = resolve_active_days(user.profile)
            progress.draft_days = sum(1 << i for i, day in enumerate(DAYS) if day in accepted)
    elif action == "cancel" and stage in {"SETTINGS", "EDIT_TIME", "EDIT_DAYS", "EDIT_ZONE"}:
        progress.draft_days = None
        next_stage = "CONFIRM"
    elif stage == "EDIT_DAYS" and action in {f"day_{i}" for i in range(7)}:
        progress.draft_days ^= 1 << int(action[-1])
    elif stage == "EDIT_DAYS" and action == "apply":
        if not progress.draft_days:
            return render(user, progress, hint="Вибери хоча б один день."), None
        user.profile.active_days = [
            day for i, day in enumerate(DAYS) if progress.draft_days & (1 << i)
        ]
        progress.draft_days = None
        next_stage = "CONFIRM"
    elif stage == "EDIT_ZONE" and action in {"page_0", "page_1", "page_2"}:
        return render(user, progress, page=int(action[-1])), None
    elif stage == "EDIT_ZONE" and action == "default_zone":
        if not accept_timezone(user, None):
            return render(user, progress, hint="Стандартний часовий пояс поки не налаштовано."), None
        next_stage = "CONFIRM"
    elif stage == "EDIT_ZONE" and action in {f"zone_{n}" for n in OFFSETS}:
        accept_timezone(user, OFFSETS[int(action.removeprefix("zone_"))])
        next_stage = "CONFIRM"
    elif stage == "CONFIRM" and action == _confirm_action(user):
        if not _zone_available(user.timezone):
            return render(user, progress, hint="Спочатку вибери часовий пояс у налаштуваннях."), None
        if db.query(AIPlan.id).filter_by(user_id=user.id).first() is not None:
            return SetupScreen("Розклад уже створено. Звернися до підтримки, якщо його не видно."), None
        return None, activate_plan(
            db, user_id=user.id, plan_type="SHORT", day_time=_accepted_time(user),
            evening_time=None, source_operation_id=FIRST_PLAN_OPERATION,
        )
    else:
        return render(user, progress, hint=stale), None

    progress.stage = next_stage
    progress.revision += 1
    return render(user, progress), None


def recover_confirmation(db, user, revision, action):
    """Retry post-commit work only for the confirmed first plan still in use."""
    progress = user.onboarding_progress
    if (
        progress is None or progress.stage != "COMPLETED"
        or progress.revision != revision or not action.startswith("confirm_")
    ):
        return None
    if user.profile is None or action != _confirm_action(user):
        return None
    receipt = find_lifecycle_operation(db, user.id, FIRST_PLAN_OPERATION)
    plan = get_current_plan(db, user.id)
    if receipt is None or plan is None or plan.id != receipt.plan_id or plan.status != "active":
        return None
    return recover_current_plan_activation(db, user_id=user.id, plan_type="SHORT")

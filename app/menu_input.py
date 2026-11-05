"""Explicit Telegram text actions. Call under the access-checked user lock."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.access import require_access
from app.db import FeedbackEvent, MenuInput, UserProfile
from app.lifecycle import (
    LifecycleResult, change_delivery_time, get_current_plan, record_evening_time_preference,
    switch_plan_format,
)
from app.time_slots import TimeSlotError, canonicalize_hhmm


PROMPT_TTL = timedelta(minutes=15)


@dataclass(frozen=True)
class InputResult:
    route: str
    hint: str
    effect: LifecycleResult | None = None


def open_prompt(db, user, notice_id, action, *, floor, plan=None):
    enrollment_id = require_access(db, user.id).id
    row = db.get(MenuInput, user.id)
    if row is None:
        row = MenuInput(user_id=user.id, last_message_id=0)
        db.add(row)
    row.action = action
    row.draft_days = None
    row.nonce = uuid4().hex
    row.expires_at = datetime.now(timezone.utc) + PROMPT_TTL
    row.notice_id = notice_id
    row.enrollment_id = enrollment_id
    row.target_plan_id = plan.id if plan is not None else None
    row.target_version = plan.version if plan is not None else None
    row.last_message_id = max(row.last_message_id, floor)
    return row


def handle_text(db, user, notice_id, message_id, text, *, sent_at=None):
    row = db.get(MenuInput, user.id)
    if row is None:
        return None
    if message_id <= row.last_message_id:
        return InputResult("root", "Це повідомлення вже оброблено або надіслано раніше.")
    if row.action is None or row.action.startswith("done_"):
        return None

    started = (row.expires_at - PROMPT_TTL).replace(microsecond=0)
    if sent_at is not None and sent_at < started:
        return InputResult("root", "Це повідомлення надіслано до початку введення.")
    action = row.action
    row.last_message_id = message_id
    if row.expires_at <= datetime.now(timezone.utc) or row.notice_id != notice_id or row.enrollment_id != require_access(db, user.id).id:
        row.action = None
        return InputResult("root", "Цей запит уже неактуальний. Відкрий потрібний пункт меню ще раз.")
    if row.action in {"calendar_days", "calendar_zone", "profile_zone", "setup_zone", "confirm_end", "confirm_short", "confirm_medium", "confirm_followup"}:
        return InputResult("input_pending", "Скористайся кнопками під повідомленням.")
    if not text.strip():
        return InputResult("input_pending", "Надішли текстове повідомлення.")

    if action == "feedback":
        db.add(FeedbackEvent(
            user_id=user.id, source="product_feedback",
            source_operation_id=f"telegram:feedback:{user.tg_id}:{message_id}",
            value="submitted", extracted_text=text, telegram_message_id=message_id,
        ))
        row.action = None
        return InputResult("Про проєкт", "Відгук збережено.")

    if action == "name":
        name = text.strip()
        if len(name) > 80:
            return InputResult("input_pending", "Введи ім’я до 80 символів.")
        if user.profile is None:
            user.profile = UserProfile()
        user.profile.name_preference = name
        row.action = None
        return InputResult("Мої дані", "Ім’я збережено.")

    if action in {"day", "evening", "switch_evening"}:
        plan = get_current_plan(db, user.id, lock=True)
        if plan is None or (plan.id, plan.version) != (row.target_plan_id, row.target_version):
            row.action = None
            return InputResult("Мій розклад", "Розклад змінився. Відкрий редагування часу ще раз.")
        try:
            hhmm = canonicalize_hhmm(text)
        except TimeSlotError:
            return InputResult(
                "input_pending",
                "Не вдалося прочитати час вечірнього нагадування."
                if action in {"evening", "switch_evening"}
                else "Введи час у форматі ГГ:ХХ, наприклад 14:30.",
            )
        source = f"telegram:time:{user.tg_id}:{message_id}"
        if action == "switch_evening":
            pending = format_operation(plan.id, plan.version, "MEDIUM")
            record_evening_time_preference(
                db, user_id=user.id, hhmm=hhmm, context="switch",
                pending_source_operation_id=pending, source_operation_id=source,
            )
            effect = switch_plan_format(
                db, user_id=user.id, target_plan_type="MEDIUM", source_operation_id=pending,
            )
        else:
            effect = change_delivery_time(
                db, user_id=user.id, slot=action, hhmm=hhmm, source_operation_id=source,
            )
        row.action = None
        return InputResult("Мій розклад", "", effect)

    row.action = None
    return InputResult("root", "Ця дія вже неактуальна.")


def format_operation(plan_id, plan_version, target):
    return f"telegram:format:{plan_id}:{plan_version}:{target}"


def calendar_screen(user, row, *, hint="", page=None):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    from app.onboarding import SetupScreen, weekday_rows, zone_picker

    def button(label, action):
        return InlineKeyboardButton(text=label, callback_data=f"menu:cal:{row.nonce}:{action}")

    if row.action == "calendar_days":
        body = "Вибери дні надсилання."
        rows = weekday_rows(row.draft_days, button)
        rows.append([button("Застосувати", "apply")])
    else:
        body, rows = zone_picker(user, button, page)
    if row.action in {"calendar_days", "calendar_zone"}:
        body += "\n\nМайбутні нагадування за новим розкладом почнуться з найближчого вибраного дня після сьогодні."
    rows.append([button("Скасувати зміну", "cancel")])
    return SetupScreen(f"{hint}\n\n{body}" if hint else body, InlineKeyboardMarkup(inline_keyboard=rows))


def calendar_action(db, user, plan, row, notice_id, route):
    from app.lifecycle import change_schedule_calendar
    from app.onboarding import DAYS, OFFSETS, accept_timezone, setup_progress

    _, _, tail = route.partition(":")
    nonce, _, action = tail.partition(":")
    if (
        row is None or row.action not in {"calendar_days", "calendar_zone", "profile_zone", "setup_zone"}
        or nonce != row.nonce or row.notice_id != notice_id
        or row.enrollment_id != require_access(db, user.id).id
        or row.expires_at <= datetime.now(timezone.utc)
    ):
        return None, None, "Ця кнопка вже неактуальна."
    if row.action == "setup_zone":
        progress = setup_progress(db, user)
        if progress is None or progress.revision != row.target_version:
            return None, None, "Налаштування вже змінилися."
    elif plan is None or (plan.id, plan.version) != (row.target_plan_id, row.target_version):
        return None, None, "Розклад уже змінився."
    if action == "cancel":
        was_setup = row.action == "setup_zone"
        row.action = None
        if was_setup:
            from app.onboarding import render

            return render(user, progress), None, ""
        return None, None, ""

    days = None
    zone = None
    if row.action == "calendar_days":
        if action in {f"day_{i}" for i in range(7)}:
            row.draft_days ^= 1 << int(action[-1])
            row.nonce = uuid4().hex
            return calendar_screen(user, row), None, ""
        if action != "apply":
            return calendar_screen(user, row), None, ""
        if not row.draft_days:
            return calendar_screen(user, row, hint="Вибери хоча б один день."), None, ""
        days = [day for i, day in enumerate(DAYS) if row.draft_days & (1 << i)]
    else:
        if action in {"page_0", "page_1", "page_2"}:
            return calendar_screen(user, row, page=int(action[-1])), None, ""
        if action in {f"zone_{n}" for n in OFFSETS}:
            zone = OFFSETS[int(action.removeprefix("zone_"))]
        elif action != "default_zone":
            return calendar_screen(user, row), None, ""

    if row.action == "setup_zone":
        if not accept_timezone(user, zone):
            return calendar_screen(user, row, hint="Стандартний часовий пояс поки не налаштовано."), None, ""
        progress.revision += 1
        row.action = None
        row.draft_days = None
        from app.onboarding import render

        return render(user, progress), None, ""

    result = change_schedule_calendar(
        db, user_id=user.id, plan_id=plan.id, plan_version=plan.version,
        source_operation_id=f"telegram:calendar:{row.nonce}",
        active_days=days, timezone_override=zone,
    )
    row.action = None
    row.draft_days = None
    return None, result, ""


def pending_screen(user, row, *, in_setup):
    from app import user_menu

    if row.action in {"calendar_days", "calendar_zone", "profile_zone", "setup_zone"}:
        return calendar_screen(user, row)
    if row.action == "feedback":
        body = user_menu.feedback_text()
    elif row.action == "name":
        body = "Як до тебе звертатися? Надішли ім’я одним повідомленням."
    elif row.action in {"evening", "switch_evening"}:
        body = "Введи час вечірнього нагадування у форматі ГГ:ХХ, наприклад 18:30, і надішли повідомлення."
    elif row.action == "day":
        body = "Введи час у форматі ГГ:ХХ, наприклад 14:30, і надішли повідомлення."
    else:
        label = "Підтвердити завершення" if row.action == "confirm_end" else "Підтвердити зміну"
        return user_menu.navigation(
            "Підтвердити обрану дію?", in_setup=in_setup,
            actions=((label, f"confirm:{row.nonce}"), ("Скасувати", f"input_cancel:{row.nonce}")),
        )
    return user_menu.navigation(
        body, in_setup=in_setup, actions=(("Скасувати", f"input_cancel:{row.nonce}"),),
    )

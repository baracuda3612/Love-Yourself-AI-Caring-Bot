"""Telegram menu presentation. Access decisions stay with the caller."""

import json
import re
from pathlib import Path

from datetime import datetime, timezone
from html import escape
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from app.active_days import resolve_active_days
from app.db import AIPlanDay, AIPlanStep
from app.onboarding import DAYS, SetupScreen, zone_label
from app.time_slots import resolve_daily_time_slots

DATA = "Мої дані"
ABOUT = "Про проєкт"
SUPPORT = "Підтримка"
SCHEDULE = "Мій розклад"
MENU_TEXTS = frozenset({DATA, ABOUT, SUPPORT, SCHEDULE})


def keyboard(*, schedule, own_data):
    rows = [[DATA, ABOUT], [SUPPORT]] if own_data else [[ABOUT, SUPPORT]]
    if schedule:
        rows.insert(0, [SCHEDULE])
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=label) for label in row] for row in rows],
        resize_keyboard=True,
        is_persistent=True,
    )


def navigation(text, *, in_setup, actions=()):
    label, route = ("Повернутися до налаштувань", "setup") if in_setup else ("Назад до меню", "root")
    rows = [
        [InlineKeyboardButton(text=title, callback_data=f"menu:{key}")]
        for title, key in (*actions, (label, route))
    ]
    return SetupScreen(text, InlineKeyboardMarkup(inline_keyboard=rows))


def support(in_setup):
    return navigation(f"<b>Підтримка</b>\n\n{_article_body('support')}", in_setup=in_setup)


HELP_SECTIONS = {
    "overview": ("Опис проєкту", ("about", "exercises", "pause", "assistant", "boundaries")),
    "reminders": ("Як приходять нагадування", ("schedule", "rhythms", "completion", "manage")),
    "faq": ("Часті запитання", ("how", "why", "missed", "stop")),
    "confidentiality": ("Конфіденційність", ("retention", "visibility", "deletion", "notice")),
}
HELP_ARTICLES = json.loads(Path(__file__).with_name("menu_articles_uk.json").read_text(encoding="utf-8"))


def _article_body(key):
    body = HELP_ARTICLES[key]["body"]
    # Expose only the approved passages whose referenced capabilities are live.
    if key == "how":
        body = body.split("\n\n", 1)[0]  # The last paragraph needs B6.
    return re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", escape(body))


def feedback_text():
    return _article_body("feedback")


def help_screen(route, *, in_setup, notice_access, feedback_access=False):
    if route == ABOUT:
        return navigation(
            "<b>Про проєкт</b>", in_setup=in_setup,
            actions=tuple((title, f"help:{section}") for section, (title, _) in HELP_SECTIONS.items())
            + ((("Залишити відгук", "feedback"),) if feedback_access else ()),
        )

    key = route.removeprefix("help:")
    if key in HELP_SECTIONS:
        title, articles = HELP_SECTIONS[key]
        return navigation(
            f"<b>{escape(title)}</b>", in_setup=in_setup,
            actions=tuple((HELP_ARTICLES[item]["title"], f"help:{item}") for item in articles)
            + (("Назад до «Про проєкт»", ABOUT),),
        )

    for section, (_, articles) in HELP_SECTIONS.items():
        if key in articles:
            title = HELP_ARTICLES[key]["title"]
            body = _article_body(key)
            actions = [("Назад до розділу", f"help:{section}")]
            if key == "notice":
                if notice_access:
                    actions.insert(0, ("Прочитати повідомлення", "help_notice"))
                else:
                    body = "Повідомлення про приватність зараз недоступне."
            return navigation(f"<b>{escape(title)}</b>\n\n{body}", in_setup=in_setup, actions=actions)
    return help_screen(ABOUT, in_setup=in_setup, notice_access=notice_access, feedback_access=feedback_access)


def schedule_text(db, user, plan, *, delivery):
    if plan is None:
        return "<b>Мій розклад</b>\n\nЗараз немає активного розкладу."
    slots = resolve_daily_time_slots(user.profile)
    times = slots["DAY"]
    if plan.total_days == 14:
        times += f" і {slots['EVENING']}"
    days = ", ".join(DAYS[day] for day in resolve_active_days(user.profile))
    status = "призупинено" if plan.status == "paused" else "активний"
    body = (
        f"<b>Мій розклад</b>\n\nСтан: {status}\nДні: {days}\n"
        f"Час: {times}\nЧасовий пояс: {escape(zone_label(user.timezone))}"
    )
    from app.plan_completion.workflow import successor_delivery_blocked

    if (
        plan.status == "paused" or not user.timezone or not delivery
        or successor_delivery_blocked(db, plan.id)
    ):
        return body
    try:
        zone = ZoneInfo(user.timezone)
    except (ValueError, ZoneInfoNotFoundError):
        return body
    next_step = (
        db.query(AIPlanStep.scheduled_for)
        .join(AIPlanDay)
        .filter(
            AIPlanDay.plan_id == plan.id,
            AIPlanStep.step_status == "pending",
            AIPlanStep.scheduled_for > datetime.now(timezone.utc),
        )
        .order_by(AIPlanStep.scheduled_for)
        .first()
    )
    if next_step is None:
        return body
    local = next_step.scheduled_for.astimezone(zone)
    return f"{body}\nНаступне заплановане нагадування: {local:%d.%m.%Y о %H:%M}"

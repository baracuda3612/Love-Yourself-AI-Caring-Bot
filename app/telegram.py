# app/telegram.py
# Версія з підтримкою чернеток, підтвердження плану і керування нагадуваннями

import json
import re
from datetime import datetime, timedelta, time as dt_time
import datetime as dtmod
import html
import traceback
from typing import List, Optional

import parsedatetime as pdt
import pytz
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from sqlalchemy import select

from app.config import settings
from app.db import (
    SessionLocal, User, Response, UsageCounter,
    UserReminder, AIPlan, AIPlanStep, UserMemoryProfile, OnboardingEvent
)
from app.ai import (
    OnboardingIntent,
    answer_user_question,
    classify_onboarding_message,
)
from app.ai_router import route_message
from app.redis_client import create_fsm_storage, create_redis_client
from app.scheduler import (
    remove_job,
    schedule_custom_reminder,
    schedule_plan_step,
)
from app.ai_plans import generate_ai_plan
from app.plan_parser import parse_plan_request
from app.plan_normalizer import normalize_plan_steps
from app.session_memory import SessionMemory

# ----------------- базові речі -----------------

bot = Bot(token=settings.BOT_TOKEN, parse_mode="HTML")
redis_client = create_redis_client()
storage = create_fsm_storage(redis_client) or MemoryStorage()
dp = Dispatcher(storage=storage)
router = Router()
dp.include_router(router)
session_memory = SessionMemory(redis_client=redis_client)


_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)
_TIME_COLON_RE = re.compile(r"(\d{1,2}):(\d{2})")
_TIME_DIGITS_RE = re.compile(r"\b(\d{3,4})\b")


RECENT_MESSAGES_LIMIT = 6


CONSENT_TEXT = (
    "Я — wellbeing-бот Love Yourself.\n"
    "Щоб працювати, я зберігаю базові дані: імʼя, налаштування, відповіді в онбордингу.\n"
    "Ці дані використовуються тільки для персоналізації досвіду.\n"
    "Натискаючи «Погоджуюсь», ти дозволяєш це зберігання."
)

TIMEZONE_CONFIRM_TEMPLATE = "Твій часовий пояс: {tz}.\nВсе ок?"

QUICK_WIN_TEXT = (
    "Давай зараз зробимо перший маленький крок 👇\n"
    "1-хвилинна вправа на заземлення:\n"
    "• сядь з прямою спиною,\n"
    "• зроби 5 повільних вдихів через ніс і видихів через рот,\n"
    "• на кожному видиху помічай, як напруга в тілі падає хоча б на 1%.\n"
    "Все. Цього вже достатньо, щоб почати."
)


class PlanStates(StatesGroup):
    waiting_new_hour = State()


class Onboarding(StatesGroup):
    waiting_consent = State()
    waiting_goal = State()
    waiting_stress = State()
    waiting_energy = State()
    waiting_position = State()
    waiting_department = State()
    waiting_style = State()
    waiting_time = State()
    waiting_tz_confirm = State()
    waiting_tz_manual = State()
    final = State()


ONBOARDING_STATE_NAMES = {
    Onboarding.waiting_consent.state,
    Onboarding.waiting_goal.state,
    Onboarding.waiting_stress.state,
    Onboarding.waiting_energy.state,
    Onboarding.waiting_position.state,
    Onboarding.waiting_department.state,
    Onboarding.waiting_style.state,
    Onboarding.waiting_time.state,
    Onboarding.waiting_tz_confirm.state,
    Onboarding.waiting_tz_manual.state,
    Onboarding.final.state,
}

ONBOARDING_PROMPTS = {
    Onboarding.waiting_goal.state: (
        "Привіт! Давай підлаштуємо асистента під тебе.\n\n"
        "Спочатку: на чому хочеш сфокусуватись?\n"
        "Напиши коротко: наприклад, «сон», «стрес», «продуктивність»."
    ),
    Onboarding.waiting_stress.state: (
        "Ок, сфокусуємось на цьому.\n"
        "Тепер оціни свій поточний рівень стресу від 1 до 5."
    ),
    Onboarding.waiting_energy.state: "Дякую. Тепер оціни рівень енергії від 1 до 5.",
    Onboarding.waiting_position.state: "Чим ти займаєшся? Напиши свою посаду (наприклад, Project Manager).",
    Onboarding.waiting_department.state: "А тепер департамент: IT, HR, Finance, Sales чи щось своє.",
    Onboarding.waiting_style.state: (
        "Як тобі комфортніше, щоб я з тобою говорив?\n"
        "Наприклад: «мʼякий», «прямий», «нейтральний»."
    ),
    Onboarding.waiting_time.state: (
        "О котрій годині зручно отримувати щоденні кроки?\n"
        "Формат: HH:MM, наприклад 09:00 або 21:30."
    ),
    Onboarding.waiting_tz_manual.state: (
        "Введи назву часового поясу, наприклад Europe/Kyiv, Europe/Berlin, America/New_York."
    ),
}

ONBOARDING_STATE_LABELS = {
    Onboarding.waiting_goal.state: "onboarding:waiting_goal",
    Onboarding.waiting_stress.state: "onboarding:waiting_stress",
    Onboarding.waiting_energy.state: "onboarding:waiting_energy",
    Onboarding.waiting_position.state: "onboarding:waiting_position",
    Onboarding.waiting_department.state: "onboarding:waiting_department",
    Onboarding.waiting_style.state: "onboarding:waiting_style",
    Onboarding.waiting_time.state: "onboarding:waiting_time",
    Onboarding.waiting_tz_confirm.state: "onboarding:waiting_tz_confirm",
    Onboarding.waiting_tz_manual.state: "onboarding:waiting_tz_manual",
    Onboarding.final.state: "onboarding:final",
}


def _parse_time_input(raw: str | None) -> str | None:
    if not raw:
        return None

    raw = raw.strip()
    match = _TIME_COLON_RE.search(raw)
    hours: int | None
    minutes: int | None
    hours = minutes = None

    if match:
        hours = int(match.group(1))
        minutes = int(match.group(2))
    else:
        digits_match = _TIME_DIGITS_RE.search(raw)
        if digits_match:
            digits = digits_match.group(1)
            if len(digits) == 4:
                hours = int(digits[:2])
                minutes = int(digits[2:])
            elif len(digits) == 3:
                hours = int(digits[0])
                minutes = int(digits[1:])

    if hours is None or minutes is None:
        return None

    if 0 <= hours <= 23 and 0 <= minutes <= 59:
        return f"{hours:02d}:{minutes:02d}"

    return None


def _onboarding_keyboard(state_name: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➡️ Продовжити", callback_data=f"onb:continue:{state_name}")],
            [InlineKeyboardButton(text="⏭️ Пропустити онбординг", callback_data="onb:skip")],
        ]
    )


def get_current_state_label(state_name: str | None) -> str:
    if not state_name:
        return "idle"
    if state_name in ONBOARDING_STATE_LABELS:
        return ONBOARDING_STATE_LABELS[state_name]
    if state_name == PlanStates.waiting_new_hour.state:
        return "plan:waiting_new_hour"
    return state_name


def _skip_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Так, пропустити", callback_data="onb:skip_confirm")],
            [InlineKeyboardButton(text="⬅️ Назад до онбордингу", callback_data="onb:skip_cancel")],
        ]
    )


def _consent_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Погоджуюсь", callback_data="consent:accept")],
            [InlineKeyboardButton(text="❌ Не погоджуюсь", callback_data="consent:decline")],
        ]
    )


def _ui_keyboard(suggested_ui: str | None) -> InlineKeyboardMarkup | None:
    if suggested_ui == "psychologist":
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Написати психологу",
                        url="https://t.me/veniviciave",
                    )
                ]
            ]
        )
    if suggested_ui == "settings":
        return InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="Налаштувати час", callback_data="ui:settings")]]
        )
    if suggested_ui == "plan_adjustment":
        return InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="Підлаштувати план", callback_data="ui:plan_adjustment")]]
        )
    return None


def _tz_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Так, все ок", callback_data="tz:ok")],
            [InlineKeyboardButton(text="🌍 Змінити", callback_data="tz:change")],
        ]
    )


def _log_onboarding_event(
    user_id: int | None,
    state: str,
    event_type: str,
    extra: dict | None = None,
    *,
    db=None,
    tg_id: int | None = None,
):
    owns_session = db is None
    session = db or SessionLocal()
    try:
        db_user_id = user_id
        if db_user_id is None and tg_id is not None:
            db_user = session.scalars(select(User).where(User.tg_id == tg_id)).first()
            db_user_id = db_user.id if db_user else None

        if db_user_id is None:
            return

        session.add(
            OnboardingEvent(
                user_id=db_user_id,
                state=state,
                event_type=event_type,
                extra=extra,
            )
        )
        if owns_session:
            session.commit()
    except Exception:
        if owns_session:
            session.rollback()
        print(f"[onboarding_event_failed] user={user_id} state={state} type={event_type}")
    finally:
        if owns_session:
            session.close()


async def _send_onboarding_prompt(
    m: Message | None,
    state_name: str,
    *,
    chat_id: int | None = None,
    user_id: int | None = None,
):
    prompt = ONBOARDING_PROMPTS.get(state_name)
    if not prompt:
        return

    target_user_id = user_id or (m.from_user.id if m else None)
    if target_user_id:
        _log_onboarding_event(user_id, state_name, "step_enter", tg_id=target_user_id)

    if m:
        await m.answer(prompt, reply_markup=_onboarding_keyboard(state_name))
    elif chat_id:
        await bot.send_message(chat_id, prompt, reply_markup=_onboarding_keyboard(state_name))


async def _send_consent_prompt(m: Message | None, *, chat_id: int | None = None):
    target_user_id = m.from_user.id if m else chat_id
    if target_user_id:
        _log_onboarding_event(None, Onboarding.waiting_consent.state, "step_enter", tg_id=target_user_id)

    if m:
        await m.answer(CONSENT_TEXT, reply_markup=_consent_keyboard())
    elif chat_id:
        await bot.send_message(chat_id, CONSENT_TEXT, reply_markup=_consent_keyboard())


def _current_timezone_name(u: User | None, mp: UserMemoryProfile | None) -> str:
    if u and u.timezone:
        return u.timezone
    if mp and mp.timezone:
        return mp.timezone
    return "Europe/Kyiv"


async def _send_timezone_confirm_prompt(
    m: Message | None,
    *,
    chat_id: int | None = None,
    tz_name: str,
):
    target_user_id = m.from_user.id if m else chat_id
    if target_user_id:
        _log_onboarding_event(None, Onboarding.waiting_tz_confirm.state, "step_enter", tg_id=target_user_id)

    text = TIMEZONE_CONFIRM_TEMPLATE.format(tz=tz_name)
    if m:
        await m.answer(text, reply_markup=_tz_confirm_keyboard())
    elif chat_id:
        await bot.send_message(chat_id, text, reply_markup=_tz_confirm_keyboard())


async def _send_manual_timezone_prompt(m: Message | None, *, chat_id: int | None = None):
    target_user_id = m.from_user.id if m else chat_id
    if target_user_id:
        _log_onboarding_event(None, Onboarding.waiting_tz_manual.state, "step_enter", tg_id=target_user_id)

    prompt = ONBOARDING_PROMPTS[Onboarding.waiting_tz_manual.state]
    if m:
        await m.answer(prompt)
    elif chat_id:
        await bot.send_message(chat_id, prompt)


def _profile_snapshot_for_ai(u: User, mp: UserMemoryProfile, data: dict) -> str:
    parts = [f"{u.first_name or ''} @{u.username or ''}".strip()]

    for label, key in [
        ("goal", "main_goal"),
        ("stress", "base_stress_level"),
        ("energy", "base_energy_level"),
        ("position", "position"),
        ("department", "department"),
        ("style", "communication_style"),
    ]:
        value = data.get(key, None)
        if value is None:
            value = getattr(mp, key, None)
        if value:
            parts.append(f"{label}: {value}")

    return "; ".join(p for p in parts if p)


def _profile_dict_for_router(mp: UserMemoryProfile | None, data: dict | None) -> dict:
    data = data or {}
    profile: dict = {}
    for key in [
        "main_goal",
        "base_stress_level",
        "base_energy_level",
        "position",
        "department",
        "communication_style",
        "notification_time",
        "timezone",
    ]:
        if data.get(key) is not None:
            value = data.get(key)
        else:
            value = getattr(mp, key, None) if mp else None
        if value is None:
            continue
        if isinstance(value, dt_time):
            profile[key] = value.strftime("%H:%M")
        else:
            profile[key] = value
    return profile


async def _append_recent_message(
    state: FSMContext, role: str, text: str, *, user_id: int | None = None
):
    data = await state.get_data()
    messages = list(data.get("recent_messages", [])) if isinstance(data, dict) else []
    messages.append({"role": role, "text": text})
    if len(messages) > RECENT_MESSAGES_LIMIT:
        messages = messages[-RECENT_MESSAGES_LIMIT:]

    update_payload = {"recent_messages": messages}
    if role == "bot":
        update_payload["last_bot_message"] = text

    await state.update_data(**update_payload)
    await session_memory.append_message(user_id, role, text)


async def _handle_onboarding_non_answer(m: Message, state: FSMContext):
    state_name = await state.get_state()
    if not state_name:
        return

    user_id = None
    router_result = None
    profile_for_ai = ""
    with SessionLocal() as db:
        u = db.scalars(select(User).where(User.tg_id == m.from_user.id)).first()
        if not u:
            await m.answer("Натисни /start")
            return

        mp = _get_or_create_memory_profile(db, u)
        await _append_recent_message(state, "user", m.text or "", user_id=u.id)
        data = await state.get_data()
        recent_messages = await session_memory.get_recent_messages(u.id)
        last_bot_message = await session_memory.get_last_bot_message(u.id)
        profile_for_ai = _profile_snapshot_for_ai(u, mp, data)
        router_context = {
            "user_id": u.id,
            "tg_id": m.from_user.id,
            "current_state": get_current_state_label(state_name),
            "last_bot_message": last_bot_message
            or (data or {}).get("last_bot_message")
            or ONBOARDING_PROMPTS.get(state_name),
            "recent_messages": recent_messages
            or (data or {}).get("recent_messages", []),
            "message_text": m.text or "",
            "message_type": "text",
            "user_profile": _profile_dict_for_router(mp, data),
        }

        router_result = await route_message(router_context)
        user_id = u.id

    intent = (router_result or {}).get("intent")
    if intent == "safety_alert":
        await _handle_safety_alert(m, state, source="router_onboarding")
        return

    if intent in {"coach_dialog", "onboarding_interruption"}:
        short_prompt = (
            "Ти відповідаєш українською під час онбордингу. Будь лаконічним, 1–2 речення, дружньо, без зустрічних питань."
        )
        try:
            text, _usage = answer_user_question(
                profile_for_ai or "Onboarding user",
                m.text or "",
                short_prompt,
            )
            await m.answer(_escape(text))
            await _append_recent_message(state, "bot", text, user_id=user_id)
        except Exception as e:
            print("=== ONBOARDING ROUTER ANSWER ERROR ===\n", traceback.format_exc())
            await m.answer(f"ERR [{_escape(e.__class__.__name__)}]: {_escape(str(e))}")
            return

        await _send_onboarding_prompt(m, state_name, user_id=user_id)
        return

    await m.answer(
        "Не зовсім зрозумів відповідь. Відповідай, будь ласка, у потрібному форматі, щоб продовжити налаштування."
    )
    await _append_recent_message(
        state,
        "bot",
        "Не зовсім зрозумів відповідь. Відповідай, будь ласка, у потрібному форматі, щоб продовжити налаштування.",
        user_id=user_id,
    )
    await _send_onboarding_prompt(m, state_name, user_id=user_id)


async def _handle_safety_alert(m: Message, state: FSMContext, source: str | None = None):
    current_state = await state.get_state()
    data = await state.get_data()

    user_id = data.get("user_id") if isinstance(data, dict) else None
    if user_id is None:
        with SessionLocal() as db:
            u = db.scalars(select(User).where(User.tg_id == m.from_user.id)).first()
            if u:
                user_id = u.id

    _log_onboarding_event(
        user_id,
        current_state or "idle",
        "safety_alert",
        tg_id=m.from_user.id,
        extra={"source": source or "router", "state": current_state},
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Написати психологу",
                    url="https://t.me/veniviciave",
                )
            ]
        ]
    )

    reply_text = (
        "Мені дуже шкода, що тобі зараз настільки важко. Я поряд і хочу підтримати.\n"
        "Я лише бот і не можу замінити кризову допомогу. Якщо відчуваєш, що не справляєшся — напиши спеціалісту або звернись до лікаря/гарячої лінії."
    )

    await m.answer(reply_text, reply_markup=kb)
    await _append_recent_message(state, "bot", reply_text, user_id=user_id)


async def _handle_onboarding_distress(m: Message, state: FSMContext):
    await _handle_safety_alert(m, state, source="onboarding")


async def _start_onboarding_skip_flow(m: Message | None, *, chat_id: int | None = None):
    target_chat = chat_id or (m.chat.id if m else None)
    if target_chat is None:
        return

    text = (
        "Окей, можна пропустити.\n"
        "Без онбордингу я працюю в базовому режимі — без персоналізації по стресу/енергії/посаді.\n"
        "Ти впевнений, що хочеш пропустити?"
    )

    if m:
        await m.answer(text, reply_markup=_skip_keyboard())
    else:
        await bot.send_message(target_chat, text, reply_markup=_skip_keyboard())


@router.message(Onboarding.waiting_consent)
async def onboarding_consent(m: Message, state: FSMContext):
    await _send_consent_prompt(m)


@router.callback_query(F.data == "consent:accept")
async def onboarding_consent_accept(c: CallbackQuery, state: FSMContext):
    await c.answer("Дякую за згоду")
    if c.message:
        try:
            await c.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

    user_id = None
    with SessionLocal() as db:
        u = db.scalars(select(User).where(User.tg_id == c.from_user.id)).first()
        if not u:
            if c.message:
                await c.message.answer("Натисни /start")
            else:
                await bot.send_message(c.from_user.id, "Натисни /start")
            return

        mp = _get_or_create_memory_profile(db, u)
        mp.consent_given = True
        _log_onboarding_event(
            u.id,
            Onboarding.waiting_consent.state,
            "step_answer",
            db=db,
        )
        db.commit()

        user_id = u.id

    await state.set_state(Onboarding.waiting_goal)
    await _send_onboarding_prompt(c.message, Onboarding.waiting_goal.state, chat_id=c.from_user.id, user_id=user_id)


@router.callback_query(F.data == "consent:decline")
async def onboarding_consent_decline(c: CallbackQuery, state: FSMContext):
    await c.answer("Зрозуміло")
    await state.clear()

    if c.message:
        try:
            await c.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

    text = (
        "Окей, без згоди я не можу підлаштовуватися під тебе.\n"
        "Якщо передумаєш — надішли /onboarding."
    )

    if c.message:
        await c.message.answer(text)
    else:
        await bot.send_message(c.from_user.id, text)


def _apply_skip_defaults(u: User, mp: UserMemoryProfile, data: dict):
    mp.main_goal = data.get("main_goal") or mp.main_goal or "wellbeing"
    mp.base_stress_level = data.get("base_stress_level") or mp.base_stress_level
    mp.base_energy_level = data.get("base_energy_level") or mp.base_energy_level
    mp.position = data.get("position") or mp.position
    mp.department = data.get("department") or mp.department
    mp.communication_style = (
        data.get("communication_style")
        or mp.communication_style
        or "нейтральний"
    )

    notification_time = data.get("notification_time") or mp.notification_time
    if not notification_time:
        hour = u.send_hour if u.send_hour is not None else settings.DEFAULT_SEND_HOUR
        notification_time = dt_time(hour=hour, minute=0)
    mp.notification_time = notification_time
    if notification_time:
        u.send_hour = notification_time.hour

    mp.timezone = mp.timezone or u.timezone or "Europe/Kyiv"
    mp.onboarding_completed = True


def _escape(value) -> str:
    if value is None:
        return ""
    return html.escape(str(value))


def _coerce_plan_payload(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            match = _JSON_RE.search(text)
            if match:
                try:
                    parsed = json.loads(match.group(0))
                    if isinstance(parsed, dict):
                        return parsed
                except json.JSONDecodeError:
                    pass
        return {}
    return {}


def _get_or_create_memory_profile(db, user: User) -> UserMemoryProfile:
    mp = (
        db.query(UserMemoryProfile)
        .filter(UserMemoryProfile.user_id == user.id)
        .first()
    )
    if mp:
        return mp

    mp = UserMemoryProfile(
        user_id=user.id,
        profile_data={},
    )
    db.add(mp)
    db.flush()
    return mp


async def _start_onboarding_flow(
    m: Message,
    state: FSMContext,
    *,
    start_state: State,
    user_id: int,
):
    await state.clear()
    await state.update_data(user_id=user_id)
    _log_onboarding_event(user_id, start_state.state, "start")
    await state.set_state(start_state)

    if start_state == Onboarding.waiting_consent:
        await _send_consent_prompt(m)
        return

    await _send_onboarding_prompt(m, start_state.state, user_id=user_id)


def is_admin(user_id: int) -> bool:
    return user_id in settings.ADMIN_IDS


def today_str(tz: str = "Europe/Kyiv") -> str:
    import pytz, datetime as dt
    return dt.datetime.now(pytz.timezone(tz)).strftime("%Y-%m-%d")


def month_str(tz: str = "Europe/Kyiv") -> str:
    import pytz, datetime as dt
    return dt.datetime.now(pytz.timezone(tz)).strftime("%Y-%m")


async def send_daily_with_buttons(bot: Bot, chat_id: int, text: str):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="👍 Корисно", callback_data="fb:up"),
            InlineKeyboardButton(text="👎 Не дуже", callback_data="fb:down"),
        ],
        [
            InlineKeyboardButton(text="💬 Поставити питання", callback_data="ask:init"),
        ]
    ])
    try:
        return await bot.send_message(chat_id, text, reply_markup=kb)
    except Exception:
        return None

# ----------------- службові -----------------

@router.message(Command("ping"))
async def cmd_ping(m: Message):
    await m.answer("pong")

# ----------------- старт / help / ліміт -----------------

@router.message(Command("start"))
async def cmd_start(m: Message, state: FSMContext):
    should_start_onboarding = False
    start_state = Onboarding.waiting_goal
    user_id = None
    with SessionLocal() as db:
        u = db.scalars(select(User).where(User.tg_id == m.from_user.id)).first()
        if not u:
            u = User(
                tg_id=m.from_user.id,
                first_name=m.from_user.first_name or "",
                username=m.from_user.username or "",
                daily_limit=settings.DEFAULT_DAILY_LIMIT,
                send_hour=9,
            )
            db.add(u)
            db.flush()

        mp = _get_or_create_memory_profile(db, u)
        if not getattr(mp, "consent_given", False):
            should_start_onboarding = True
            start_state = Onboarding.waiting_consent
        elif not getattr(mp, "onboarding_completed", False):
            should_start_onboarding = True
            start_state = Onboarding.waiting_goal

        user_id = u.id
        db.commit()

    if should_start_onboarding:
        return await _start_onboarding_flow(m, state, start_state=start_state, user_id=user_id)

    await m.answer(
        "Привіт! Я wellbeing-бот Love Yourself 🌿\n"
        "Щодня надсилатиму коротке повідомлення для самопідтримки.\n"
        "Використай /plan щоб створити план, або /ask щоб поставити питання."
    )


@router.message(Command("help"))
async def cmd_help(m: Message):
    await m.answer(
        "/ask — поставити питання\n"
        "/limit — залишок ліміту\n"
        "/plan <опис> — згенерувати план\n"
        "/plan_status — прогрес активного плану\n"
        "/plan_pause — призупинити план\n"
        "/plan_resume — відновити план\n"
        "/plan_cancel — завершити план\n"
        "/remind <час | текст> — нагадування"
    )


@router.message(Command("onboarding"))
async def cmd_onboarding(m: Message, state: FSMContext):
    from sqlalchemy import select

    start_state = Onboarding.waiting_goal
    user_id = None
    with SessionLocal() as db:
        u = db.scalars(select(User).where(User.tg_id == m.from_user.id)).first()
        if not u:
            await m.answer("Натисни /start спочатку, будь ласка.")
            return

        mp = _get_or_create_memory_profile(db, u)
        if not getattr(mp, "consent_given", False):
            start_state = Onboarding.waiting_consent
        elif getattr(mp, "onboarding_completed", False):
            start_state = Onboarding.waiting_goal
        user_id = u.id
        db.commit()

    await _start_onboarding_flow(m, state, start_state=start_state, user_id=user_id)


@router.message(Command("limit"))
async def cmd_limit(m: Message):
    with SessionLocal() as db:
        u = db.scalars(select(User).where(User.tg_id == m.from_user.id)).first()
        if not u:
            await m.answer("Натисни /start")
            return
        day = today_str(u.timezone or "Europe/Kyiv")
        cnt = db.scalars(
            select(UsageCounter).where(
                UsageCounter.user_id == u.id,
                UsageCounter.day == day
            )
        ).first()
        used = cnt.ask_count if cnt else 0
        await m.answer(f"Залишилось {max(0, (u.daily_limit or 10) - used)} з {u.daily_limit or 10}")

# ----------------- Q&A -----------------

@router.message(Command("ask"))
async def cmd_ask(m: Message):
    await m.answer("Напиши питання наступним повідомленням.")


@router.message(Onboarding.waiting_goal)
async def onboarding_goal(m: Message, state: FSMContext):
    state_name = await state.get_state()
    data = await state.get_data()
    user_id = data.get("user_id") if isinstance(data, dict) else None
    intent = classify_onboarding_message(m.text or "", state_name or "", ONBOARDING_PROMPTS.get(state_name))
    if intent == OnboardingIntent.DISTRESS:
        await _handle_onboarding_distress(m, state)
        return
    if intent == OnboardingIntent.SKIP:
        _log_onboarding_event(user_id, state_name or "waiting_goal", "step_skip_requested", tg_id=m.from_user.id)
        await _start_onboarding_skip_flow(m)
        return
    if intent != OnboardingIntent.ANSWER:
        await _handle_onboarding_non_answer(m, state)
        return

    goal = (m.text or "").strip()
    if not goal:
        await m.answer("Напиши, будь ласка, хоча б одне слово про свою ціль 🙃")
        return

    await state.update_data(main_goal=goal)
    _log_onboarding_event(user_id, state_name or "waiting_goal", "step_answer", tg_id=m.from_user.id)

    await state.set_state(Onboarding.waiting_stress)
    await _send_onboarding_prompt(m, Onboarding.waiting_stress.state, user_id=user_id)


@router.message(Onboarding.waiting_stress)
async def onboarding_stress(m: Message, state: FSMContext):
    state_name = await state.get_state()
    data = await state.get_data()
    user_id = data.get("user_id") if isinstance(data, dict) else None
    intent = classify_onboarding_message(m.text or "", state_name or "", ONBOARDING_PROMPTS.get(state_name))
    if intent == OnboardingIntent.DISTRESS:
        await _handle_onboarding_distress(m, state)
        return
    if intent == OnboardingIntent.SKIP:
        _log_onboarding_event(user_id, state_name or "waiting_stress", "step_skip_requested", tg_id=m.from_user.id)
        await _start_onboarding_skip_flow(m)
        return
    if intent != OnboardingIntent.ANSWER:
        await _handle_onboarding_non_answer(m, state)
        return

    try:
        value = int((m.text or "").strip())
    except ValueError:
        await m.answer("Введи число від 1 до 5 😉")
        return

    if value < 1 or value > 5:
        await m.answer("Тільки від 1 до 5, без креативу тут 😅")
        return

    await state.update_data(base_stress_level=value)
    _log_onboarding_event(user_id, state_name or "waiting_stress", "step_answer", tg_id=m.from_user.id)

    await state.set_state(Onboarding.waiting_energy)
    await _send_onboarding_prompt(m, Onboarding.waiting_energy.state, user_id=user_id)


@router.message(Onboarding.waiting_energy)
async def onboarding_energy(m: Message, state: FSMContext):
    state_name = await state.get_state()
    data = await state.get_data()
    user_id = data.get("user_id") if isinstance(data, dict) else None
    intent = classify_onboarding_message(m.text or "", state_name or "", ONBOARDING_PROMPTS.get(state_name))
    if intent == OnboardingIntent.DISTRESS:
        await _handle_onboarding_distress(m, state)
        return
    if intent == OnboardingIntent.SKIP:
        _log_onboarding_event(user_id, state_name or "waiting_energy", "step_skip_requested", tg_id=m.from_user.id)
        await _start_onboarding_skip_flow(m)
        return
    if intent != OnboardingIntent.ANSWER:
        await _handle_onboarding_non_answer(m, state)
        return

    try:
        value = int((m.text or "").strip())
    except ValueError:
        await m.answer("Знову число від 1 до 5, будь ласка 🙂")
        return

    if value < 1 or value > 5:
        await m.answer("Все ще 1–5. Спробуй ще раз.")
        return

    await state.update_data(base_energy_level=value)
    _log_onboarding_event(user_id, state_name or "waiting_energy", "step_answer", tg_id=m.from_user.id)

    await state.set_state(Onboarding.waiting_position)
    await _send_onboarding_prompt(m, Onboarding.waiting_position.state, user_id=user_id)


@router.message(Onboarding.waiting_position)
async def onboarding_position(m: Message, state: FSMContext):
    state_name = await state.get_state()
    data = await state.get_data()
    user_id = data.get("user_id") if isinstance(data, dict) else None
    intent = classify_onboarding_message(m.text or "", state_name or "", ONBOARDING_PROMPTS.get(state_name))
    if intent == OnboardingIntent.DISTRESS:
        await _handle_onboarding_distress(m, state)
        return
    if intent == OnboardingIntent.SKIP:
        _log_onboarding_event(user_id, state_name or "waiting_position", "step_skip_requested", tg_id=m.from_user.id)
        await _start_onboarding_skip_flow(m)
        return
    if intent != OnboardingIntent.ANSWER:
        await _handle_onboarding_non_answer(m, state)
        return

    position = (m.text or "").strip()
    if not position:
        await m.answer("Напиши хоча б щось типу «Developer», «HR» тощо.")
        return

    await state.update_data(position=position)
    _log_onboarding_event(user_id, state_name or "waiting_position", "step_answer", tg_id=m.from_user.id)

    await state.set_state(Onboarding.waiting_department)
    await _send_onboarding_prompt(m, Onboarding.waiting_department.state, user_id=user_id)


@router.message(Onboarding.waiting_department)
async def onboarding_department(m: Message, state: FSMContext):
    state_name = await state.get_state()
    data = await state.get_data()
    user_id = data.get("user_id") if isinstance(data, dict) else None
    intent = classify_onboarding_message(m.text or "", state_name or "", ONBOARDING_PROMPTS.get(state_name))
    if intent == OnboardingIntent.DISTRESS:
        await _handle_onboarding_distress(m, state)
        return
    if intent == OnboardingIntent.SKIP:
        _log_onboarding_event(user_id, state_name or "waiting_department", "step_skip_requested", tg_id=m.from_user.id)
        await _start_onboarding_skip_flow(m)
        return
    if intent != OnboardingIntent.ANSWER:
        await _handle_onboarding_non_answer(m, state)
        return

    department = (m.text or "").strip()
    if not department:
        await m.answer("Напиши хоча б одне слово – як це називається у вас.")
        return

    await state.update_data(department=department)
    _log_onboarding_event(user_id, state_name or "waiting_department", "step_answer", tg_id=m.from_user.id)

    await state.set_state(Onboarding.waiting_style)
    await _send_onboarding_prompt(m, Onboarding.waiting_style.state, user_id=user_id)


@router.message(Onboarding.waiting_style)
async def onboarding_style(m: Message, state: FSMContext):
    state_name = await state.get_state()
    data = await state.get_data()
    user_id = data.get("user_id") if isinstance(data, dict) else None
    intent = classify_onboarding_message(m.text or "", state_name or "", ONBOARDING_PROMPTS.get(state_name))
    if intent == OnboardingIntent.DISTRESS:
        await _handle_onboarding_distress(m, state)
        return
    if intent == OnboardingIntent.SKIP:
        _log_onboarding_event(user_id, state_name or "waiting_style", "step_skip_requested", tg_id=m.from_user.id)
        await _start_onboarding_skip_flow(m)
        return
    if intent != OnboardingIntent.ANSWER:
        await _handle_onboarding_non_answer(m, state)
        return

    style = (m.text or "").strip()
    if not style:
        await m.answer("Напиши щось типу «мʼякий», «прямий», «нейтральний».")
        return

    await state.update_data(communication_style=style)
    _log_onboarding_event(user_id, state_name or "waiting_style", "step_answer", tg_id=m.from_user.id)

    await state.set_state(Onboarding.waiting_time)
    await _send_onboarding_prompt(m, Onboarding.waiting_time.state, user_id=user_id)


@router.message(Onboarding.waiting_time)
async def onboarding_time(m: Message, state: FSMContext):
    state_name = await state.get_state()
    data = await state.get_data()
    user_id = data.get("user_id") if isinstance(data, dict) else None
    intent = classify_onboarding_message(m.text or "", state_name or "", ONBOARDING_PROMPTS.get(state_name))
    if intent == OnboardingIntent.DISTRESS:
        await _handle_onboarding_distress(m, state)
        return
    if intent == OnboardingIntent.SKIP:
        _log_onboarding_event(user_id, state_name or "waiting_time", "step_skip_requested", tg_id=m.from_user.id)
        await _start_onboarding_skip_flow(m)
        return
    if intent != OnboardingIntent.ANSWER:
        await _handle_onboarding_non_answer(m, state)
        return

    parsed = _parse_time_input(m.text or "")
    if not parsed:
        await m.answer(
            "Будь ласка, надішли час у форматі HH:MM або як числа, наприклад 09:00, 21:30 чи 900."
        )
        return

    hour, minute = map(int, parsed.split(":"))

    await state.update_data(notification_time=dt_time(hour=hour, minute=minute))
    _log_onboarding_event(user_id, state_name or "waiting_time", "step_answer", tg_id=m.from_user.id)

    with SessionLocal() as db:
        u = db.scalars(select(User).where(User.tg_id == m.from_user.id)).first()
        mp = _get_or_create_memory_profile(db, u) if u else None
        tz_name = _current_timezone_name(u, mp)

    await state.set_state(Onboarding.waiting_tz_confirm)
    await _send_timezone_confirm_prompt(m, tz_name=tz_name)


@router.callback_query(F.data == "tz:ok")
async def onboarding_timezone_ok(c: CallbackQuery, state: FSMContext):
    await c.answer("Зберігаю налаштування")
    data = await state.get_data()
    user_id = data.get("user_id") if isinstance(data, dict) else None
    try:
        if c.message:
            await c.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    _log_onboarding_event(user_id, Onboarding.waiting_tz_confirm.state, "step_answer", tg_id=c.from_user.id)
    await state.set_state(Onboarding.final)
    if c.message:
        await _finish_onboarding(c.message, state)
    else:
        await bot.send_message(c.from_user.id, "Завершуємо онбординг…")


@router.callback_query(F.data == "tz:change")
async def onboarding_timezone_change(c: CallbackQuery, state: FSMContext):
    await c.answer("Змінюємо часовий пояс")
    data = await state.get_data()
    user_id = data.get("user_id") if isinstance(data, dict) else None
    try:
        if c.message:
            await c.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    _log_onboarding_event(
        user_id,
        Onboarding.waiting_tz_confirm.state,
        "step_answer",
        extra={"choice": "change"},
        tg_id=c.from_user.id,
    )
    await state.set_state(Onboarding.waiting_tz_manual)
    await _send_manual_timezone_prompt(c.message, chat_id=c.from_user.id)


@router.message(Onboarding.waiting_tz_manual)
async def onboarding_timezone_manual(m: Message, state: FSMContext):
    state_name = await state.get_state()
    data = await state.get_data()
    user_id = data.get("user_id") if isinstance(data, dict) else None
    intent = classify_onboarding_message(m.text or "", state_name or "", ONBOARDING_PROMPTS.get(state_name))
    if intent == OnboardingIntent.DISTRESS:
        await _handle_onboarding_distress(m, state)
        return
    if intent == OnboardingIntent.SKIP:
        _log_onboarding_event(user_id, state_name or "waiting_tz_manual", "step_skip_requested", tg_id=m.from_user.id)
        await _start_onboarding_skip_flow(m)
        return

    tz_value = (m.text or "").strip()
    try:
        pytz.timezone(tz_value)
    except pytz.UnknownTimeZoneError:
        await m.answer("Не знайшов такий часовий пояс. Спробуй ще раз, наприклад Europe/Kyiv.")
        return

    await state.update_data(timezone=tz_value)
    _log_onboarding_event(user_id, state_name or "waiting_tz_manual", "step_answer", tg_id=m.from_user.id)
    await state.set_state(Onboarding.final)
    await _finish_onboarding(m, state)


@router.callback_query(F.data.startswith("onb:continue"))
async def onboarding_continue_callback(c: CallbackQuery, state: FSMContext):
    await c.answer("Продовжуємо онбординг")

    if c.message:
        try:
            await c.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

    current_state = await state.get_state()
    data = await state.get_data()
    user_id = data.get("user_id") if isinstance(data, dict) else None
    await _send_onboarding_prompt(c.message, current_state, chat_id=c.from_user.id, user_id=user_id)


@router.callback_query(F.data == "onb:skip")
async def onboarding_skip_callback(c: CallbackQuery, state: FSMContext):
    await c.answer()
    state_name = await state.get_state()
    data = await state.get_data()
    user_id = data.get("user_id") if isinstance(data, dict) else None
    if state_name:
        _log_onboarding_event(user_id, state_name, "step_skip_requested", tg_id=c.from_user.id)
    if c.message:
        try:
            await c.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

    await _start_onboarding_skip_flow(c.message, chat_id=c.from_user.id)


@router.callback_query(F.data == "onb:skip_cancel")
async def onboarding_skip_cancel(c: CallbackQuery, state: FSMContext):
    await c.answer("Продовжуємо онбординг")
    current_state = await state.get_state()
    data = await state.get_data()
    user_id = data.get("user_id") if isinstance(data, dict) else None
    if c.message:
        try:
            await c.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

    if c.message:
        await c.message.answer("Окей, тоді продовжуємо з онбордингом.")
    else:
        await bot.send_message(c.from_user.id, "Окей, тоді продовжуємо з онбордингом.")

    await _send_onboarding_prompt(c.message, current_state, chat_id=c.from_user.id, user_id=user_id)


@router.callback_query(F.data == "onb:skip_confirm")
async def onboarding_skip_confirm(c: CallbackQuery, state: FSMContext):
    await c.answer("Пропускаємо онбординг")
    current_state = await state.get_state()
    data = await state.get_data()

    with SessionLocal() as db:
        u = db.scalars(select(User).where(User.tg_id == c.from_user.id)).first()
        if not u:
            if c.message:
                await c.message.answer("Натисни /start")
            else:
                await bot.send_message(c.from_user.id, "Натисни /start")
            return

        mp = _get_or_create_memory_profile(db, u)
        if current_state:
            _log_onboarding_event(u.id, current_state, "step_skip_confirm", db=db)
        _apply_skip_defaults(u, mp, data)
        _log_onboarding_event(u.id, current_state or "skip", "skipped", db=db)
        db.commit()

    await state.clear()

    if c.message:
        try:
            await c.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

    final_text = (
        "Готово. Працюємо в базовому режимі без детального онбордингу.\n"
        "Якщо захочеш — завжди можеш пройти налаштування командою /onboarding."
    )

    if c.message:
        await c.message.answer(final_text)
        await c.message.answer(QUICK_WIN_TEXT)
    else:
        await bot.send_message(c.from_user.id, final_text)
        await bot.send_message(c.from_user.id, QUICK_WIN_TEXT)


async def _finish_onboarding(m: Message, state: FSMContext):
    data = await state.get_data()
    current_state = await state.get_state()

    with SessionLocal() as db:
        from sqlalchemy import select

        u = db.scalars(select(User).where(User.tg_id == m.from_user.id)).first()
        if not u:
            await m.answer("Щось пішло не так: не знайшов користувача. Спробуй /start.")
            await state.clear()
            return

        mp = _get_or_create_memory_profile(db, u)

        mp.main_goal = data.get("main_goal")
        mp.base_stress_level = data.get("base_stress_level")
        mp.base_energy_level = data.get("base_energy_level")
        mp.position = data.get("position")
        mp.department = data.get("department")
        mp.communication_style = data.get("communication_style")
        mp.notification_time = data.get("notification_time")
        timezone = data.get("timezone") or mp.timezone or (u.timezone or "Europe/Kyiv")
        mp.timezone = timezone
        mp.onboarding_completed = True

        if data.get("timezone") or not u.timezone:
            u.timezone = timezone

        if data.get("notification_time"):
            notif_time = data["notification_time"]
            u.send_hour = notif_time.hour

        _log_onboarding_event(
            u.id,
            current_state or Onboarding.final.state,
            "completed",
            db=db,
        )

        db.commit()

    await state.clear()
    await m.answer(
        "Готово ✅\n"
        "Я запамʼятав твою ціль і налаштування.\n"
        "Тепер план і відповіді будуть більше під тебе."
    )
    await m.answer(QUICK_WIN_TEXT)

# Ігноруємо текстові команди на кшталт "/plan" в загальному обробнику
@router.message(F.text & ~F.via_bot & ~F.text.startswith("/"))
async def on_text(m: Message, state: FSMContext):
    with SessionLocal() as db:
        u = db.scalars(select(User).where(User.tg_id == m.from_user.id)).first()
        if not u:
            await m.answer("Натисни /start")
            return

        await _append_recent_message(state, "user", m.text or "", user_id=u.id)

        current_state = await state.get_state()
        if current_state == PlanStates.waiting_new_hour.state:
            data = await state.get_data()
            plan_id = data.get("plan_id")
            await _process_plan_hour_response(m, state, db, u, plan_id)
            return

        if current_state in ONBOARDING_STATE_NAMES:
            return

        data = await state.get_data()
        mp = _get_or_create_memory_profile(db, u)
        profile_snapshot = _profile_snapshot_for_ai(u, mp, data)
        recent_messages = await session_memory.get_recent_messages(u.id)
        last_bot_message = await session_memory.get_last_bot_message(u.id)
        if isinstance(data, dict):
            last_bot_message = last_bot_message or data.get("last_bot_message")
            fallback_recent_messages = data.get("recent_messages", [])
        else:
            fallback_recent_messages = []
        router_context = {
            "user_id": u.id,
            "tg_id": m.from_user.id,
            "current_state": get_current_state_label(current_state),
            "last_bot_message": last_bot_message,
            "recent_messages": recent_messages or fallback_recent_messages,
            "message_text": m.text or "",
            "message_type": "text",
            "user_profile": _profile_dict_for_router(mp, data),
        }

        router_result = await route_message(router_context)
        intent = router_result.get("intent")
        suggested_ui = router_result.get("suggested_ui")

        if intent == "safety_alert":
            await _handle_safety_alert(m, state, source="router_general")
            return

        if intent == "manager_flow":
            reply_text = (
                "Я запамʼятав, що ти хочеш налаштувати бот. "
                "У наступних версіях зʼявиться зручне меню, а поки що я просто беру це до уваги."
            )
            kb = _ui_keyboard(suggested_ui)
            if kb:
                await m.answer(reply_text, reply_markup=kb)
            else:
                await m.answer(reply_text)
            await _append_recent_message(state, "bot", reply_text, user_id=u.id)
            return

        # за замовчуванням працюємо як Coach
        day = today_str(u.timezone or "Europe/Kyiv")
        mon = month_str(u.timezone or "Europe/Kyiv")

        cnt = db.scalars(
            select(UsageCounter).where(
                UsageCounter.user_id == u.id,
                UsageCounter.day == day
            )
        ).first()
        used = cnt.ask_count if cnt else 0
        if used >= (u.daily_limit or 10):
            await m.answer("Ліміт на сьогодні вичерпано.")
            await _append_recent_message(state, "bot", "Ліміт на сьогодні вичерпано.", user_id=u.id)
            return

        try:
            text, _usage = answer_user_question(
                profile_snapshot or f"{u.first_name or ''} @{u.username or ''}",
                m.text,
                u.prompt_template
            )
        except Exception as e:
            print("=== GENERATION ERROR ===\n", traceback.format_exc())
            await m.answer(f"ERR [{_escape(e.__class__.__name__)}]: {_escape(str(e))}")
            await session_memory.append_message(
                u.id,
                "bot",
                f"ERR [{_escape(e.__class__.__name__)}]: {_escape(str(e))}",
            )
            return

        kb = _ui_keyboard(suggested_ui)
        if kb:
            await m.answer(_escape(text), reply_markup=kb)
        else:
            await m.answer(_escape(text))
        await _append_recent_message(state, "bot", text, user_id=u.id)

        if not cnt:
            cnt = UsageCounter(user_id=u.id, day=day, ask_count=0, month=mon, month_ask_count=0)
        cnt.ask_count += 1
        if cnt.month != mon:
            cnt.month = mon
            cnt.month_ask_count = 0
        cnt.month_ask_count += 1

        db.add(Response(delivery_id=None, user_id=u.id, kind="text", payload=m.text))
        db.add(cnt)
        db.commit()

# ----------------- План (основне) -----------------

pdt_calendar = pdt.Calendar()
PLAN_PREVIEW_STEP_LIMIT = 3


def _get_timezone(tz_name: Optional[str]) -> pytz.BaseTzInfo:
    try:
        return pytz.timezone(tz_name or "Europe/Kyiv")
    except pytz.UnknownTimeZoneError:
        return pytz.timezone("Europe/Kyiv")


def _format_plan_message(plan: AIPlan, steps: List[AIPlanStep], tz_name: Optional[str], *, limit: Optional[int] = PLAN_PREVIEW_STEP_LIMIT, note: Optional[str] = None) -> str:
    tz = _get_timezone(tz_name)
    lines: List[str] = [
        f"План: {_escape(plan.name or '')}",
        f"Статус: {_escape(plan.status or '')}",
    ]
    if getattr(plan, "goal", None):
        lines.append(f"Ціль: {_escape(plan.goal)}")
    if getattr(plan, "duration_days", None):
        lines.append(f"Тривалість: {_escape(plan.duration_days)} днів")
    if getattr(plan, "tasks_per_day", None):
        lines.append(f"Кроків на день: {_escape(plan.tasks_per_day)}")
    send_hour = getattr(plan, "send_hour", None)
    if send_hour is not None:
        send_minute = getattr(plan, "send_minute", 0) or 0
        lines.append(f"Бажаний час: {int(send_hour):02d}:{int(send_minute):02d}")
    if plan.approved_at:
        lines.append(f"Затверджено: {plan.approved_at.astimezone(tz).strftime('%Y-%m-%d %H:%M')}")
    if note:
        lines.append("")
        lines.append(_escape(note))

    lines.append("")

    sorted_steps = sorted(
        steps,
        key=lambda s: (
            getattr(s, "day_index", None) if getattr(s, "day_index", None) is not None else getattr(s, "day", 0) - 1,
            getattr(s, "slot_index", 0),
            s.scheduled_for or s.proposed_for or datetime.max.replace(tzinfo=pytz.UTC),
        ),
    )
    display_steps = sorted_steps if limit is None else sorted_steps[:limit]
    current_day = None
    for step in display_steps:
        day_idx = getattr(step, "day_index", None)
        day_number = (day_idx + 1) if day_idx is not None else getattr(step, "day", None) or 1
        if current_day != day_number:
            if current_day is not None:
                lines.append("")
            lines.append(f"День {day_number}")
            current_day = day_number

        dt_source = step.scheduled_for or step.proposed_for
        when_str = "?"
        if dt_source:
            dt_local = dt_source.astimezone(tz)
            when_str = dt_local.strftime("%H:%M")
        elif getattr(step, "time", None):
            when_str = str(getattr(step, "time"))
        status_text = _escape(step.status or "pending")
        message_text = _escape(step.message or "")
        lines.append(f" • {when_str} [{status_text}] — {message_text}")

    total_steps = len(sorted_steps)
    if limit is not None and total_steps > limit:
        lines.append("")
        lines.append(f"Показано перші {limit} кроки з {total_steps}.")

    return "\n".join(lines).strip()


def _plan_keyboard(plan: AIPlan):
    if plan.status in {"draft", "pending"}:
        return InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Затвердити", callback_data=f"plan:approve:{plan.id}"),
                InlineKeyboardButton(text="🕘 Змінити час", callback_data=f"plan:change_hour:{plan.id}"),
            ],
            [InlineKeyboardButton(text="❌ Скасувати", callback_data=f"plan:cancel:{plan.id}")]
        ])
    elif plan.status == "active":
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Скасувати", callback_data=f"plan:cancel:{plan.id}")]
        ])
    return None


def _parse_hour_minute(text: str) -> tuple[int, int] | None:
    cleaned = (text or "").strip().replace(".", ":")
    if not cleaned:
        return None
    if ":" in cleaned:
        hour_part, minute_part = cleaned.split(":", 1)
    else:
        hour_part, minute_part = cleaned, "00"
    try:
        hour = int(hour_part)
        minute = int(minute_part)
    except ValueError:
        return None
    if 0 <= hour < 24 and 0 <= minute < 60:
        return hour, minute
    return None


def _get_latest_plan(db, user_id: int, statuses: tuple[str, ...] | None = None) -> AIPlan | None:
    query = db.query(AIPlan).filter(AIPlan.user_id == user_id)
    if statuses:
        query = query.filter(AIPlan.status.in_(statuses))
    return query.order_by(AIPlan.created_at.desc()).first()


async def _process_plan_hour_response(
    message: Message,
    state: FSMContext,
    db,
    user: User,
    plan_id: Optional[int],
) -> None:
    if not plan_id:
        await message.answer("Сталася помилка. Спробуй запустити зміну часу ще раз.")
        await state.clear()
        return

    parsed = _parse_hour_minute(message.text)
    if not parsed:
        await message.answer("Не вдалося розпізнати час. Напиши у форматі HH:MM, напр. 09:00.")
        await state.clear()
        return
    hour, minute = parsed

    plan = (
        db.query(AIPlan)
        .filter(AIPlan.id == plan_id, AIPlan.user_id == user.id)
        .first()
    )
    if not plan:
        await message.answer("План не знайдено або вже завершений.")
        await state.clear()
        return

    steps = (
        db.query(AIPlanStep)
        .filter(
            AIPlanStep.plan_id == plan.id,
            AIPlanStep.is_completed == False,
            (AIPlanStep.status.is_(None)) | (AIPlanStep.status.notin_(["completed", "canceled"])),
        )
        .all()
    )
    if not steps:
        await message.answer("У плані немає кроків для оновлення.")
        await state.clear()
        return

    user_tz = pytz.timezone(user.timezone or "Europe/Kyiv")
    now_local = dtmod.datetime.now(user_tz)

    for step in steps:
        base_dt = step.scheduled_for or step.proposed_for or now_local
        if base_dt.tzinfo is None:
            base_dt = user_tz.localize(base_dt)  # на всяк випадок
        local_dt = base_dt.astimezone(user_tz)
        new_local = local_dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if new_local <= now_local:
            new_local += timedelta(days=1)
        new_utc = new_local.astimezone(pytz.UTC)

        if step.job_id:
            remove_job(step.job_id)
            step.job_id = None
        step.scheduled_for = None
        step.proposed_for = new_utc
        step.status = "pending"

    plan.status = "pending"
    plan.approved_at = None
    db.commit()

    await message.answer(
        f"Годину плану оновлено на {hour:02d}:{minute:02d}. Кроки позначено як pending і чекають підтвердження."
    )

    all_steps = (
        db.query(AIPlanStep)
        .filter(AIPlanStep.plan_id == plan.id)
        .order_by(
            AIPlanStep.day_index,
            AIPlanStep.slot_index,
            AIPlanStep.scheduled_for,
            AIPlanStep.proposed_for,
        )
        .all()
    )
    preview_text = _format_plan_message(plan, all_steps, user.timezone or "Europe/Kyiv")
    keyboard = _plan_keyboard(plan)
    await message.answer(preview_text, reply_markup=keyboard)

    await state.clear()

# ----------------- Команди створення/керування планом -----------------

@router.message(Command("plan"))
async def cmd_plan(m: Message):
    parsed = parse_plan_request(m.text or "")
    if not parsed.original_text:
        await m.answer("Використання: /plan <опис> (наприклад: план покращення сну на 30 днів)")
        return

    with SessionLocal() as db:
        u = db.scalars(select(User).where(User.tg_id == m.from_user.id)).first()
        if not u:
            await m.answer("Натисни /start")
            return

        mp = db.query(UserMemoryProfile).filter(UserMemoryProfile.user_id == u.id).first()

        actual_tasks_per_day = max(parsed.tasks_per_day, len(parsed.hours_list))

        try:
            plan_payload = _coerce_plan_payload(
                generate_ai_plan(
                    goal=parsed.goal or parsed.original_text,
                    days=parsed.days,
                    tasks_per_day=actual_tasks_per_day,
                    preferred_hour=parsed.time_str,
                    preferred_hours=parsed.hours_list,
                    tz_name=u.timezone or "Europe/Kyiv",
                    memory=mp.profile_data if mp else None,
                )
            )
        except Exception as e:
            await m.answer(f"Помилка генерації плану: {_escape(str(e))}")
            return

        steps_payload = normalize_plan_steps(
            plan_payload,
            goal=parsed.goal or parsed.original_text or "Підтримка добробуту",
            days=parsed.days,
            tasks_per_day=actual_tasks_per_day,
            preferred_hour=parsed.time_str,
            preferred_hours=parsed.hours_list,
            tz_name=u.timezone or "Europe/Kyiv",
        )

        if not steps_payload:
            await m.answer(
                "Не вдалося сформувати кроки плану. Спробуй інший запит або іншу годину."
            )
            return

        # створюємо чернетку: кроки -> pending + proposed_for (UTC), без job_id
        plan_name = None
        if isinstance(plan_payload, dict):
            plan_name = plan_payload.get("plan_name")
            if isinstance(plan_name, str):
                plan_name = plan_name.strip() or None

        plan = AIPlan(
            user_id=u.id,
            name=plan_name
            or parsed.goal
            or parsed.original_text
            or "Персональний план турботи",
            description=parsed.original_text,
            status="draft",
            approved_at=None,
            goal=parsed.goal,
            duration_days=parsed.days,
            send_hour=parsed.hour,
            send_minute=parsed.minute,
            tasks_per_day=actual_tasks_per_day,
        )
        db.add(plan)
        db.flush()

        stored_steps: List[AIPlanStep] = []

        for s in steps_payload:
            msg = str(s.get("message") or "").strip()
            proposed = s.get("proposed_for")
            if not msg or not isinstance(proposed, datetime):
                continue
            if proposed.tzinfo is None:
                proposed = pytz.UTC.localize(proposed)
            day_index = s.get("day_index")
            if day_index is None:
                try:
                    day_index = int(s.get("day", 1)) - 1
                except Exception:
                    day_index = 0
            slot_index = s.get("slot_index") or 0

            step = AIPlanStep(
                plan_id=plan.id,
                job_id=None,
                message=msg,
                status="pending",
                proposed_for=proposed.astimezone(pytz.UTC),
                scheduled_for=None,
                day_index=day_index,
                slot_index=slot_index,
                is_completed=False,
                completed_at=None,
            )
            db.add(step)
            stored_steps.append(step)

        db.commit()
        db.refresh(plan)

        preview_text = _format_plan_message(
            plan,
            stored_steps,
            u.timezone or "Europe/Kyiv",
            limit=PLAN_PREVIEW_STEP_LIMIT,
        )
        keyboard = _plan_keyboard(plan)
        await m.answer(preview_text, reply_markup=keyboard)

        goal_text = (parsed.goal or "").replace(";", ",")
        db.add(
            Response(
                delivery_id=None,
                user_id=u.id,
                kind="plan_preview",
                payload=(
                    "plan_id={plan_id};status={status};steps={steps};goal={goal};days={days};time={time};tasks_per_day={tasks}".format(
                        plan_id=plan.id,
                        status=plan.status,
                        steps=len(stored_steps),
                        goal=goal_text,
                        days=parsed.days,
                        time=parsed.time_str,
                        tasks=parsed.tasks_per_day,
                    )
                ),
            )
        )
        db.commit()


def _extract_plan_id(data: str) -> Optional[int]:
    try:
        return int(data.split(":")[-1])
    except Exception:
        return None


@router.callback_query(F.data.startswith("plan:approve:"))
async def cb_plan_approve(c: CallbackQuery):
    plan_id = _extract_plan_id(c.data)
    if not plan_id:
        await c.answer("Не вдалося знайти план.", show_alert=True)
        return

    with SessionLocal() as db:
        plan = db.query(AIPlan).filter(AIPlan.id == plan_id).first()
        if not plan:
            await c.answer("План не знайдено.", show_alert=True)
            return

        user = db.query(User).filter(User.id == plan.user_id).first()
        if not user or user.tg_id != c.from_user.id:
            await c.answer("Немає доступу до цього плану.", show_alert=True)
            return

        tz_name = user.timezone or "Europe/Kyiv"
        now_utc = datetime.now(pytz.UTC)
        scheduled = 0

        # затверджуємо усі pending кроки -> approved, ставимо scheduled_for (з proposed_for або +1 хв)
        for step in plan.steps:
            if step.is_completed:
                continue
            if step.status in {"completed", "canceled"}:
                continue

            scheduled_for_utc = step.proposed_for or (now_utc + timedelta(minutes=1))
            if scheduled_for_utc <= now_utc:
                scheduled_for_utc = now_utc + timedelta(minutes=1)

            step.scheduled_for = scheduled_for_utc
            step.status = "approved"

            # створюємо/оновлюємо job
            schedule_plan_step(step, user)
            scheduled += 1

        if plan.status in {"draft", "pending"}:
            plan.status = "active"
            plan.approved_at = now_utc

        db.add(
            Response(
                delivery_id=None,
                user_id=user.id,
                kind="plan_action",
                payload=f"plan_id={plan.id};action=approve;status={plan.status};scheduled={scheduled}",
            )
        )
        db.commit()

        message_text = _format_plan_message(plan, list(plan.steps), tz_name)
        keyboard = _plan_keyboard(plan)

    try:
        await c.message.edit_text(message_text, reply_markup=keyboard)
    except Exception:
        await c.message.answer(message_text)
    await c.answer("✅ План затверджено!")


@router.callback_query(F.data.startswith("plan:cancel:"))
async def cb_plan_cancel(c: CallbackQuery, state: FSMContext):
    plan_id = _extract_plan_id(c.data)
    if not plan_id:
        await c.answer("Не вдалося знайти план.", show_alert=True)
        return

    with SessionLocal() as db:
        plan = db.query(AIPlan).filter(AIPlan.id == plan_id).first()
        if not plan:
            await c.answer("План не знайдено.", show_alert=True)
            return

        user = db.query(User).filter(User.id == plan.user_id).first()
        if not user or user.tg_id != c.from_user.id:
            await c.answer("Немає доступу до цього плану.", show_alert=True)
            return

        tz_name = user.timezone or "Europe/Kyiv"
        removed = 0

        for step in plan.steps:
            if step.job_id:
                remove_job(step.job_id)
                removed += 1
            step.job_id = None
            if step.status != "completed":
                step.status = "canceled"
            step.scheduled_for = None
            step.is_completed = False
            step.completed_at = None

        plan.status = "canceled"
        plan.completed_at = datetime.now(pytz.UTC)

        db.add(
            Response(
                delivery_id=None,
                user_id=user.id,
                kind="plan_action",
                payload=f"plan_id={plan.id};action=cancel;removed={removed}",
            )
        )
        db.commit()

        message_text = _format_plan_message(plan, list(plan.steps), tz_name)
        keyboard = _plan_keyboard(plan)

    try:
        await c.message.edit_text(message_text, reply_markup=keyboard)
    except Exception:
        await c.message.answer(message_text)
    await state.clear()
    await c.answer("❌ План скасовано")


@router.callback_query(F.data.startswith("plan:change_hour:"))
async def cb_plan_change_hour(c: CallbackQuery, state: FSMContext):
    plan_id = _extract_plan_id(c.data)
    if not plan_id:
        await c.answer("Некоректний план.", show_alert=True)
        return

    with SessionLocal() as db:
        user = db.scalars(select(User).where(User.tg_id == c.from_user.id)).first()
        if not user:
            await c.answer("Натисни /start", show_alert=True)
            return

        plan = db.query(AIPlan).filter(AIPlan.id == plan_id, AIPlan.user_id == user.id).first()
        if not plan:
            await c.answer("План не знайдено", show_alert=True)
            return

    await state.clear()
    await state.set_state(PlanStates.waiting_new_hour)
    await state.update_data(plan_id=plan_id)

    await c.message.answer("Надішли нову годину у форматі HH:MM для всіх кроків плану.")
    await c.answer()

# ----------------- /plan_status /plan_pause /plan_resume /plan_cancel -----------------

def _format_plan_status(plan: AIPlan, steps: list[AIPlanStep], user: User) -> str:
    tz = pytz.timezone(user.timezone or "Europe/Kyiv")
    total = len(steps)
    completed = sum(1 for step in steps if step.is_completed or step.status == "completed")
    pending = sum(1 for step in steps if not step.is_completed and step.status == "pending")

    upcoming = [
        s for s in steps
        if not s.is_completed and s.status not in {"completed", "canceled"}
    ]
    next_step = None
    if upcoming:
        def _key(s: AIPlanStep):
            return s.scheduled_for or dtmod.datetime.max.replace(tzinfo=pytz.UTC)
        next_step = min(upcoming, key=_key)

    lines = [f"План: {_escape(plan.name or '')}", f"Статус: {_escape(plan.status or '')}"]
    if total:
        lines.append(f"Прогрес: {completed}/{total} кроків виконано.")
    else:
        lines.append("Прогрес: у плані ще немає кроків.")
    if pending:
        lines.append(f"На погодження: {pending} крок(и).")
    if next_step and next_step.scheduled_for:
        next_local = next_step.scheduled_for.astimezone(tz)
        preview = (next_step.message or "").strip().split("\n", 1)[0]
        if len(preview) > 120:
            preview = preview[:117] + "..."
        status_hint = f" [{_escape(next_step.status)}]" if next_step.status else ""
        lines.append(
            f"Наступний крок{status_hint}: {next_local.strftime('%Y-%m-%d %H:%M %Z')} — {_escape(preview)}"
        )
    else:
        lines.append("Наступний крок: відсутній.")

    lines.append("")
    sorted_steps = sorted(
        steps,
        key=lambda s: (
            getattr(s, "day_index", None) if getattr(s, "day_index", None) is not None else getattr(s, "day", 0) - 1,
            getattr(s, "slot_index", 0),
            s.scheduled_for or s.proposed_for or datetime.max.replace(tzinfo=pytz.UTC),
        ),
    )
    current_day = None
    for step in sorted_steps:
        day_idx = getattr(step, "day_index", None)
        day_number = (day_idx + 1) if day_idx is not None else getattr(step, "day", None) or 1
        if current_day != day_number:
            if current_day is not None:
                lines.append("")
            lines.append(f"День {day_number}")
            current_day = day_number

        dt_source = step.scheduled_for or step.proposed_for
        when_str = "?"
        if dt_source:
            when_str = dt_source.astimezone(tz).strftime("%H:%M")
        elif getattr(step, "time", None):
            when_str = str(getattr(step, "time"))
        status_text = _escape(step.status or "pending")
        message_text = _escape(step.message or "")
        lines.append(f" • {when_str} [{status_text}] — {message_text}")

    return "\n".join(lines)


def _remove_future_plan_jobs(steps: list[AIPlanStep]):
    now_utc = datetime.now(pytz.UTC)
    for step in steps:
        if step.job_id and step.scheduled_for and step.scheduled_for > now_utc:
            remove_job(step.job_id)
            step.job_id = None


@router.message(Command("plan_status"))
async def cmd_plan_status(m: Message):
    with SessionLocal() as db:
        u = db.scalars(select(User).where(User.tg_id == m.from_user.id)).first()
        if not u:
            await m.answer("Натисніть /start")
            return

        plan = _get_latest_plan(db, u.id, ("active", "paused", "pending"))
        if not plan:
            await m.answer("Активних планів немає.")
            return

        steps = (
            db.query(AIPlanStep)
            .filter(AIPlanStep.plan_id == plan.id)
            .order_by(
                AIPlanStep.day_index,
                AIPlanStep.slot_index,
                AIPlanStep.scheduled_for,
                AIPlanStep.proposed_for,
            )
            .all()
        )
        await m.answer(_format_plan_status(plan, steps, u))


@router.message(Command("plan_pause"))
async def cmd_plan_pause(m: Message):
    with SessionLocal() as db:
        u = db.scalars(select(User).where(User.tg_id == m.from_user.id)).first()
        if not u:
            await m.answer("Натисніть /start")
            return

        plan = _get_latest_plan(db, u.id, ("active",))
        if not plan:
            await m.answer("Немає активного плану для паузи.")
            return

        steps = (
            db.query(AIPlanStep)
            .filter(AIPlanStep.plan_id == plan.id, AIPlanStep.is_completed == False)
            .all()
        )
        _remove_future_plan_jobs(steps)
        plan.status = "paused"
        db.commit()

    await m.answer("План призупинено. Майбутні повідомлення зупинено.")


@router.message(Command("plan_resume"))
async def cmd_plan_resume(m: Message):
    with SessionLocal() as db:
        u = db.scalars(select(User).where(User.tg_id == m.from_user.id)).first()
        if not u:
            await m.answer("Натисніть /start")
            return

        plan = _get_latest_plan(db, u.id, ("paused", "pending"))
        if not plan:
            await m.answer("Немає плану, який можна відновити.")
            return

        steps = (
            db.query(AIPlanStep)
            .filter(AIPlanStep.plan_id == plan.id, AIPlanStep.is_completed == False)
            .all()
        )
        for step in steps:
            if step.status == "pending":
                step.status = "approved"
            if step.status != "approved":
                continue
            schedule_plan_step(step, u)

        plan.status = "active"
        db.commit()

    await m.answer("План відновлено. Майбутні кроки повторно заплановано.")


@router.message(Command("plan_cancel"))
async def cmd_plan_cancel_cmd(m: Message, state: FSMContext):
    with SessionLocal() as db:
        u = db.scalars(select(User).where(User.tg_id == m.from_user.id)).first()
        if not u:
            await m.answer("Натисніть /start")
            return

        plan = _get_latest_plan(db, u.id, ("active", "paused", "pending"))
        if not plan:
            await m.answer("Немає плану для завершення.")
            return

        steps = (
            db.query(AIPlanStep)
            .filter(AIPlanStep.plan_id == plan.id, AIPlanStep.is_completed == False)
            .all()
        )
        _remove_future_plan_jobs(steps)
        for step in steps:
            if step.status != "completed":
                step.status = "canceled"
            step.job_id = None

        plan.status = "canceled"
        plan.completed_at = datetime.now(pytz.UTC)

        db.commit()

    await state.clear()
    await m.answer("План завершено і всі майбутні повідомлення скасовано.")

# ----------------- нагадування -----------------

def parse_natural_time(text: str, user_tz: str = "Europe/Kyiv"):
    # повертає datetime у UTC або None
    now_local = dtmod.datetime.now(pytz.timezone(user_tz))
    dt_local, status = pdt_calendar.parseDT(text, sourceTime=now_local)
    if status == 0:
        return None
    return dt_local.astimezone(pytz.UTC)


@router.message(Command("remind"))
async def cmd_remind(m: Message):
    # формат: /remind <час> | <повідомлення>
    args = m.text.split(maxsplit=1)
    if len(args) < 2:
        await m.answer("Використання: /remind <час> | <повідомлення>\nНапр.: /remind завтра о 09:00 | важлива зустріч")
        return
    payload = args[1]
    if "|" in payload:
        time_part, text = [s.strip() for s in payload.split("|", 1)]
    else:
        time_part, text = payload, "Нагадування"

    with SessionLocal() as db:
        u = db.scalars(select(User).where(User.tg_id == m.from_user.id)).first()
        if not u:
            await m.answer("Натисніть /start")
            return

        user_tz = u.timezone or "Europe/Kyiv"
        dt_utc = parse_natural_time(time_part, user_tz)
        if not dt_utc:
            await m.answer("Не зрозумів час. Спробуй: 'завтра о 9:00' або 'через 2 години', або формат 'час | текст'.")
            return

        job_id = UserReminder.generate_job_id(u.id)
        reminder = UserReminder(
            user_id=u.id,
            job_id=job_id,
            message=text,
            scheduled_at=dt_utc,
            timezone=user_tz,
            active=True,
        )
        db.add(reminder)
        db.commit()
        db.refresh(reminder)

    schedule_custom_reminder(reminder)
    scheduled_local = dt_utc.astimezone(pytz.timezone(user_tz))
    scheduled_str = scheduled_local.strftime('%Y-%m-%d %H:%M %Z')
    await m.answer(
        f"Нагадування заплановано на {_escape(scheduled_str)} (job_id={_escape(job_id)})"
    )


@router.message(Command("my_reminders"))
async def cmd_my_reminders(m: Message):
    with SessionLocal() as db:
        u = db.scalars(select(User).where(User.tg_id == m.from_user.id)).first()
        if not u:
            await m.answer("Натисніть /start")
            return

        rs = db.query(UserReminder).filter(UserReminder.user_id == u.id, UserReminder.active == True).all()
        if not rs:
            await m.answer("У вас немає активних нагадувань.")
            return

        text = "Ваші нагадування:\n\n"
        for r in rs:
            when = (
                r.scheduled_at.astimezone(pytz.timezone(u.timezone or "Europe/Kyiv")).strftime('%Y-%m-%d %H:%M')
                if r.scheduled_at else (r.cron_expression or "?")
            )
            job_display = r.job_id or "?"
            message_display = r.message or ""
            text += (
                f"- id:{r.id} job:{_escape(job_display)} коли:{_escape(when)} текст:{_escape(message_display)}\n"
            )
        await m.answer(text)

# ----------------- inline кнопки для Q&A -----------------

@router.callback_query(F.data.in_(["fb:up", "fb:down"]))
async def cb_fb(c: CallbackQuery):
    with SessionLocal() as db:
        u = db.scalars(select(User).where(User.tg_id == c.from_user.id)).first()
        if u:
            db.add(Response(delivery_id=None, user_id=u.id, kind="button", payload=c.data))
            db.commit()
    await c.answer("Дякую!")


@router.callback_query(F.data == "ask:init")
async def cb_ask(c: CallbackQuery):
    await c.message.answer("Напиши питання наступним повідомленням.")
    await c.answer()


@router.callback_query(F.data == "ui:settings")
async def cb_ui_settings(c: CallbackQuery):
    await c.answer()
    await c.message.answer(
        "Я занотував, що хочеш змінити налаштування. Це скоро буде доступно прямо в боті."
    )


@router.callback_query(F.data == "ui:plan_adjustment")
async def cb_ui_plan_adjustment(c: CallbackQuery):
    await c.answer()
    await c.message.answer(
        "Я врахую, що план треба підлаштувати. Скоро додамо зручний вибір складності."
    )

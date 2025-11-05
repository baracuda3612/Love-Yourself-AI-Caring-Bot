# app/telegram.py
# Версія з підтримкою чернеток, підтвердження плану і керування нагадуваннями

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from sqlalchemy import select
from datetime import datetime, timedelta
import datetime as dtmod
import pytz
import traceback
import parsedatetime as pdt
from typing import List, Optional

from app.config import BOT_TOKEN, ADMIN_IDS, DEFAULT_DAILY_LIMIT
from app.db import (
    SessionLocal, User, Response, UsageCounter,
    UserReminder, AIPlan, AIPlanStep, UserMemoryProfile
)
from app.ai import answer_user_question, generate_daily_message
from app.scheduler import add_job, remove_job, schedule_custom_reminder, send_scheduled_message
from app.ai_plans import generate_ai_plan

# ----------------- базові речі -----------------

bot = Bot(token=BOT_TOKEN, parse_mode="HTML")
dp = Dispatcher()
router = Router()
dp.include_router(router)

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def today_str(tz: str = "Europe/Kyiv") -> str:
    import pytz, datetime as dt
    return dt.datetime.now(pytz.timezone(tz)).strftime("%Y-%m-%d")

def month_str(tz: str = "Europe/Kyiv") -> str:
    import pytz, datetime as dt
    return dt.datetime.now(pytz.timezone(tz)).strftime("%Y-%m")

# ----------------- службові -----------------

@router.message(Command("ping"))
async def cmd_ping(m: Message):
    await m.answer("pong")

# ----------------- старт / help / ліміт -----------------

@router.message(Command("start"))
async def cmd_start(m: Message):
    with SessionLocal() as db:
        u = db.scalars(select(User).where(User.tg_id == m.from_user.id)).first()
        if not u:
            u = User(
                tg_id=m.from_user.id,
                first_name=m.from_user.first_name or "",
                username=m.from_user.username or "",
                daily_limit=DEFAULT_DAILY_LIMIT,
                send_hour=9,
            )
            db.add(u)
            db.commit()
        await m.answer(
            "Привіт! Я wellbeing-бот Love Yourself 🌿\n"
            "Щодня надсилатиму коротке повідомлення для самопідтримки.\n"
            "Використай /plan щоб створити план, або /ask щоб поставити питання."
        )

@router.message(Command("help"))
async def cmd_help(m: Message):
    await m.answer(
        "/ask — поставити питання\n"
        "/limit — перевірити ліміт\n"
        "/plan <опис> — створити AI-план\n"
        "/remind <час | текст> — створити нагадування"
    )

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

@router.message(F.text & ~F.via_bot)
async def on_text(m: Message):
    # обробка звичайного тексту як запитання до AI
    with SessionLocal() as db:
        u = db.scalars(select(User).where(User.tg_id == m.from_user.id)).first()
        if not u:
            await m.answer("Натисни /start")
            return

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
            return

        try:
            text, usage = answer_user_question(
                f"{u.first_name or ''} @{u.username or ''}",
                m.text,
                u.prompt_template
            )
        except Exception as e:
            print("=== GENERATION ERROR ===\n", traceback.format_exc())
            await m.answer(f"ERR [{e.__class__.__name__}]: {e}")
            return

        await m.answer(text)

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

def _format_plan_message(plan: AIPlan, steps: List[AIPlanStep], tz_name: Optional[str]) -> str:
    tz = _get_timezone(tz_name)
    lines = [
        f"План: {plan.name}",
        f"Статус: {plan.status}",
        ""
    ]
    for i, s in enumerate(steps[:PLAN_PREVIEW_STEP_LIMIT], 1):
        when = s.proposed_for or s.scheduled_for
        when_str = when.astimezone(tz).strftime('%H:%M %d-%m') if when else "?"
        lines.append(f"{i}. [{s.status}] {when_str} — {s.message}")
    return "\n".join(lines)

def _plan_keyboard(plan: AIPlan):
    if plan.status == "draft":
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Затвердити", callback_data=f"plan:approve:{plan.id}")],
            [InlineKeyboardButton(text="🕘 Змінити час", callback_data=f"plan:change:{plan.id}")],
            [InlineKeyboardButton(text="❌ Скасувати", callback_data=f"plan:cancel:{plan.id}")]
        ])
    elif plan.status == "active":
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Скасувати", callback_data=f"plan:cancel:{plan.id}")]
        ])
    return None

@router.message(Command("plan"))
async def cmd_plan(m: Message):
    args = m.text.split(maxsplit=1)
    if len(args) < 2:
        await m.answer("Використання: /plan <опис> (наприклад: план покращення сну)")
        return
    plan_prompt = args[1]

    with SessionLocal() as db:
        user = db.scalars(select(User).where(User.tg_id == m.from_user.id)).first()
        if not user:
            await m.answer("Натисни /start")
            return

        mp = db.query(UserMemoryProfile).filter(UserMemoryProfile.user_id == user.id).first()

        try:
            plan_name, steps = generate_ai_plan(
                plan_prompt,
                mp.profile_data if mp else None,
                timezone=user.timezone or "Europe/Kyiv",
            )
        except Exception as e:
            await m.answer(f"Помилка генерації плану: {e}")
            return

        # створюємо чернетку
        plan = AIPlan(
            user_id=user.id,
            name=plan_name,
            description=plan_prompt,
            status="draft"
        )
        db.add(plan)
        db.flush()

        stored_steps = []
        for s in steps:
            msg = s.get("message")
            when = s.get("scheduled_for")
            if not msg:
                continue
            if isinstance(when, (datetime, dtmod.datetime)) and when.tzinfo is None:
                when = pytz.timezone(user.timezone or "Europe/Kyiv").localize(when)
            step = AIPlanStep(
                plan_id=plan.id,
                message=msg,
                proposed_for=when.astimezone(pytz.UTC) if when else None,
                status="pending"
            )
            db.add(step)
            stored_steps.append(step)

        db.commit()

        preview = _format_plan_message(plan, stored_steps, user.timezone)
        kb = _plan_keyboard(plan)
        await m.answer(preview, reply_markup=kb)

# ----------------- Кнопки керування планом -----------------

def _extract_plan_id(data: str) -> Optional[int]:
    try:
        return int(data.split(":")[-1])
    except Exception:
        return None

@router.callback_query(F.data.startswith("plan:approve:"))
async def cb_plan_approve(c: CallbackQuery):
    plan_id = _extract_plan_id(c.data)
    if not plan_id:
        await c.answer("Не знайдено план.")
        return

    with SessionLocal() as db:
        plan = db.query(AIPlan).filter(AIPlan.id == plan_id).first()
        user = db.query(User).filter(User.id == plan.user_id).first()
        if not plan or not user:
            await c.answer("План не знайдено.")
            return

        now_utc = datetime.now(pytz.UTC)
        for step in plan.steps:
            when = step.proposed_for or now_utc + timedelta(minutes=1)
            job_id = AIPlanStep.generate_job_id(user.id, plan.id)
            add_job(
                send_scheduled_message,
                'date',
                id=job_id,
                run_date=when,
                args=[user.tg_id, step.message],
                replace_existing=True,
            )
            step.job_id = job_id
            step.scheduled_for = when
            step.status = "approved"
        plan.status = "active"
        plan.approved_at = now_utc
        db.commit()

        msg = _format_plan_message(plan, plan.steps, user.timezone)
        kb = _plan_keyboard(plan)
    await c.message.edit_text(msg, reply_markup=kb)
    await c.answer("✅ План затверджено!")

@router.callback_query(F.data.startswith("plan:cancel:"))
async def cb_plan_cancel(c: CallbackQuery):
    plan_id = _extract_plan_id(c.data)
    if not plan_id:
        await c.answer("Не знайдено план.")
        return

    with SessionLocal() as db:
        plan = db.query(AIPlan).filter(AIPlan.id == plan_id).first()
        user = db.query(User).filter(User.id == plan.user_id).first()
        if not plan or not user:
            await c.answer("План не знайдено.")
            return
        for step in plan.steps:
            if step.job_id:
                remove_job(step.job_id)
            step.status = "canceled"
        plan.status = "canceled"
        db.commit()

        msg = _format_plan_message(plan, plan.steps, user.timezone)
        kb = _plan_keyboard(plan)
    await c.message.edit_text(msg, reply_markup=kb)
    await c.answer("❌ План скасовано")

@router.callback_query(F.data.startswith("plan:change:"))
async def cb_plan_change(c: CallbackQuery):
    await c.answer("🕘 Напиши, коли хочеш отримувати повідомлення (наприклад: о 9:00 або ввечері).")
    await c.message.answer("Функція редагування часу поки в розробці 🧠")


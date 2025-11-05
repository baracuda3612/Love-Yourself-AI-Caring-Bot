# app/telegram.py

from aiogram import Bot, Dispatcher, F, Router, types
from aiogram.filters import Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery

from sqlalchemy import select
from datetime import datetime, timedelta
import datetime as dtmod
import pytz
import traceback
import parsedatetime as pdt

from app.config import BOT_TOKEN, ADMIN_IDS, DEFAULT_DAILY_LIMIT
from app.db import (
    SessionLocal, User, Response, UsageCounter,
    UserReminder, AIPlan, AIPlanStep, UserMemoryProfile
)
from app.ai import answer_user_question, generate_daily_message
from app.scheduler import add_job, schedule_custom_reminder, send_scheduled_message
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

# ----------------- службові / діагностика -----------------

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
            "Привіт! Я wellbeing-бот Love Yourself.\n"
            "Щоденно надсилатиму коротке корисне повідомлення.\n"
            "Натисни 'Поставити питання', щоб отримати AI-відповідь (є ліміт на день)."
        )

@router.message(Command("help"))
async def cmd_help(m: Message):
    await m.answer("/ask — поставити питання\n/limit — залишок ліміту\n/plan <опис> — згенерувати план\n/remind <час | текст> — нагадування")

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
        await m.answer(f"Залишилось: {max(0, (u.daily_limit or 10) - used)} з {u.daily_limit or 10}")

# ----------------- Q&A -----------------

@router.message(Command("ask"))
async def cmd_ask(m: Message):
    await m.answer("Напиши питання наступним повідомленням.")

@router.message(F.text & ~F.via_bot)
async def on_text(m: Message):
    # якщо це відповідь після /ask — обробляємо як питання до AI
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

        # update counters + лог відповіді
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

# ----------------- feedback кнопки -----------------

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

# ----------------- нагадування -----------------

pdt_calendar = pdt.Calendar()

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
    await m.answer(f"Нагадування заплановано на {scheduled_local.strftime('%Y-%m-%d %H:%M %Z')} (job_id={job_id})")

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
            text += f"- id:{r.id} job:{r.job_id} коли:{when} текст:{r.message}\n"
        await m.answer(text)

# ----------------- AI-план -----------------

@router.message(Command("plan"))
async def cmd_plan(m: Message):
    # /plan <опис плану>
    args = m.text.split(maxsplit=1)
    if len(args) < 2:
        await m.answer("Використання: /plan <опис> (напр.: план покращення сну на 30 днів)")
        return
    plan_prompt = args[1]

    with SessionLocal() as db:
        u = db.scalars(select(User).where(User.tg_id == m.from_user.id)).first()
        if not u:
            await m.answer("Натисніть /start")
            return

        mp = db.query(UserMemoryProfile).filter(UserMemoryProfile.user_id == u.id).first()

        try:
            plan_name, steps = generate_ai_plan(
                plan_prompt,
                mp.profile_data if mp else None,
                timezone=u.timezone or "Europe/Kyiv",
            )
        except Exception as e:
            print("=== PLAN GENERATION ERROR ===\n", traceback.format_exc())
            await m.answer(f"ERR плану [{e.__class__.__name__}]: {e}")
            return

        if not steps:
            await m.answer("Не вдалося сформувати план. Спробуй уточнити запит.")
            return

        plan = AIPlan(user_id=u.id, name=plan_name, description=plan_prompt, status="active")
        db.add(plan)
        db.commit()
        db.refresh(plan)

        scheduled_count = 0
        now_utc = datetime.now(pytz.UTC)

        for s in steps:
            scheduled_local = s.get("scheduled_for")
            msg = s.get("message")
            if not msg:
                continue

            if isinstance(scheduled_local, (datetime, dtmod.datetime)):
                scheduled_for_utc = scheduled_local.astimezone(pytz.UTC)
            else:
                # якщо прийшов рядок — проігноруємо або зсунемо на +1 хв
                scheduled_for_utc = now_utc + timedelta(minutes=1)

            if scheduled_for_utc <= now_utc:
                scheduled_for_utc = now_utc + timedelta(minutes=1)

            step_status = (s.get("status") or "approved").strip().lower()
            if step_status not in {"approved", "pending", "canceled"}:
                step_status = "pending"
            job_id = None
            if step_status == "approved" and plan.status != "paused":
                job_id = AIPlanStep.generate_job_id(u.id, plan.id)
                # плановий one-shot
                add_job(
                    send_scheduled_message,
                    'date',
                    id=job_id,
                    run_date=scheduled_for_utc,
                    args=[u.tg_id, msg],
                    replace_existing=True,
                )

            step = AIPlanStep(
                plan_id=plan.id,
                job_id=job_id,
                status=step_status,
                message=msg,
                scheduled_for=scheduled_for_utc,
                is_completed=False
            )
            db.add(step)
            scheduled_count += 1

        db.commit()
        await m.answer(f"План '{plan_name}' створено. Заплановано {scheduled_count} повідомлень.")

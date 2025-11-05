# app/telegram.py

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
PLAN_PREVIEW_STEP_LIMIT = 3


def _get_timezone(tz_name: Optional[str]) -> pytz.BaseTzInfo:
    try:
        return pytz.timezone(tz_name or "Europe/Kyiv")
    except pytz.UnknownTimeZoneError:
        return pytz.timezone("Europe/Kyiv")


def _format_plan_message(plan: AIPlan, steps: List[AIPlanStep], tz_name: Optional[str], *, limit: Optional[int] = None, note: Optional[str] = None) -> str:
    tz = _get_timezone(tz_name)
    lines: List[str] = [
        f"План: {plan.name}",
        f"Статус: {plan.status}",
    ]
    if plan.approved_at:
        lines.append(f"Затверджено: {plan.approved_at.astimezone(tz).strftime('%Y-%m-%d %H:%M')}")
    if note:
        lines.append("")
        lines.append(note)

    lines.append("")

    display_steps = steps if limit is None else steps[:limit]
    for idx, step in enumerate(display_steps, 1):
        dt_source = step.proposed_for or step.scheduled_for
        when_str = "?"
        if dt_source:
            dt_local = dt_source.astimezone(tz)
            when_str = dt_local.strftime("%Y-%m-%d %H:%M")
        lines.append(f"{idx}. [{step.status}] {when_str}\n{step.message}")

    total_steps = len(steps)
    if limit is not None and total_steps > limit:
        lines.append("")
        lines.append(f"Показано перші {limit} кроки з {total_steps}.")

    return "\n".join(lines).strip()


def _plan_keyboard(plan: AIPlan) -> Optional[InlineKeyboardMarkup]:
    if plan.status == "draft":
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ Затвердити", callback_data=f"plan:approve:{plan.id}"),
                    InlineKeyboardButton(text="🕘 Змінити час", callback_data=f"plan:change_hour:{plan.id}"),
                ],
                [
                    InlineKeyboardButton(text="❌ Скасувати", callback_data=f"plan:cancel:{plan.id}"),
                ],
            ]
        )
    if plan.status == "active":
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="❌ Скасувати", callback_data=f"plan:cancel:{plan.id}")],
            ]
        )
    return None


def _extract_plan_id(data: Optional[str]) -> Optional[int]:
    if not data:
        return None
    try:
        return int(data.split(":")[-1])
    except (ValueError, AttributeError):
        return None

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

        plan = AIPlan(
            user_id=u.id,
            name=plan_name,
            description=plan_prompt,
            status="draft",
            approved_at=None,
        )
        db.add(plan)
        db.flush()

        stored_steps: List[AIPlanStep] = []
        for s in steps:
            scheduled_local = s.get("scheduled_for")
            msg = s.get("message")
            if not msg:
                continue

            if not isinstance(scheduled_local, (datetime, dtmod.datetime)):
                continue

            step = AIPlanStep(
                plan_id=plan.id,
                job_id=None,
                message=msg,
                status="pending",
                proposed_for=scheduled_local,
                scheduled_for=None,
                is_completed=False,
            )
            plan.steps.append(step)
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

        db.add(
            Response(
                delivery_id=None,
                user_id=u.id,
                kind="plan_preview",
                payload=f"plan_id={plan.id};status={plan.status};steps={len(stored_steps)}",
            )
        )
        db.commit()


@router.callback_query(F.data.startswith("plan:approve:"))
async def cb_plan_approve(c: CallbackQuery):
    plan_id = _extract_plan_id(c.data)
    if not plan_id:
        await c.answer("Не вдалося знайти план.", show_alert=True)
        return

    message_text = None
    keyboard = None
    alert_text = "План затверджено!"

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

        if plan.status == "draft":
            for step in plan.steps:
                proposed = step.proposed_for or datetime.now(_get_timezone(tz_name))
                if proposed.tzinfo is None:
                    proposed = _get_timezone(tz_name).localize(proposed)
                scheduled_for_utc = proposed.astimezone(pytz.UTC)
                if scheduled_for_utc <= now_utc:
                    scheduled_for_utc = now_utc + timedelta(minutes=1)

                job_id = AIPlanStep.generate_job_id(user.id, plan.id)
                add_job(
                    send_scheduled_message,
                    'date',
                    id=job_id,
                    run_date=scheduled_for_utc,
                    args=[user.tg_id, step.message],
                    replace_existing=True,
                )

                step.job_id = job_id
                step.scheduled_for = scheduled_for_utc
                step.status = "approved"
                step.is_completed = False
                step.completed_at = None
                scheduled += 1

            plan.status = "active"
            plan.approved_at = now_utc
            plan.completed_at = None
        else:
            alert_text = "План уже оброблено."

        db.add(
            Response(
                delivery_id=None,
                user_id=user.id,
                kind="plan_action",
                payload=f"plan_id={plan.id};action=approve;status={plan.status};scheduled={scheduled}",
            )
        )
        db.commit()

        message_text = _format_plan_message(
            plan,
            list(plan.steps),
            tz_name,
            limit=PLAN_PREVIEW_STEP_LIMIT,
        )
        keyboard = _plan_keyboard(plan)

    try:
        await c.message.edit_text(message_text, reply_markup=keyboard)
    except Exception:
        await c.message.answer(message_text)
    await c.answer(alert_text)


@router.callback_query(F.data.startswith("plan:cancel:"))
async def cb_plan_cancel(c: CallbackQuery):
    plan_id = _extract_plan_id(c.data)
    if not plan_id:
        await c.answer("Не вдалося знайти план.", show_alert=True)
        return

    message_text = None
    keyboard = None

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

        message_text = _format_plan_message(
            plan,
            list(plan.steps),
            tz_name,
            limit=PLAN_PREVIEW_STEP_LIMIT,
        )
        keyboard = _plan_keyboard(plan)

    try:
        await c.message.edit_text(message_text, reply_markup=keyboard)
    except Exception:
        await c.message.answer(message_text)
    await c.answer("План скасовано.")


@router.callback_query(F.data.startswith("plan:change_hour:"))
async def cb_plan_change_hour(c: CallbackQuery):
    plan_id = _extract_plan_id(c.data)
    if not plan_id:
        await c.answer("Не вдалося знайти план.", show_alert=True)
        return

    message_text = None
    keyboard = None
    note = None

    with SessionLocal() as db:
        plan = db.query(AIPlan).filter(AIPlan.id == plan_id).first()
        if not plan:
            await c.answer("План не знайдено.", show_alert=True)
            return

        user = db.query(User).filter(User.id == plan.user_id).first()
        if not user or user.tg_id != c.from_user.id:
            await c.answer("Немає доступу до цього плану.", show_alert=True)
            return

        if plan.status == "draft":
            note = "Напишіть у чаті бажаний час або деталі — ми уточнимо розклад перед затвердженням."
        else:
            note = "План уже активний. Скасуйте його та створіть новий, щоб змінити час кроків."

        db.add(
            Response(
                delivery_id=None,
                user_id=user.id,
                kind="plan_action",
                payload=f"plan_id={plan.id};action=change_hour;status={plan.status}",
            )
        )
        db.commit()

        message_text = _format_plan_message(
            plan,
            list(plan.steps),
            user.timezone or "Europe/Kyiv",
            limit=PLAN_PREVIEW_STEP_LIMIT,
            note=note,
        )
        keyboard = _plan_keyboard(plan)

    try:
        await c.message.edit_text(message_text, reply_markup=keyboard)
    except Exception:
        await c.message.answer(message_text)
    await c.answer("Добре! Чекаю на уточнення часу.")

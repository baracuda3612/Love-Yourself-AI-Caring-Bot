# app/telegram.py

from aiogram import Bot, Dispatcher, F, Router
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
from app.ai import answer_user_question
from app.scheduler import (
    remove_job,
    schedule_custom_reminder,
    schedule_plan_step,
)
from app.ai_plans import generate_ai_plan

# ----------------- базові речі -----------------

bot = Bot(token=BOT_TOKEN, parse_mode="HTML")
dp = Dispatcher()
router = Router()
dp.include_router(router)

_pending_plan_hour_change: dict[int, int] = {}

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

        if u.id in _pending_plan_hour_change:
            plan_id = _pending_plan_hour_change[u.id]
            handled = await _process_plan_hour_response(m, db, u, plan_id)
            if handled:
                _pending_plan_hour_change.pop(u.id, None)
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


async def _process_plan_hour_response(message: Message, db, user: User, plan_id: int) -> bool:
    parsed = _parse_hour_minute(message.text)
    if not parsed:
        await message.answer("Не вдалося розпізнати час. Напиши у форматі HH:MM, напр. 09:00.")
        return False

    hour, minute = parsed

    plan = (
        db.query(AIPlan)
        .filter(AIPlan.id == plan_id, AIPlan.user_id == user.id)
        .first()
    )
    if not plan:
        await message.answer("План не знайдено або вже завершений.")
        return True

    steps = (
        db.query(AIPlanStep)
        .filter(
            AIPlanStep.plan_id == plan.id,
            AIPlanStep.is_completed == False,
            (AIPlanStep.status.is_(None))
            | (AIPlanStep.status.notin_(["completed", "cancelled"])),
        )
        .all()
    )

    if not steps:
        await message.answer("У плані немає кроків для оновлення.")
        return True

    user_tz = pytz.timezone(user.timezone or "Europe/Kyiv")
    now_local = dtmod.datetime.now(user_tz)

    updated_any = False

    for step in steps:
        local_dt = step.scheduled_for.astimezone(user_tz)
        new_local = local_dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if new_local <= now_local:
            new_local += timedelta(days=1)
        new_utc = new_local.astimezone(pytz.UTC)
        step.scheduled_for = new_utc
        step.proposed_for = new_utc
        step.status = "pending"
        if step.job_id:
            remove_job(step.job_id)
            step.job_id = None
        updated_any = True

    if not updated_any:
        await message.answer("Жоден із кроків не потребував оновлення.")
        return True

    plan.status = "pending"

    db.commit()

    await message.answer(
        f"Годину плану оновлено на {hour:02d}:{minute:02d}. Кроки позначено як pending і чекають підтвердження."
    )
    return True


@router.callback_query(F.data.startswith("plan:change_hour:"))
async def cb_plan_change_hour(c: CallbackQuery):
    plan_id_str = c.data.split(":", 2)[-1]
    try:
        plan_id = int(plan_id_str)
    except ValueError:
        await c.answer("Некоректний ідентифікатор", show_alert=True)
        return

    with SessionLocal() as db:
        user = db.scalars(select(User).where(User.tg_id == c.from_user.id)).first()
        if not user:
            await c.answer("Натисни /start", show_alert=True)
            return

        plan = (
            db.query(AIPlan)
            .filter(AIPlan.id == plan_id, AIPlan.user_id == user.id)
            .first()
        )

        if not plan:
            await c.answer("План не знайдено", show_alert=True)
            return

        _pending_plan_hour_change[user.id] = plan.id

    await c.message.answer("Надішли нову годину у форматі HH:MM для всіх кроків плану.")
    await c.answer()


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

            step = AIPlanStep(
                plan_id=plan.id,
                message=msg,
                scheduled_for=scheduled_for_utc,
                proposed_for=scheduled_for_utc,
                is_completed=False,
                status="approved",
            )
            db.add(step)
            db.flush()

            schedule_plan_step(step, u)
            scheduled_count += 1

        db.commit()
        await m.answer(f"План '{plan_name}' створено. Заплановано {scheduled_count} повідомлень.")


def _format_plan_status(plan: AIPlan, steps: list[AIPlanStep], user: User) -> str:
    tz = pytz.timezone(user.timezone or "Europe/Kyiv")
    total = len(steps)
    completed = sum(1 for step in steps if step.is_completed or step.status == "completed")
    pending = sum(
        1
        for step in steps
        if not step.is_completed and step.status == "pending"
    )

    upcoming_steps = [
        step
        for step in steps
        if not step.is_completed
        and step.status not in {"completed", "cancelled"}
    ]

    next_step = None
    if upcoming_steps:
        next_step = min(
            upcoming_steps,
            key=lambda s: s.scheduled_for or dtmod.datetime.max.replace(tzinfo=pytz.UTC),
        )

    lines = [f"План: {plan.name}", f"Статус: {plan.status}"]
    if total:
        lines.append(f"Прогрес: {completed}/{total} кроків виконано.")
    else:
        lines.append("Прогрес: у плані ще немає кроків.")

    if pending:
        lines.append(f"На погодження: {pending} крок(и).")

    if next_step:
        next_local = next_step.scheduled_for.astimezone(tz)
        preview = (next_step.message or "").strip().split("\n", 1)[0]
        if len(preview) > 120:
            preview = preview[:117] + "..."
        status_hint = f" [{next_step.status}]" if next_step.status else ""
        lines.append(
            f"Наступний крок{status_hint}: {next_local.strftime('%Y-%m-%d %H:%M %Z')} — {preview}"
        )
    else:
        lines.append("Наступний крок: відсутній.")

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
            .order_by(AIPlanStep.scheduled_for)
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
async def cmd_plan_cancel(m: Message):
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
                step.status = "cancelled"
            step.job_id = None

        plan.status = "done"
        plan.completed_at = datetime.now(pytz.UTC)

        if u.id in _pending_plan_hour_change:
            _pending_plan_hour_change.pop(u.id, None)

        db.commit()

    await m.answer("План завершено і всі майбутні повідомлення скасовано.")

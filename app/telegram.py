# app/telegram.py
# Версія з підтримкою чернеток, підтвердження плану і керування нагадуваннями

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from sqlalchemy import select
from datetime import datetime, timedelta
import datetime as dtmod
import html
import pytz
import traceback
import parsedatetime as pdt
from typing import List, Optional

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
from app.plan_parser import parse_plan_request

# ----------------- базові речі -----------------

bot = Bot(token=BOT_TOKEN, parse_mode="HTML")
dp = Dispatcher()
router = Router()
dp.include_router(router)


class PlanStates(StatesGroup):
    waiting_new_hour = State()


def _escape(value) -> str:
    if value is None:
        return ""
    return html.escape(str(value))


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
        await m.answer(f"Залишилось {max(0, (u.daily_limit or 10) - used)} з {u.daily_limit or 10}")

# ----------------- Q&A -----------------

@router.message(Command("ask"))
async def cmd_ask(m: Message):
    await m.answer("Напиши питання наступним повідомленням.")

# Ігноруємо текстові команди на кшталт "/plan" в загальному обробнику
@router.message(F.text & ~F.via_bot & ~F.text.startswith("/"))
async def on_text(m: Message, state: FSMContext):
    # якщо очікуємо HH:MM для зміни часу плану — обробляємо саме це
    with SessionLocal() as db:
        u = db.scalars(select(User).where(User.tg_id == m.from_user.id)).first()
        if not u:
            await m.answer("Натисни /start")
            return

        current_state = await state.get_state()
        if current_state == PlanStates.waiting_new_hour.state:
            data = await state.get_data()
            plan_id = data.get("plan_id")
            await _process_plan_hour_response(m, state, db, u, plan_id)
            return

        # інакше — звичайний Q&A з лімітом
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
            text, _usage = answer_user_question(
                f"{u.first_name or ''} @{u.username or ''}",
                m.text,
                u.prompt_template
            )
        except Exception as e:
            print("=== GENERATION ERROR ===\n", traceback.format_exc())
            await m.answer(f"ERR [{_escape(e.__class__.__name__)}]: {_escape(str(e))}")
            return

        await m.answer(_escape(text))

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

    display_steps = steps if limit is None else steps[:limit]
    for idx, step in enumerate(display_steps, 1):
        dt_source = step.proposed_for or step.scheduled_for
        when_str = "?"
        if dt_source:
            dt_local = dt_source.astimezone(tz)
            when_str = dt_local.strftime("%Y-%m-%d %H:%M")
        status_text = _escape(step.status or "pending")
        message_text = _escape(step.message or "")
        lines.append(f"{idx}. [{status_text}] {when_str}\n{message_text}")

    total_steps = len(steps)
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
        .order_by(AIPlanStep.scheduled_for, AIPlanStep.proposed_for)
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

        try:
            plan_payload = generate_ai_plan(
                goal=parsed.goal or parsed.original_text,
                days=parsed.days,
                tasks_per_day=parsed.tasks_per_day,
                preferred_hour=parsed.time_str,
                tz_name=u.timezone or "Europe/Kyiv",
                memory=mp.profile_data if mp else None,
            )
        except Exception as e:
            await m.answer(f"Помилка генерації плану: {_escape(str(e))}")
            return

        # створюємо чернетку: кроки -> pending + proposed_for (UTC), без job_id
plan_name = None
if isinstance(plan_payload, dict):
    plan_name = plan_payload.get("plan_name")

plan = AIPlan(
    user_id=u.id,
    name=plan_name or parsed.goal or parsed.original_text or "AI План",
    description=parsed.original_text,
    status="draft",
    approved_at=None,
    goal=parsed.goal,
    duration_days=parsed.days,
    send_hour=parsed.hour,
    send_minute=parsed.minute,
    tasks_per_day=parsed.tasks_per_day,
)
db.add(plan)
db.flush()

        stored_steps: List[AIPlanStep] = []
        for s in plan_payload.get("entries", []):
            msg = s.get("message")
            when = s.get("scheduled_for")
            if not msg:
                continue
            proposed_utc = None
            if isinstance(when, (datetime, dtmod.datetime)):
                if when.tzinfo is None:
                    try:
                        user_tz = pytz.timezone(u.timezone or "Europe/Kyiv")
                    except pytz.UnknownTimeZoneError:
                        user_tz = pytz.timezone("Europe/Kyiv")
                    when = user_tz.localize(when)
                proposed_utc = when.astimezone(pytz.UTC)

            step = AIPlanStep(
                plan_id=plan.id,
                job_id=None,
                message=msg,
                status="pending",
                proposed_for=proposed_utc,
                scheduled_for=None,
                is_completed=False,
            )
            db.add(step)
            stored_steps.append(step)

        db.commit()
        db.refresh(plan)

        preview_text = _format_plan_message(plan, stored_steps, u.timezone or "Europe/Kyiv")
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

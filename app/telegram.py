
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from app.config import BOT_TOKEN, ADMIN_IDS, DEFAULT_DAILY_LIMIT
from app.db import SessionLocal, init_db, User, Response, UsageCounter
from sqlalchemy import select
from datetime import datetime
import pytz

bot = Bot(token=BOT_TOKEN, parse_mode="HTML")
dp = Dispatcher()

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def today_str(tz: str = "Europe/Kyiv") -> str:
    import pytz, datetime as dt
    return dt.datetime.now(pytz.timezone(tz)).strftime("%Y-%m-%d")

def month_str(tz: str = "Europe/Kyiv") -> str:
    import pytz, datetime as dt
    return dt.datetime.now(pytz.timezone(tz)).strftime("%Y-%m")

async def send_daily_with_buttons(bot: Bot, chat_id: int, text: str):
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="👍 Корисно", callback_data="fb:up"),
        InlineKeyboardButton(text="👎 Не дуже", callback_data="fb:down"),
    ],[
        InlineKeyboardButton(text="💬 Поставити питання", callback_data="ask:init"),
    ]])
    try:
        return await bot.send_message(chat_id, text, reply_markup=kb)
    except Exception:
        return None

@dp.message(Command("start"))
async def cmd_start(m: Message):
    with SessionLocal() as db:
        u = db.scalars(select(User).where(User.tg_id==m.from_user.id)).first()
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

@dp.message(Command("help"))
async def cmd_help(m: Message):
    await m.answer("/ask — поставити питання\n/limit — залишок ліміту")

@dp.message(Command("limit"))
async def cmd_limit(m: Message):
    with SessionLocal() as db:
        u = db.scalars(select(User).where(User.tg_id==m.from_user.id)).first()
        if not u:
            await m.answer("Натисни /start")
            return
        day = today_str(u.timezone or "Europe/Kyiv")
        cnt = db.scalars(select(UsageCounter).where(UsageCounter.user_id==u.id, UsageCounter.day==day)).first()
        used = cnt.ask_count if cnt else 0
        await m.answer(f"Залишилось: {max(0, (u.daily_limit or 10)-used)} з {u.daily_limit or 10}")

@dp.message(Command("ask"))
async def cmd_ask(m: Message):
    await m.answer("Напиши питання наступним повідомленням.")

@dp.message(F.text & ~F.via_bot)
async def on_text(m: Message):
    from app.ai import answer_user_question
    with SessionLocal() as db:
        u = db.scalars(select(User).where(User.tg_id==m.from_user.id)).first()
        if not u:
            await m.answer("Натисни /start")
            return
        day = today_str(u.timezone or "Europe/Kyiv")
        mon = month_str(u.timezone or "Europe/Kyiv")
        cnt = db.scalars(select(UsageCounter).where(UsageCounter.user_id==u.id, UsageCounter.day==day)).first()
        used = cnt.ask_count if cnt else 0
        if used >= (u.daily_limit or 10):
            await m.answer("Ліміт на сьогодні вичерпано.")
            return
        try:
            text, usage = answer_user_question(f"{u.first_name or ''} @{u.username or ''}", m.text, u.prompt_template)
        import traceback
except Exception as e:
    print("=== GENERATION ERROR ===\n", traceback.format_exc())
    await message.answer(f"ERR [{e.__class__.__name__}]: {e}")
            return
        await m.answer(text)
        # update counters
        if not cnt:
            cnt = UsageCounter(user_id=u.id, day=day, ask_count=0, month=mon, month_ask_count=0)
        cnt.ask_count += 1
        if cnt.month != mon:
            cnt.month = mon
            cnt.month_ask_count = 0
        cnt.month_ask_count += 1
        from app.db import Response
        r = Response(delivery_id=None, user_id=u.id, kind="text", payload=m.text)
        db.add(r); db.add(cnt); db.commit()

@dp.callback_query(F.data.in_(["fb:up","fb:down"]))
async def cb_fb(c: CallbackQuery):
    with SessionLocal() as db:
        u = db.scalars(select(User).where(User.tg_id==c.from_user.id)).first()
        if u:
            from app.db import Response
            db.add(Response(delivery_id=None, user_id=u.id, kind="button", payload=c.data)); db.commit()
    await c.answer("Дякую!")

@dp.callback_query(F.data == "ask:init")
async def cb_ask(c: CallbackQuery):
    await c.message.answer("Напиши питання наступним повідомленням.")
    await c.answer()
# Додайте ці функції/хендлери в кінець app/telegram.py або інтегруйте з існуючими
from aiogram.filters import Command
from aiogram.types import Message
from app.db import UserReminder, UserMemoryProfile, AIPlan, AIPlanStep, SessionLocal
from app.scheduler import add_job, schedule_custom_reminder, send_scheduled_message
from app.ai_plans import generate_ai_plan
import parsedatetime as pdt
import datetime
import pytz

pdt_calendar = pdt.Calendar()

def parse_natural_time(text: str, user_tz: str = "Europe/Kyiv"):
    # Повертає datetime у UTC або None
    now_local = datetime.datetime.now(pytz.timezone(user_tz))
    time_struct, parse_status = pdt_calendar.parseDT(text, sourceTime=now_local)
    if parse_status == 0:
        return None
    # Перетворюємо на UTC naive
    return time_struct.astimezone(pytz.UTC)

@dp.message(Command("start_memory_test"))
async def start_memory_test(m: Message):
    # Простий приклад: збір пар ключ-значення у кілька повідомлень 
    await m.answer("Почнемо короткий тест. Напишіть кілька фактів про себе у форматі 'ключ:значення'. Коли закінчите, надішліть /done_memory")
    with SessionLocal() as db:
        # зберегти маркер, що користувач у режимі опитування — реалізуйте FSM або простий флаг
        pass

@dp.message(Command("done_memory"))
async def done_memory(m: Message):
    # Тут потрібно зібрані повідомлення конвертувати в JSON і зберегти UserMemoryProfile
    await m.answer("Профіль збережено.")

@dp.message(Command("remind"))
async def cmd_remind(m: Message):
    # Приклад виклику: /remind завтра о 09:00 важлива зустріч
    args = m.get_args()
    if not args:
        await m.answer("Використання: /remind <час> <повідомлення>")
        return
    # Розділити час і текст (найпростіше: перше слово/фраза до першої лапки або до першого довгого тексту)
    # Для MVP - припустимо формат: /remind <час> | <повідомлення>
    if "|" in args:
        time_part, text = [s.strip() for s in args.split("|", 1)]
    else:
        # Якщо нема роздільника - намагаймося виділити час парсером parsedatetime
        # Спроба: шукаємо дату/час на початку рядка
        parts = args.split(" ", 3)
        time_part = parts[0] if parts else args
        text = args[len(time_part):].strip() or "Нагадування"
    with SessionLocal() as db:
        u = db.query(User).filter(User.tg_id==m.from_user.id).first()
        if not u:
            await m.answer("Натисніть /start")
            return
        user_tz = u.timezone if u.timezone else "Europe/Kyiv"
        dt_utc = parse_natural_time(time_part, user_tz)
        if not dt_utc:
            await m.answer("Не зрозумів час. Спробуйте: 'завтра о 9:00' або 'через 2 години' або використайте формат 'час | текст'.")
            return
        job_id = UserReminder.generate_job_id(u.id)
        reminder = UserReminder(
            user_id=u.id,
            job_id=job_id,
            message=text,
            scheduled_at=dt_utc,
            timezone=user_tz,
        )
        db.add(reminder)
        db.commit()
        db.refresh(reminder)

    schedule_custom_reminder(reminder)

    scheduled_local = dt_utc.astimezone(pytz.timezone(user_tz))
    await m.answer(
        f"Нагадування заплановано на {scheduled_local.strftime('%Y-%m-%d %H:%M %Z')} (job_id={job_id})"
    )

@dp.message(Command("my_reminders"))
async def cmd_my_reminders(m: Message):
    with SessionLocal() as db:
        u = db.query(User).filter(User.tg_id==m.from_user.id).first()
        if not u:
            await m.answer("Натисніть /start")
            return
        rs = db.query(UserReminder).filter(UserReminder.user_id==u.id, UserReminder.active==True).all()
        if not rs:
            await m.answer("У вас немає активних нагадувань.")
            return
        text = "Ваші нагадування:\n\n"
        for r in rs:
            when = (
                r.scheduled_at.astimezone(pytz.timezone(u.timezone)).strftime('%Y-%m-%d %H:%M')
                if r.scheduled_at
                else r.cron_expression
            )
            text += f"- id:{r.id} job:{r.job_id} коли:{when} текст:{r.message}\n"
        await m.answer(text)

@dp.message(Command("plan"))
async def cmd_plan(m: Message):
    # Використати OpenAI (викликати існуючу функцію generate_ai_plan) з системним prompt + memory_profile
    args = m.get_args()
    if not args:
        await m.answer("Використання: /plan <опис плану> (наприклад: 'план покращення сну на 30 днів')")
        return
    # 1) Отримати memory profile (якщо є)
    with SessionLocal() as db:
        u = db.query(User).filter(User.tg_id==m.from_user.id).first()
        if not u:
            await m.answer("Натисніть /start")
            return
        mp = db.query(UserMemoryProfile).filter(UserMemoryProfile.user_id==u.id).first()
        # 2) Виклик до OpenAI: generate list of steps: [{day:1, send_at: '2025-11-05 22:00', message: '...'}, ...]
        # Тут припускаємо, що є утиліта generate_ai_plan(prompt, memory_profile)
        plan_name, steps = generate_ai_plan(
            args,
            mp.profile_data if mp else None,
            timezone=u.timezone or "Europe/Kyiv",
        )
        if not steps:
            await m.answer("Не вдалося сформувати план. Спробуйте уточнити запит.")
            return

        # 3) Зберегти AIPlan і AIPlanStep та додати job-и
        plan = AIPlan(user_id=u.id, name=plan_name, description=args)
        db.add(plan)
        db.commit()
        db.refresh(plan)

        scheduled_count = 0
        now_utc = datetime.datetime.now(pytz.UTC)
        for s in steps:
            scheduled_local = s["scheduled_for"]
            if not isinstance(scheduled_local, datetime.datetime):
                continue
            scheduled_for_utc = scheduled_local.astimezone(pytz.UTC)
            if scheduled_for_utc <= now_utc:
                scheduled_for_utc = now_utc + datetime.timedelta(minutes=1)
            job_id = AIPlanStep.generate_job_id(u.id, plan.id)
            add_job(
                send_scheduled_message,
                'date',
                id=job_id,
                run_date=scheduled_for_utc,
                args=[u.tg_id, s["message"]],
                replace_existing=True,
            )
            step = AIPlanStep(
                plan_id=plan.id,
                job_id=job_id,
                message=s["message"],
                scheduled_for=scheduled_for_utc,
            )
            db.add(step)
            scheduled_count += 1
        db.commit()
        await m.answer(f"План '{plan_name}' створено та заплановано {scheduled_count} повідомлень.")


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
        except Exception:
            await m.answer("Помилка генерації. Спробуй пізніше.")
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

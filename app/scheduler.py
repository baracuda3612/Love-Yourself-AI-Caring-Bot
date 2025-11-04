import asyncio
from datetime import datetime
import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from app.db import SessionLocal, User, Delivery
from app.config import TZ, DEFAULT_SEND_HOUR
from app.ai import generate_daily_message
from app.telegram import send_daily_with_buttons, bot

tz = pytz.timezone(TZ)

async def schedule_daily_loop():
    scheduler = AsyncIOScheduler(timezone=TZ)
    # 🔔 Запуск один раз на день у задану годину (хвилина = 00)
    scheduler.add_job(check_and_schedule_deliveries, "cron",
                      hour=DEFAULT_SEND_HOUR, minute=0, id="daily_check")
    scheduler.start()

async def check_and_schedule_deliveries():
    """Один раз на день обійти активних юзерів і надіслати по одному повідомленню."""
    with SessionLocal() as db:
        users = db.scalars(select(User).where(User.active == True)).all()
        for u in users:
            await send_once(u.id)

async def send_once(user_pk: int):
    """Згенерувати і надіслати одне щоденне повідомлення користувачу."""
    from sqlalchemy import select
    with SessionLocal() as db:
        u = db.get(User, user_pk)
        if not u:
            return

        # Генерація тексту
        text, usage = generate_daily_message(
            user_profile=f"{u.first_name or ''} @{u.username or ''}",
            template_override=u.prompt_template
        )

        # Відправка
        msg = await send_daily_with_buttons(bot, u.tg_id, text)

        # Логування доставки
        now = datetime.now(pytz.timezone(u.timezone or "Europe/Kyiv"))
        d = Delivery(
            user_id=u.id,
            scheduled_for=now,
            sent_at=now,
            status="sent",
            message_id=msg.message_id if msg else None,
            prompt_snapshot=u.prompt_template,
            model="gpt-4o-mini",
            tokens_prompt=usage.get("prompt_tokens", 0),
            tokens_completion=usage.get("completion_tokens", 0),
            tokens_total=usage.get("total_tokens", 0),
        )
        db.add(d)
        db.commit()

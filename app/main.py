
import asyncio
try:
    import uvloop; uvloop.install()
except Exception:
    pass
from app.db import init_db
from app.telegram import dp, bot
from app.scheduler import schedule_daily_loop

async def main():
    init_db()
    asyncio.create_task(schedule_daily_loop())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

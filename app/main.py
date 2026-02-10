import sys
print("STDOUT TEST — I AM ALIVE", flush=True)
print("STDERR TEST — I AM ALIVE", file=sys.stderr, flush=True)
import asyncio
try:
    import uvloop; uvloop.install()
except Exception:
    pass
from app.db import audit_startup_schema, init_db
from app.telegram import dp, bot
from app.scheduler import schedule_daily_loop

async def main():
    init_db()
    audit_startup_schema()
    asyncio.create_task(schedule_daily_loop())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

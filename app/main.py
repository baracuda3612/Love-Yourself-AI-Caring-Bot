import asyncio

try:
    import uvloop

    uvloop.install()
except Exception:
    pass

import uvicorn

from app.api import app
from app.db import audit_startup_schema, init_db
from app.scheduler import schedule_daily_loop
from app.telegram import bot, dp


async def main() -> None:
    init_db()
    audit_startup_schema()
    asyncio.create_task(schedule_daily_loop())

    config = uvicorn.Config(app, host="0.0.0.0", port=8000, log_level="info")
    server = uvicorn.Server(config)

    await asyncio.gather(dp.start_polling(bot), server.serve())


if __name__ == "__main__":
    asyncio.run(main())

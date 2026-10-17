import asyncio

try:
    import uvloop

    uvloop.install()
except Exception:
    pass

import uvicorn

from app.api import app
from app.config import settings
from app.db import audit_startup_schema
from app.scheduler import schedule_daily_loop
from app.telegram import bot, dp


async def main() -> None:
    audit_startup_schema()
    from app.access_environment import audit_environment
    from app.db import SessionLocal
    with SessionLocal() as db:
        audit_environment(db)
    asyncio.create_task(schedule_daily_loop())

    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=settings.PORT,
        log_level="info",
        access_log=False,  # Enrollment callback codes must never enter URL logs.
    )
    server = uvicorn.Server(config)

    await asyncio.gather(dp.start_polling(bot), server.serve())


if __name__ == "__main__":
    asyncio.run(main())

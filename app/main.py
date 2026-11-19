from app.logging.privacy_logging import install_privacy_logging
install_privacy_logging()

import asyncio
from concurrent.futures import ThreadPoolExecutor

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
    if not settings.IS_DEV:
        from app.access_environment import audit_environment
        from app.db import SessionLocal
        with SessionLocal() as db:
            audit_environment(db)
    asyncio.get_running_loop().set_default_executor(
        ThreadPoolExecutor(max_workers=4, thread_name_prefix="runtime-io"),
    )
    asyncio.create_task(schedule_daily_loop())

    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=settings.PORT,
        log_level="info",
        access_log=False,  # Enrollment callback codes must never enter URL logs.
    )
    server = uvicorn.Server(config)

    await asyncio.gather(dp.start_polling(bot, tasks_concurrency_limit=32), server.serve())


if __name__ == "__main__":
    asyncio.run(main())

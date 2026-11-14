"""Bounded admission for synchronous database units called by async ingress."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial

_workers = ThreadPoolExecutor(max_workers=16, thread_name_prefix="ingress-db")
_slots = asyncio.Semaphore(16)


class DBBusyError(RuntimeError):
    pass


async def run_db(fn, *args):
    try:
        await asyncio.wait_for(_slots.acquire(), timeout=5)
    except TimeoutError as exc:
        raise DBBusyError("database ingress capacity exhausted") from exc
    try:
        future = asyncio.get_running_loop().run_in_executor(_workers, partial(fn, *args))
    except BaseException:
        _slots.release()
        raise
    # Cancellation of a caller cannot admit more work before its DB unit ends.
    future.add_done_callback(lambda _done: _slots.release())
    return await asyncio.shield(future)

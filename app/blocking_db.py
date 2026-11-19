"""Bounded admission for synchronous database units called by async ingress."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from threading import BoundedSemaphore

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


class BlockingLane:
    """Bound submitted work to running capacity, including canceled callers."""

    def __init__(self, workers, name):
        self._workers = ThreadPoolExecutor(max_workers=workers, thread_name_prefix=name)
        self._slots = BoundedSemaphore(workers)

    def submit(self, fn, /, *args, **kwargs):
        if not self._slots.acquire(blocking=False):
            raise DBBusyError("blocking worker capacity exhausted")
        try:
            future = self._workers.submit(fn, *args, **kwargs)
        except BaseException:
            self._slots.release()
            raise
        future.add_done_callback(lambda _done: self._slots.release())
        return future

    async def run(self, fn, /, *args, **kwargs):
        return await asyncio.shield(asyncio.wrap_future(self.submit(fn, *args, **kwargs)))

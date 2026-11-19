"""Bounded admission for synchronous database units called by async ingress."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import BoundedSemaphore

class DBBusyError(RuntimeError):
    pass


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

    def shutdown(self, *, wait=True):
        self._workers.shutdown(wait=wait)

    async def run(self, fn, /, *args, **kwargs):
        return await asyncio.shield(asyncio.wrap_future(self.submit(fn, *args, **kwargs)))


_ingress = BlockingLane(8, "bot-ingress")
schedule_effects_lane = BlockingLane(2, "schedule-effects")


async def run_db(fn, /, *args, **kwargs):
    return await _ingress.run(fn, *args, **kwargs)

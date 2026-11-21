"""Bounded admission for synchronous database units called by async ingress."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar
from threading import Lock


class DBBusyError(RuntimeError):
    pass


class BlockingLane:
    """Bound submitted work to running capacity, including canceled callers."""

    def __init__(self, workers, name):
        self._workers = ThreadPoolExecutor(max_workers=workers, thread_name_prefix=name)
        self._lock = Lock()
        self._available = workers

    def available(self):
        with self._lock:
            return self._available

    def _release(self, _future=None):
        with self._lock:
            self._available += 1

    def submit(self, fn, /, *args, **kwargs):
        with self._lock:
            if not self._available:
                raise DBBusyError("blocking worker capacity exhausted")
            self._available -= 1
        try:
            future = self._workers.submit(fn, *args, **kwargs)
        except BaseException:
            self._release()
            raise
        future.add_done_callback(self._release)
        return future

    def shutdown(self, *, wait=True):
        self._workers.shutdown(wait=wait)

    async def run(self, fn, /, *args, **kwargs):
        return await asyncio.shield(asyncio.wrap_future(self.submit(fn, *args, **kwargs)))


_ingress = BlockingLane(8, "bot-ingress")
schedule_effects_lane = BlockingLane(2, "schedule-effects")
deployment_lock_nowait = ContextVar("deployment_lock_nowait", default=False)


@contextmanager
def fail_fast_on_deployment_lock():
    token = deployment_lock_nowait.set(True)
    try:
        yield
    finally:
        deployment_lock_nowait.reset(token)


def _run_interactive(fn, args, kwargs):
    with fail_fast_on_deployment_lock():
        return fn(*args, **kwargs)


async def run_db(fn, /, *args, **kwargs):
    return await _ingress.run(_run_interactive, fn, args, kwargs)

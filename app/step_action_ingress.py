"""Order live Telegram actions against the expiry sweep in the polling process."""

from contextlib import contextmanager
from datetime import datetime, timezone
from threading import Lock


_pending: dict[tuple[int, int], dict[object, datetime]] = {}
_lock = Lock()


@contextmanager
def track_step_action(telegram_user_id: int, callback_data: str | None):
    if not callback_data or not callback_data.startswith(("task_complete:", "task_skip:")):
        yield None
        return
    try:
        step_id = int(callback_data.split(":")[1])
    except ValueError:
        yield None
        return

    key = (telegram_user_id, step_id)
    token = object()
    with _lock:
        received_at = datetime.now(timezone.utc)
        # Each entry belongs to one admitted polling update. The polling limit
        # bounds the total, including repeated presses for the same step.
        _pending.setdefault(key, {})[token] = received_at
    try:
        yield received_at
    finally:
        with _lock:
            actions = _pending[key]
            actions.pop(token)
            if not actions:
                del _pending[key]


def has_timely_step_action(telegram_user_id: int, step_id: int, deadline: datetime) -> bool:
    with _lock:
        actions = _pending.get((telegram_user_id, step_id), {})
        return any(received_at < deadline for received_at in actions.values())

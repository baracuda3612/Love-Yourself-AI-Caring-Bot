"""Order live Telegram actions against the expiry sweep in the polling process."""

from contextlib import contextmanager
from datetime import datetime, timezone
from threading import Lock

from aiogram.methods import GetUpdates
from aiogram.types import Update


_StepAction = tuple[int, int]
_pending: dict[_StepAction, dict[object, datetime]] = {}
_lock = Lock()


def _step_action(telegram_user_id: int, callback_data: str | None) -> _StepAction | None:
    if not callback_data or not callback_data.startswith(("task_complete:", "task_skip:")):
        return None
    try:
        return telegram_user_id, int(callback_data.split(":")[1])
    except ValueError:
        return None


def _register(key: _StepAction, received_at: datetime) -> object:
    token = object()
    _pending.setdefault(key, {})[token] = received_at
    return token


def _forget(key: _StepAction, token: object) -> None:
    with _lock:
        actions = _pending[key]
        del actions[token]
        if not actions:
            del _pending[key]


@contextmanager
def track_step_action(telegram_user_id: int, callback_data: str | None):
    key = _step_action(telegram_user_id, callback_data)
    if key is None:
        yield None
        return
    with _lock:
        received_at = datetime.now(timezone.utc)
        token = _register(key, received_at)
    try:
        yield received_at
    finally:
        _forget(key, token)


def has_timely_step_action(telegram_user_id: int, step_id: int, deadline: datetime) -> bool:
    with _lock:
        actions = _pending.get((telegram_user_id, step_id), {})
        return any(received_at < deadline for received_at in actions.values())


class PollingStepActions:
    """Track an entire GetUpdates response before aiogram queues its handlers."""

    def __init__(self):
        self._buffered: dict[int, tuple[_StepAction, object, datetime]] = {}

    async def receive(self, make_request, bot, method):
        result = await make_request(bot, method)
        if isinstance(method, GetUpdates):
            # One batch (Telegram's default limit is 100) plus the updates
            # admitted by aiogram can be in flight. Mark the whole batch at
            # once so expiry cannot interleave with registration.
            with _lock:
                received_at = datetime.now(timezone.utc)
                for update in result:
                    callback = update.callback_query
                    if callback is None or callback.message is None:
                        continue
                    chat = callback.message.chat
                    if chat.type != "private" or chat.id != callback.from_user.id:
                        continue
                    key = _step_action(callback.from_user.id, callback.data)
                    if key is not None and update.update_id not in self._buffered:
                        self._buffered[update.update_id] = key, _register(key, received_at), received_at
        return result

    async def dispatch(self, handler, update: Update, data):
        action = self._buffered.pop(update.update_id, None)
        if action is None:
            return await handler(update, data)
        key, token, received_at = action
        data["step_action_received_at"] = received_at
        try:
            return await handler(update, data)
        finally:
            _forget(key, token)

    async def clear_buffered(self):
        # The dispatcher has stopped polling. Handlers already admitted still
        # own their entries and release them in dispatch's finally block.
        while self._buffered:
            _, (key, token, _) = self._buffered.popitem()
            _forget(key, token)

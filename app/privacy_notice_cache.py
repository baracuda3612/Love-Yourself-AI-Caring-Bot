"""Bounded positive hints for immutable, committed notice acknowledgements.

Every caller still validates current access, deployment pin and notice
availability from PostgreSQL. A hint only avoids rereading the same receipt.
It never grants access, acknowledges a new version or persists across restart.
"""
from collections import OrderedDict
from dataclasses import dataclass
from threading import Lock

from sqlalchemy import event
from sqlalchemy.orm import Session

_LIMIT = 10_000
_PENDING = 'privacy_notice_ack_hint'
_hints = OrderedDict()
_mutex = Lock()


@dataclass(eq=False)
class _Hint:
    binding: tuple[int, int] | None = None


def _key(db, user_id):
    bind = db.get_bind()
    return (getattr(bind, 'engine', bind), user_id)


def _entry(db, user_id):
    key = _key(db, user_id)
    with _mutex:
        hint = _hints.get(key)
        if hint is None:
            hint = _Hint()
            _hints[key] = hint
        _hints.move_to_end(key)
        while len(_hints) > _LIMIT:
            _hints.popitem(last=False)
        return key, hint


def has_committed_ack(db, user_id, deployment_id, notice_id):
    key, hint = _entry(db, user_id)
    with _mutex:
        return _hints.get(key) is hint and hint.binding == (deployment_id, notice_id)


def stage_committed_ack(db, user_id, deployment_id, notice_id):
    """Promote an observed/written receipt only if its root transaction commits."""
    key, hint = _entry(db, user_id)
    db.info[_PENDING] = (key, hint, (deployment_id, notice_id))


def commit_read_check(db):
    """A standalone read-only admission check can warm the hint on commit."""
    if getattr(db, 'info', {}).get(_PENDING) is not None:
        db.commit()


def forget_user(db, user_id):
    with _mutex:
        _hints.pop(_key(db, user_id), None)
    # An in-flight transaction retains the old entry object. Its later commit
    # cannot resurrect the erased hint because promotion checks object identity.


@event.listens_for(Session, 'after_commit')
def _promote(db):
    if db.in_nested_transaction():
        return
    pending = db.info.pop(_PENDING, None)
    if pending is not None:
        key, hint, binding = pending
        with _mutex:
            if _hints.get(key) is hint:
                hint.binding = binding


@event.listens_for(Session, 'after_rollback')
def _discard(db):
    db.info.pop(_PENDING, None)


@event.listens_for(Session, 'after_transaction_end')
def _end(db, transaction):
    # Session.close() may roll back without an after_rollback callback. A reused
    # Session must never promote a receipt staged by an abandoned transaction.
    if transaction.parent is None:
        db.info.pop(_PENDING, None)

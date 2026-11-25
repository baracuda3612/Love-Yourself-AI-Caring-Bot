"""Same-message projection of authoritative step state onto a durable receipt."""
from dataclasses import replace
import asyncio
from threading import Lock
from weakref import WeakValueDictionary

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import String, and_, or_

from app.config import settings
from app.db import AIPlanStep, ExerciseDelivery, FeedbackEvent
from app.lifecycle import _lock_user
from app.scheduled_delivery import efficacy, presentation_from_snapshot, sent_receipt
from app.ux.exercise_renderer import render_exercise


_projection_locks = WeakValueDictionary()
_projection_locks_guard = Lock()


def pending_status_steps(db):
    """Find durable receipts whose last visible projection is stale."""
    return (db.query(AIPlanStep.id)
            .outerjoin(ExerciseDelivery, and_(ExerciseDelivery.plan_step_id == AIPlanStep.id,
                                              ExerciseDelivery.state == 'delivered'))
            .outerjoin(FeedbackEvent, and_(FeedbackEvent.plan_step_id == AIPlanStep.id,
                                          FeedbackEvent.source == 'exercise_efficacy'))
            .filter(AIPlanStep.step_status.in_(('completed', 'skipped', 'expired', 'canceled')),
                    or_(
                        and_(ExerciseDelivery.id.is_(None), AIPlanStep.step_status == 'expired',
                             AIPlanStep.tg_message_id.is_not(None)),
                        and_(ExerciseDelivery.id.is_not(None), ExerciseDelivery.projection_failure_code.is_(None),
                             or_(ExerciseDelivery.visible_status.is_distinct_from(AIPlanStep.step_status.cast(String)),
                                 ExerciseDelivery.visible_feedback.is_distinct_from(FeedbackEvent.value))),
                    )))


def pending_status_step_ids(db, *, limit=16):
    return [step_id for step_id, in pending_status_steps(db).order_by(AIPlanStep.id).limit(limit)]


def permanent_projection_failure(exc):
    if isinstance(exc, TelegramForbiddenError):
        return 'chat_inaccessible'
    if isinstance(exc, TelegramBadRequest) and any(reason in str(exc).lower() for reason in (
        'message to edit not found', "message can't be edited", 'message can not be edited', 'chat not found',
    )):
        return 'message_uneditable'
    return None


def reconcile_status(db, step_id):
    # The single scheduler/polling process serializes edits to the same
    # message without holding a database connection during Telegram I/O.
    with _projection_locks_guard:
        lock = _projection_locks.get(step_id)
        if lock is None:
            lock = Lock()
            _projection_locks[step_id] = lock
    if not lock.acquire(blocking=False):
        return False
    try:
        return _project_status(db, step_id)
    finally:
        lock.release()


def _project_status(db, step_id):
    step = db.get(AIPlanStep, step_id)
    if step is None:
        return False
    user_id = step.day.plan.user_id
    _lock_user(db, user_id, require_entitlement=False)
    db.refresh(step)
    receipt = sent_receipt(db, step_id)
    if receipt is None:
        return None  # historical pre-WP-03.4 compatibility
    status = str(step.step_status)
    feedback = efficacy(db, step_id)
    value = feedback.value if feedback else None
    if status == 'delivered':
        return True
    if receipt.visible_status == status and receipt.visible_feedback == value:
        return True
    try:
        presentation = replace(presentation_from_snapshot(receipt.presentation_snapshot), status=status, available_actions=())
        payload = render_exercise(presentation, caption=receipt.variant == 'gif')
    except (KeyError, TypeError, ValueError, AttributeError):
        receipt.projection_failure_code = 'invalid_snapshot'
        db.commit()
        return False
    keyboard = None
    if status == 'completed' and feedback is None:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text='Допомогло? Краще', callback_data=f'task_feedback:{step_id}:better'),
            InlineKeyboardButton(text='Так само', callback_data=f'task_feedback:{step_id}:same'),
            InlineKeyboardButton(text='Гірше', callback_data=f'task_feedback:{step_id}:worse'),
        ]])

    receipt_id, chat_id, message_id, variant = receipt.id, receipt.chat_id, receipt.message_id, receipt.variant
    db.commit()

    async def edit():
        async with Bot(token=settings.BOT_TOKEN) as bot:
            if variant == 'gif':
                return await bot.edit_message_caption(chat_id=chat_id, message_id=message_id,
                                                      caption=payload, parse_mode='HTML', reply_markup=keyboard, request_timeout=20)
            return await bot.edit_message_text(chat_id=chat_id, message_id=message_id,
                                               text=payload, parse_mode='HTML', reply_markup=keyboard, request_timeout=20)

    failure = None
    try:
        asyncio.run(asyncio.wait_for(edit(), timeout=20))
    except Exception as exc:
        if 'message is not modified' not in str(exc).lower():
            failure = permanent_projection_failure(exc)
            if failure is None:
                return False

    # Record the exact snapshot sent. If a concurrent button action changed
    # authoritative state during the edit, that receipt is still discoverable.
    current = db.get(AIPlanStep, step_id, populate_existing=True)
    if current is None:
        return True
    _lock_user(db, user_id, require_entitlement=False)
    receipt = db.get(ExerciseDelivery, receipt_id, populate_existing=True)
    if receipt is None:
        return True
    receipt.projection_failure_code = failure
    if failure is None:
        receipt.visible_status = status
        receipt.visible_feedback = value
        if current.tg_message_id == message_id:
            current.tg_message_id = None
    db.commit()
    return failure is None

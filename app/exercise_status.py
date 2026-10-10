"""Same-message projection of authoritative step state onto a durable receipt."""
from dataclasses import replace
import asyncio

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import String, and_, or_

from app.config import settings
from app.db import AIPlanStep, ExerciseDelivery, FeedbackEvent
from app.lifecycle import _lock_user
from app.scheduled_delivery import efficacy, presentation_from_snapshot, sent_receipt
from app.ux.exercise_renderer import render_exercise


def pending_status_step_ids(db):
    """Select stale terminal projections before opening per-user lock sessions."""
    return [row[0] for row in db.query(ExerciseDelivery.plan_step_id)
            .join(AIPlanStep, AIPlanStep.id == ExerciseDelivery.plan_step_id)
            .outerjoin(FeedbackEvent, and_(FeedbackEvent.plan_step_id == AIPlanStep.id,
                                          FeedbackEvent.source == 'exercise_efficacy'))
            .filter(ExerciseDelivery.state == 'delivered',
                    ExerciseDelivery.projection_failure_code.is_(None),
                    AIPlanStep.step_status.in_(('completed', 'skipped', 'expired', 'canceled')),
                    or_(ExerciseDelivery.visible_status.is_distinct_from(AIPlanStep.step_status.cast(String)),
                        ExerciseDelivery.visible_feedback.is_distinct_from(FeedbackEvent.value)))
            .all()]


def reconcile_status(db, step_id):
    step = db.get(AIPlanStep, step_id)
    if step is None:
        return False
    # All projections and feedback/action writes share the same user lock.
    # Hold it through the bounded edit so stale markup cannot win a race.
    _lock_user(db, step.day.plan.user_id, require_entitlement=False)
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

    async def edit():
        # A worker-owned HTTP session keeps this bounded edit independent of the
        # polling event loop, whose legacy synchronous controls also lock users.
        async with Bot(token=settings.BOT_TOKEN) as bot:
            if receipt.variant == 'gif':
                return await bot.edit_message_caption(chat_id=receipt.chat_id, message_id=receipt.message_id,
                                                      caption=payload, parse_mode='HTML', reply_markup=keyboard, request_timeout=20)
            return await bot.edit_message_text(chat_id=receipt.chat_id, message_id=receipt.message_id,
                                               text=payload, parse_mode='HTML', reply_markup=keyboard, request_timeout=20)
    try:
        asyncio.run(edit())
    except Exception as exc:
        if 'message is not modified' not in str(exc).lower():
            # Definitive access/message rejection cannot improve by polling.
            # A later explicit action may try again, without falsifying markers.
            if isinstance(exc, TelegramForbiddenError):
                receipt.projection_failure_code = 'chat_inaccessible'
            elif isinstance(exc, TelegramBadRequest) and any(reason in str(exc).lower() for reason in (
                'message to edit not found', "message can't be edited", 'message can not be edited', 'chat not found',
            )):
                receipt.projection_failure_code = 'message_uneditable'
            if receipt.projection_failure_code:
                db.commit()
            return False
    receipt.projection_failure_code = None
    receipt.visible_status = status
    receipt.visible_feedback = value
    # Compatibility marker may be cleared; the durable receipt never is.
    step.tg_message_id = None
    db.commit()
    return True

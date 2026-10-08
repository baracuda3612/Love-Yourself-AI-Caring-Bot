"""One Telegram exercise send, with definite-failure fallback and no retries."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Literal

from aiogram.exceptions import (
    TelegramBadRequest, TelegramEntityTooLarge, TelegramForbiddenError,
    TelegramMigrateToChat, TelegramNotFound, TelegramRetryAfter,
    TelegramUnauthorizedError,
)
from aiogram.types import BufferedInputFile, InlineKeyboardMarkup

from app.content_library import ROOT
from app.exercise_presentation import ExerciseMedia, ExercisePresentation
from app.ux.exercise_renderer import PresentationTooLong, render_exercise

# Disposable optimization only: never a receipt or content authority.
_animation_file_ids: dict[tuple, str] = {}
_DEFINITE_REJECTIONS = (
    TelegramBadRequest, TelegramEntityTooLarge, TelegramForbiddenError,
    TelegramMigrateToChat, TelegramNotFound, TelegramRetryAfter,
    TelegramUnauthorizedError,
)


@dataclass(frozen=True)
class ExerciseSendResult:
    outcome: Literal['delivered', 'failed', 'uncertain']
    # On success this is the actual variant; on a known attempted call it is
    # the attempted variant. None means the scheduler could not observe it.
    variant: Literal['gif', 'text'] | None
    presentation: ExercisePresentation
    rendered_payload: str
    chat_id: int | None = None
    message_id: int | None = None
    failure_code: str | None = None

    @property
    def delivered(self) -> bool:
        return self.outcome == 'delivered'


def _media_key(bot, media: ExerciseMedia) -> tuple:
    return (
        bot.id, media.exercise_id, media.content_version, media.asset_version,
        media.revision, media.sha256,
    )


def _confirmed_result(message, chat_id: int, variant, presentation, payload) -> ExerciseSendResult:
    message_id = getattr(message, 'message_id', None)
    returned_chat = getattr(getattr(message, 'chat', None), 'id', None)
    if type(message_id) is not int or message_id <= 0 or returned_chat != chat_id:
        return ExerciseSendResult('uncertain', variant, presentation, payload, failure_code='missing_message_identity')
    return ExerciseSendResult('delivered', variant, presentation, payload, returned_chat, message_id)


async def send_exercise(
    bot, chat_id: int, presentation: ExercisePresentation, *,
    reply_markup: InlineKeyboardMarkup | None = None, scheduled: bool = True,
) -> ExerciseSendResult:
    """Return observed result; caller owns receipts, events and reconciliation.

    Do not use a short client timeout as proof of non-delivery. The complete
    caption is readable without playback; no client-load polling is possible.
    """
    try:
        text = render_exercise(presentation, scheduled=scheduled)
    except (ValueError, KeyError):
        return ExerciseSendResult('failed', 'text', presentation, '', failure_code='invalid_text_presentation')

    media = presentation.media
    if media:
        try:
            caption = render_exercise(presentation, caption=True, scheduled=scheduled)
        except PresentationTooLong:
            caption = None
        if caption is not None:
            key = _media_key(bot, media)
            animation = _animation_file_ids.get(key)
            if animation is None:
                # Approved packaged bytes only, read off the event loop. Integrity
                # and dimensions were checked at release loading, not here.
                path = (ROOT / media.path).resolve()
                allowed = (ROOT / 'resource/assets/content_library/media').resolve()
                try:
                    if not path.is_relative_to(allowed) or path.suffix != '.gif':
                        raise OSError('invalid packaged media path')
                    data = await asyncio.to_thread(path.read_bytes)
                    animation = BufferedInputFile(data, filename=path.name)
                except OSError:
                    animation = None
            if animation is not None:
                try:
                    message = await bot.send_animation(
                        chat_id=chat_id, animation=animation, caption=caption,
                        parse_mode='HTML', show_caption_above_media=True,
                        reply_markup=reply_markup,
                    )
                except _DEFINITE_REJECTIONS:
                    # A rejected cached handle must not poison later deliveries.
                    _animation_file_ids.pop(key, None)
                except Exception:
                    return ExerciseSendResult('uncertain', 'gif', presentation, caption, failure_code='media_send_uncertain')
                else:
                    result = _confirmed_result(message, chat_id, 'gif', presentation, caption)
                    if result.delivered:
                        file_id = getattr(getattr(message, 'animation', None), 'file_id', None)
                        if isinstance(file_id, str) and file_id:
                            _animation_file_ids[key] = file_id
                    return result

    try:
        message = await bot.send_message(
            chat_id=chat_id, text=text, parse_mode='HTML', reply_markup=reply_markup,
        )
    except _DEFINITE_REJECTIONS:
        return ExerciseSendResult('failed', 'text', presentation, text, failure_code='text_send_rejected')
    except Exception:
        return ExerciseSendResult('uncertain', 'text', presentation, text, failure_code='text_send_uncertain')
    return _confirmed_result(message, chat_id, 'text', presentation, text)

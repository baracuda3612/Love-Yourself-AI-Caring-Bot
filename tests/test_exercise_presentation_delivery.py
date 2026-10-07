"""WP-03.3: usable complete instructions, versioned media and truthful sends."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from html import unescape, escape
import json
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import pytz
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramServerError
from aiogram.methods import SendAnimation
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from app import exercise_delivery
from app.content_library import DEFAULT_SEED_PATH, FIELDS, ROOT, ContentValidationError
from app.db import ContentLibrary
from app.exercise_presentation import exercise_presentation, step_presentation
from app.exercise_delivery import send_exercise
from app.ux.exercise_renderer import PresentationTooLong, render_exercise, telegram_length


@pytest.fixture
def records():
    return json.loads(DEFAULT_SEED_PATH.read_text())['inventory']


@pytest.fixture(autouse=True)
def clear_media_cache():
    exercise_delivery._animation_file_ids.clear()
    yield
    exercise_delivery._animation_file_ids.clear()


def content_db(record):
    db = Mock()
    db.execute.return_value.scalar_one_or_none.return_value = ContentLibrary(
        exercise_id=record['id'], **{key: deepcopy(record[key]) for key in FIELDS},
    )
    return db


def approved_cold(record):
    record = deepcopy(record)
    record['review_status'] = 'approved'
    record['review_evidence'] = dict(
        exercise_id=record['id'], content_version=record['content_version'],
        reviewer='disposable test reviewer', qualification='test fixture only',
        reference='not a real medical approval', media_sha256=record['media']['sha256'],
    )
    return record


def presentation(record):
    return exercise_presentation(
        content_db(record), record['id'], record['content_version'],
        action_deadline=datetime(2099, 10, 7, 23, 59, tzinfo=timezone.utc),
    )


def bot(bot_id=10, chat_id=123):
    message = SimpleNamespace(message_id=77, chat=SimpleNamespace(id=chat_id),
                              animation=SimpleNamespace(file_id='telegram-gif'))
    return SimpleNamespace(id=bot_id, send_animation=AsyncMock(return_value=message),
                           send_message=AsyncMock(return_value=message))


def visible(text):
    return unescape(re.sub(r'</?b>', '', text))


def rejection(cls=TelegramBadRequest):
    return cls(method=SendAnimation(chat_id=123, animation='x'), message='test rejection')


def test_all_nine_exact_plain_presentations_and_no_internal_copy(records, monkeypatch):
    monkeypatch.setattr('pathlib.Path.read_bytes', Mock(side_effect=AssertionError('runtime media I/O')))
    for record in records:
        if record['id'] == 'cold_water_face':
            record = approved_cold(record)
        p = presentation(record)
        text = render_exercise(p)
        assert text.splitlines()[0] == 'Пауза'
        assert p.title == record['display']['title']
        assert p.steps == tuple(record['display']['steps'])
        assert p.duration_label in text
        for index, step in enumerate(p.steps, 1):
            assert f'{index}. {step}' in visible(text)
        assert all(part not in text for part in ('switch', 'unload', 'DAY', 'EVENING', 'rationale', '━━━━━━━━', 'День 1'))
        assert p.available_actions == ('complete', 'skip')
        assert p.to_payload()['content_version'] == 1
        assert visible(render_exercise(p, scheduled=False)).startswith(p.title)


def test_dynamic_html_and_unicode_limits(records):
    p = replace(presentation(records[0]), title='<b>A & B</b>',
                duration_label='X < Y', steps=('Do <this> & "that" 😀',))
    text = render_exercise(p)
    assert '&lt;b&gt;A &amp; B&lt;/b&gt;' in text
    assert visible(text).splitlines()[2] == p.title
    assert visible(text).splitlines()[5] == '1. ' + p.steps[0]
    minimal = replace(p, steps=('x',))
    overhead = telegram_length(visible(render_exercise(minimal))) - 1
    for limit, caption in ((1024, True), (4096, False)):
        exact = replace(p, steps=('😀' * ((limit-overhead)//2) + 'x' * ((limit-overhead)%2),))
        assert telegram_length(visible(render_exercise(exact, caption=caption))) == limit
        with pytest.raises(PresentationTooLong):
            render_exercise(replace(exact, steps=(exact.steps[0]+'x',)), caption=caption)
    # Escaped source length may exceed the limit; visible length is authoritative.
    entities = replace(p, steps=('&' * (1024-overhead),))
    assert len(render_exercise(entities, caption=True)) > 1024


def test_exact_version_and_medical_gate(records):
    cold = next(r for r in records if r['id'] == 'cold_water_face')
    with pytest.raises(ContentValidationError):
        presentation(cold)
    record = deepcopy(records[0]); record['is_active'] = False
    with pytest.raises(ContentValidationError):
        presentation(record)
    p = presentation(records[0])
    record = deepcopy(records[0]); record['content_version'] = 2
    db = content_db(record)
    db.execute.return_value.scalar_one_or_none.return_value = None
    with pytest.raises(ContentValidationError):
        exercise_presentation(db, p.exercise_id, 1)


def test_deadline_status_actions_are_channel_neutral(records):
    step = SimpleNamespace(exercise_id=records[0]['id'], content_version=1,
        step_status='delivered', expires_at=datetime(2099,10,7,21,59,tzinfo=timezone.utc))
    p = step_presentation(content_db(records[0]), step, user_timezone=pytz.timezone('Europe/Kyiv'))
    assert p.action_deadline.hour == 23
    assert '07.10 23:59' in render_exercise(p)
    for status in ('completed', 'skipped', 'expired', 'canceled'):
        step.step_status = status
        p = step_presentation(content_db(records[0]), step)
        assert p.status == status and not p.available_actions
    step.step_status = 'delivered'; step.expires_at = datetime(2000,1,1)
    assert not step_presentation(content_db(records[0]), step).available_actions


@pytest.mark.parametrize('exercise_id', ['breathing_sigh','pmr_fist','cold_water_face'])
async def test_three_real_versioned_gifs_have_complete_captions(records, exercise_id, monkeypatch):
    record = next(r for r in records if r['id'] == exercise_id)
    if exercise_id == 'cold_water_face':
        record = approved_cold(record)
    monkeypatch.setattr('app.content_library.sha256', Mock(side_effect=AssertionError('runtime hashing')))
    p = presentation(record); tg = bot()
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='Виконано', callback_data='task_complete:1')]])
    result = await send_exercise(tg, 123, p, reply_markup=keyboard)
    assert result.delivered and result.variant == 'gif'
    assert (result.chat_id, result.message_id) == (123,77)
    args = tg.send_animation.call_args.kwargs
    assert isinstance(args['animation'], BufferedInputFile)
    assert args['animation'].data == (ROOT / record['media']['path']).read_bytes()
    assert result.presentation.media.content_version == record['content_version']
    assert args['caption'] == result.rendered_payload == render_exercise(p, caption=True)
    assert args['show_caption_above_media'] is True
    assert args['reply_markup'] == keyboard
    assert args['caption'].splitlines()[0] == 'Пауза'
    assert telegram_length(visible(args['caption'])) <= 1024
    for step in p.steps:
        assert escape(step) in args['caption']
    tg.send_message.assert_not_called()


async def test_six_text_only_exercises_are_complete(records):
    for record in records:
        if record['media'] is not None:
            continue
        p = presentation(record); tg = bot()
        result = await send_exercise(tg,123,p)
        assert result.delivered and result.variant == 'text'
        assert result.rendered_payload == render_exercise(p)
        tg.send_animation.assert_not_called()


@pytest.mark.parametrize('reason', ['caption_overflow','definite_rejection','missing_file'])
async def test_same_complete_text_fallback(records, monkeypatch, reason):
    p = presentation(records[0]); tg = bot()
    if reason == 'caption_overflow':
        p = replace(p, steps=('x' * 1500,))
    elif reason == 'definite_rejection':
        tg.send_animation.side_effect = rejection()
    else:
        monkeypatch.setattr('pathlib.Path.read_bytes', Mock(side_effect=OSError('missing')))
    result = await send_exercise(tg,123,p)
    assert result.delivered and result.variant == 'text'
    assert result.presentation == p
    assert tg.send_message.call_args.kwargs['text'] == render_exercise(p)
    assert tg.send_animation.call_count == (reason == 'definite_rejection')


@pytest.mark.parametrize('failure', [TimeoutError(), rejection(TelegramNetworkError), rejection(TelegramServerError), RuntimeError('unknown')])
async def test_uncertain_gif_never_sends_duplicate_text(records, failure):
    tg = bot(); tg.send_animation.side_effect = failure
    result = await send_exercise(tg,123,presentation(records[0]))
    assert result.outcome == 'uncertain' and not result.delivered
    assert result.variant == 'gif' and result.message_id is None
    tg.send_message.assert_not_called()
    assert not exercise_delivery._animation_file_ids


@pytest.mark.parametrize('text_error,outcome', [(rejection(),'failed'),(TimeoutError(),'uncertain')])
async def test_both_sends_fail_without_success(records,text_error,outcome):
    tg = bot(); tg.send_animation.side_effect = rejection(); tg.send_message.side_effect = text_error
    result = await send_exercise(tg,123,presentation(records[0]))
    assert result.outcome == outcome and not result.delivered
    assert result.message_id is None
    assert tg.send_animation.call_count == tg.send_message.call_count == 1


async def test_oversize_text_fails_before_any_upload(records):
    tg = bot(); p = replace(presentation(records[0]), steps=('x' * 4096,))
    result = await send_exercise(tg,123,p)
    assert result.outcome == 'failed'
    tg.send_animation.assert_not_called(); tg.send_message.assert_not_called()


@pytest.mark.parametrize('malformed', [None,SimpleNamespace(message_id=0,chat=SimpleNamespace(id=123)),SimpleNamespace(message_id=77,chat=SimpleNamespace(id=999))])
async def test_missing_or_wrong_message_identity_is_uncertain(records, malformed):
    tg = bot(); tg.send_animation.return_value = malformed
    result = await send_exercise(tg,123,presentation(records[0]))
    assert result.outcome == 'uncertain'
    tg.send_message.assert_not_called()


async def test_cache_reuses_exact_bot_version_asset_without_file_io(records,monkeypatch):
    p = presentation(records[0]); tg = bot()
    assert (await send_exercise(tg,123,p)).variant == 'gif'
    read_bytes = Mock(side_effect=OSError('file unavailable'))
    monkeypatch.setattr('pathlib.Path.read_bytes',read_bytes)
    assert (await send_exercise(tg,123,p)).variant == 'gif'
    assert tg.send_animation.call_args.kwargs['animation'] == 'telegram-gif'
    read_bytes.assert_not_called()
    for other in (
        replace(p,content_version=2,media=replace(p.media,content_version=2)),
        replace(p,media=replace(p.media,asset_version=2)),
        replace(p,media=replace(p.media,revision='another')),
        replace(p,media=replace(p.media,sha256='0'*64)),
    ):
        assert (await send_exercise(tg,123,other)).variant == 'text'
    assert (await send_exercise(bot(bot_id=11),123,p)).variant == 'text'
    assert read_bytes.call_count == 5


async def test_rejected_cached_handle_is_evicted_and_same_text_sent(records):
    p = presentation(records[0]); tg = bot()
    await send_exercise(tg,123,p)
    tg.send_animation.side_effect = rejection()
    assert (await send_exercise(tg,123,p)).variant == 'text'
    assert not exercise_delivery._animation_file_ids


def test_coach_message_forwards_only_canonical_facts(records):
    from app.workers.coach_agent import _context_message
    context = presentation(records[0]).to_payload()
    message = _context_message({'current_exercise_context':context})
    payload = json.loads(message.split('\n',1)[1])
    assert payload['current_exercise_context'] == context
    assert not {'mechanic','slot','category','rationale'} & context.keys()

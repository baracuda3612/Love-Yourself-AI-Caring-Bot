from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

import pytest

from app import onboarding


def setup():
    user = SimpleNamespace(
        id=1, timezone="Europe/Kyiv", onboarding_progress=None, timezone_override=None, timezone_override_explicit=False,
        profile=SimpleNamespace(daily_time_slots={"DAY": "14:00"}, active_days=None),
    )
    return user, SimpleNamespace(stage="START", revision=0, draft_days=None)


def click(db, user, progress, action):
    return onboarding.handle_action(db, user, progress, progress.revision, action)


def test_invalid_input_and_old_confirmation_do_not_mutate_or_activate():
    db = Mock()
    user, progress = setup()
    click(db, user, progress, "start")
    for invalid in ("99:99", "24:00", "14:75", "привіт"):
        assert "Не вдалося" in onboarding.handle_text(user, progress, invalid).text
        assert progress.stage == "TIME"
        assert user.profile.daily_time_slots["DAY"] == "14:00"

    screen = onboarding.handle_text(user, progress, "00:00")
    old_revision = progress.revision
    old_confirm = screen.keyboard.inline_keyboard[0][0].callback_data.split(":")[2]
    click(db, user, progress, "settings")
    click(db, user, progress, "time")
    screen = onboarding.handle_text(user, progress, "23:59")
    assert "23:59" in screen.text and "Київ" in screen.text
    stale, result = onboarding.handle_action(db, user, progress, old_revision, old_confirm)
    assert "неактуальна" in stale.text
    assert result is None
    db.query.assert_not_called()


def test_days_stay_draft_until_nonempty_apply():
    db = Mock()
    user, progress = setup()
    click(db, user, progress, "start")
    onboarding.handle_text(user, progress, "16:10")
    click(db, user, progress, "settings")
    click(db, user, progress, "days")
    for i in range(5):
        click(db, user, progress, f"day_{i}")
    screen, _ = click(db, user, progress, "apply")
    assert "хоча б один" in screen.text
    assert user.profile.active_days == onboarding.ACTIVE_DAYS_DEFAULT
    click(db, user, progress, "cancel")
    assert progress.stage == "CONFIRM"
    assert user.profile.active_days == onboarding.ACTIVE_DAYS_DEFAULT
    click(db, user, progress, "settings")
    click(db, user, progress, "days")
    click(db, user, progress, "day_6")
    screen, _ = click(db, user, progress, "apply")
    assert "неділя" in screen.text and "16:10" in screen.text
    assert progress.draft_days is None


def test_fixed_offset_catalogue_has_27_valid_zones():
    assert set(onboarding.OFFSETS) == set(range(-12, 15))
    for offset, name in onboarding.OFFSETS.items():
        from datetime import datetime

        local = datetime(2026, 11, 4, tzinfo=ZoneInfo(name))
        assert local.utcoffset().total_seconds() == offset * 3600


@pytest.mark.anyio
async def test_setup_text_bypasses_coach_and_history(monkeypatch):
    from app import privacy, telegram

    user, progress = setup()
    progress.stage = "TIME"
    db = Mock()
    coach = AsyncMock()
    send = AsyncMock()
    monkeypatch.setattr(telegram, "SessionLocal", lambda: nullcontext(db))
    monkeypatch.setattr(telegram, "_ensure_user", lambda *_: (user, False))
    monkeypatch.setattr(privacy, "pinned_notice", Mock(return_value=(None, SimpleNamespace(id=1))))
    monkeypatch.setattr(telegram.menu_input, "handle_text", Mock(return_value=None))
    db.begin_nested.return_value = nullcontext()
    monkeypatch.setattr(onboarding, "setup_progress", lambda *_: progress)
    monkeypatch.setattr(telegram, "handle_incoming_message", coach)
    monkeypatch.setattr(telegram, "_send_setup_screen", send)

    message = SimpleNamespace(from_user=SimpleNamespace(id=1), text="сторонній текст", message_id=5)
    await telegram.on_text(message)

    coach.assert_not_awaited()
    db.add.assert_not_called()
    assert progress.stage == "TIME"
    assert "ГГ:ХХ" in send.call_args.args[2].text


def test_standard_zone_preserves_local_time_across_dst_and_manual_offset_is_fixed():
    from datetime import date, datetime, timezone

    from app.time_slots import compute_scheduled_for

    dates = (date(2026, 10, 24), date(2026, 10, 26))
    standard, fixed = [], []
    for day in dates:
        standard.append(compute_scheduled_for(
            plan_start=datetime(2026, 10, 1, tzinfo=timezone.utc), day_number=1,
            time_slot="DAY", timezone_name="Europe/Kyiv", daily_time_slots={"DAY": "14:34"},
            anchor_date=day,
        ))
        fixed.append(compute_scheduled_for(
            plan_start=datetime(2026, 10, 1, tzinfo=timezone.utc), day_number=1,
            time_slot="DAY", timezone_name="Etc/GMT-2", daily_time_slots={"DAY": "14:34"},
            anchor_date=day,
        ))
    assert [instant.astimezone(ZoneInfo("Europe/Kyiv")).strftime("%H:%M") for instant in standard] == ["14:34", "14:34"]
    assert [instant.strftime("%H:%M") for instant in standard] == ["11:34", "12:34"]
    assert [instant.strftime("%H:%M") for instant in fixed] == ["12:34", "12:34"]

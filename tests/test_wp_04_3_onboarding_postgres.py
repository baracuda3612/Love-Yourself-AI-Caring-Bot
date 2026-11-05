"""Onboarding rehearsal against a newly migrated disposable PostgreSQL database."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy.orm import sessionmaker

from app import access, db as models, onboarding, privacy
from tests.test_wp_04_2_privacy_postgres import engine, pytestmark, subject  # noqa: F401


async def test_setup_survives_new_sessions_and_only_activates_once(engine, monkeypatch, request):
    from app import scheduler, telegram

    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as db:
        user, deployment, notice = subject(db)
        deployment.default_timezone = "Europe/Kyiv"
        privacy.acknowledge_notice(db, user.id, notice.id, "wp043:test:notice")
        user_id, tg_id = user.id, user.tg_id
        db.commit()

    monkeypatch.setattr(telegram, "SessionLocal", factory)
    monkeypatch.setattr(models, "SessionLocal", factory)
    monkeypatch.setattr(access, "outbound_claim", lambda _user_id: True)
    jobs = BackgroundScheduler()
    jobs.start(paused=True)
    request.addfinalizer(jobs.shutdown)
    monkeypatch.setattr(scheduler, "scheduler", jobs)
    monkeypatch.setattr(scheduler, "SessionLocal", factory)
    coach = AsyncMock()
    monkeypatch.setattr(telegram, "handle_incoming_message", coach)

    actor = SimpleNamespace(id=tg_id, username=None, first_name=None)
    message = SimpleNamespace(
        from_user=actor, chat=SimpleNamespace(id=tg_id, type="private"),
        text="/start", message_id=5, answer=AsyncMock(),
    )

    async def click(data):
        await telegram.setup_action(SimpleNamespace(
            data=data, from_user=actor, message=message, answer=AsyncMock(),
        ))

    def buttons():
        return message.answer.call_args.kwargs["reply_markup"].inline_keyboard

    await telegram.cmd_start(message)
    await click(buttons()[0][0].callback_data)
    message.text = "99:99"
    await telegram.on_text(message)
    assert "ГГ:ХХ" in message.answer.call_args.args[0]
    message.message_id += 1
    message.text = "14:34"
    await telegram.on_text(message)
    assert "Київ" in message.answer.call_args.args[0]
    confirmation = buttons()[0][0].callback_data
    await telegram.on_text(message)
    assert buttons()[0][0].callback_data == confirmation

    async def menu_click(label):
        data = next(button.callback_data for row in buttons() for button in row if button.text == label)
        await telegram.menu_action(SimpleNamespace(
            data=data, from_user=actor, message=message,
            id=f"menu-{label}", answer=AsyncMock(),
        ))
        return data

    # Public guidance routes bypass conversation ingress and restore the exact draft.
    message.text = "Про проєкт"
    await telegram.menu_text(message)
    assert [row[0].text for row in buttons()[:4]] == [
        "Опис проєкту", "Як приходять нагадування", "Часті запитання", "Конфіденційність",
    ]
    await menu_click("Часті запитання")
    await menu_click("Як це працює?")
    assert "коротку вправу для перерви" in message.answer.call_args.args[0]
    assert "Вправа зараз" not in message.answer.call_args.args[0]
    await menu_click("Назад до розділу")
    await menu_click("Що, якщо не встигну?")
    await menu_click("Повернутися до налаштувань")
    assert buttons()[0][0].callback_data == confirmation

    async def submit(text):
        message.message_id += 1
        message.text = text
        await telegram.on_text(message)

    message.text = "Про проєкт"
    await telegram.menu_text(message)
    await menu_click("Залишити відгук")
    await submit("  Зручно повернутися до розкладу.  ")
    assert "Відгук збережено" in message.answer.call_args.args[0]
    await telegram.on_text(message)  # Duplicate transport update.
    with factory() as db:
        feedback = db.query(models.FeedbackEvent).filter_by(user_id=user_id, source="product_feedback").one()
        assert feedback.extracted_text == "  Зручно повернутися до розкладу.  "
        assert db.query(models.ChatHistory).filter_by(user_id=user_id).count() == 0

    message.text = "Мої дані"
    await telegram.menu_text(message)
    await menu_click("Виправити дані")
    await menu_click("Як до тебе звертатися")
    await submit("Тестове ім’я")
    assert "Ім’я: Тестове ім’я" in message.answer.call_args.args[0]

    # /start uses a new session and renders the accepted draft again.
    message.text = "/start"
    await telegram.cmd_start(message)
    assert buttons()[0][0].callback_data == confirmation
    await click(confirmation)
    assert "Перше планове нагадування" in message.answer.call_args.args[0]
    await click(confirmation)
    root = message.answer.call_args_list[-2].kwargs["reply_markup"]
    assert any(button.text == "Мій розклад" for row in root.keyboard for button in row)

    coach.assert_not_awaited()
    with factory() as db:
        progress = db.get(models.OnboardingProgress, user_id)
        assert progress.stage == "COMPLETED"
        assert progress.completed_at is not None
        assert db.query(models.AIPlan).filter_by(user_id=user_id).count() == 1
        assert db.query(models.PlanLifecycleOperation).filter_by(
            user_id=user_id, source_operation_id=onboarding.FIRST_PLAN_OPERATION,
        ).count() == 1
        assert db.query(models.ChatHistory).filter_by(user_id=user_id).count() == 0
        first = (
            db.query(models.AIPlanStep)
            .join(models.AIPlanDay)
            .join(models.AIPlan)
            .filter(models.AIPlan.user_id == user_id)
            .order_by(models.AIPlanStep.scheduled_for)
            .first()
        )
        assert jobs.get_job(scheduler._generate_step_job_id(first)).next_run_time == first.scheduled_for
        local = first.scheduled_for.astimezone(ZoneInfo("Europe/Kyiv"))
        assert f"{local:%d.%m.%Y} о {local:%H:%M}" in message.answer.call_args.args[0]


    message.text = "Мій розклад"
    await telegram.menu_text(message)
    await menu_click("Змінити налаштування")
    await menu_click("Змінити час")
    await submit("99:99")
    assert "ГГ:ХХ" in message.answer.call_args.args[0]
    await submit("16:12")
    assert "16:12" in message.answer.call_args.args[0]
    # An already-due step keeps its schedule and expiry across both calendar edits.
    with factory() as db:
        first = db.query(models.AIPlanStep).join(models.AIPlanDay).join(models.AIPlan).filter(
            models.AIPlan.user_id == user_id,
        ).order_by(models.AIPlanStep.scheduled_for).first()
        first.scheduled_for = datetime.now(timezone.utc) - timedelta(seconds=20)
        due_step_id = first.id
        original_due, original_expiry = first.scheduled_for, first.expires_at
        db.commit()
    await menu_click("Змінити налаштування")
    await menu_click("Дні надсилання")
    await submit("пʼятниця")
    assert "Скористайся кнопками" in message.answer.call_args.args[0]
    stale_day = await menu_click("субота")
    await telegram.menu_action(SimpleNamespace(
        data=stale_day, from_user=actor, message=message, id="replayed-day", answer=AsyncMock(),
    ))
    assert "неактуальна" in message.answer.call_args.args[0]
    await menu_click("Змінити налаштування")
    await menu_click("Дні надсилання")
    await menu_click("субота")
    await menu_click("Застосувати")
    assert "субота" in message.answer.call_args.args[0]
    await menu_click("Змінити налаштування")
    await menu_click("Часовий пояс")
    await menu_click("UTC+03:00")
    assert "UTC+03:00" in message.answer.call_args.args[0]
    with factory() as db:
        profile = db.query(models.UserProfile).filter_by(user_id=user_id).one()
        assert profile.active_days[-1] == "SAT"
        steps = db.query(models.AIPlanStep).join(models.AIPlanDay).join(models.AIPlan).filter(
            models.AIPlan.user_id == user_id,
        ).all()
        assert len(steps) == 7
        due_step = db.get(models.AIPlanStep, due_step_id)
        assert (due_step.scheduled_for, due_step.expires_at) == (original_due, original_expiry)
        zone = ZoneInfo("Etc/GMT-3")
        future = [step for step in steps if step.id != due_step_id]
        assert all(step.scheduled_for.astimezone(zone).strftime("%H:%M") == "16:12" for step in future)
        assert all(step.scheduled_for.astimezone(zone).weekday() != 6 for step in future)
    await menu_click("Змінити налаштування")
    await menu_click("Часовий пояс")
    await menu_click("Стандартний часовий пояс")
    assert "Київ" in message.answer.call_args.args[0]
    await menu_click("Змінити формат")
    cancelled_format = next(
        button.callback_data for row in buttons() for button in row if button.text == "Підтвердити зміну"
    )
    await menu_click("Скасувати")
    await telegram.menu_action(SimpleNamespace(
        data=cancelled_format, from_user=actor, message=message,
        id="cancelled-format", answer=AsyncMock(),
    ))
    assert "неактуальна" in message.answer.call_args.args[0]
    await menu_click("Змінити формат")
    old_format = await menu_click("Підтвердити зміну")
    assert "Введи час вечірнього нагадування" in message.answer.call_args.args[0]
    await submit("99:99")
    assert "Не вдалося прочитати час вечірнього нагадування." in message.answer.call_args.args[0]
    assert "18:30" in message.answer.call_args.args[0]
    assert "14:30" not in message.answer.call_args.args[0]
    await submit("18:30")
    assert "і 18:30" in message.answer.call_args.args[0]
    await menu_click("Змінити налаштування")
    await menu_click("Змінити вечірній час")
    await submit("99:99")
    assert "Не вдалося прочитати час вечірнього нагадування." in message.answer.call_args.args[0]
    assert "18:30" in message.answer.call_args.args[0]
    assert "14:30" not in message.answer.call_args.args[0]
    await menu_click("Скасувати")
    message.text = "Мій розклад"
    await telegram.menu_text(message)
    assert "і 18:30" in message.answer.call_args.args[0]
    await menu_click("Змінити формат")
    await menu_click("Підтвердити зміну")
    assert "Стан: активний" in message.answer.call_args.args[0]
    assert "і 18:30" not in message.answer.call_args.args[0]
    await telegram.menu_action(SimpleNamespace(
        data=old_format, from_user=actor, message=message, id="replayed-format", answer=AsyncMock(),
    ))
    assert "і 18:30" not in message.answer.call_args.args[0]
    old_pause = await menu_click("Призупинити")
    assert "Стан: призупинено" in message.answer.call_args.args[0]
    await menu_click("Відновити")
    assert "Стан: активний" in message.answer.call_args.args[0]
    await telegram.menu_action(SimpleNamespace(
        data=old_pause, from_user=actor, message=message, id="replayed-pause", answer=AsyncMock(),
    ))
    assert "Стан: активний" in message.answer.call_args.args[0]
    await menu_click("Завершити")
    assert "Завершити поточний розклад?" in message.answer.call_args.args[0]
    cancelled_end = next(
        button.callback_data for row in buttons() for button in row if button.text == "Підтвердити завершення"
    )
    await menu_click("Скасувати")
    await telegram.menu_action(SimpleNamespace(
        data=cancelled_end, from_user=actor, message=message,
        id="cancelled-end", answer=AsyncMock(),
    ))
    assert "Стан: активний" in message.answer.call_args.args[0]
    await menu_click("Завершити")
    await menu_click("Підтвердити завершення")
    assert "немає активного розкладу" in message.answer.call_args.args[0]
    with factory() as db:
        plans = db.query(models.AIPlan).filter_by(user_id=user_id).order_by(models.AIPlan.cycle_number).all()
        assert [plan.total_days for plan in plans] == [7, 14, 7]
        assert all(plan.status == "abandoned" for plan in plans)
        assert db.query(models.ChatHistory).filter_by(user_id=user_id).count() == 0

    message.text = "Мої дані"
    await telegram.menu_text(message)
    await menu_click("Виправити дані")
    await menu_click("Часовий пояс")
    await menu_click("UTC+04:00")
    assert "немає активного розкладу" in message.answer.call_args.args[0]
    await menu_click("Почати нові 7 днів")
    await menu_click("Підтвердити")
    assert "Стан: активний" in message.answer.call_args.args[0]
    with factory() as db:
        assert db.query(models.AIPlan).filter_by(user_id=user_id).count() == 4
        assert db.query(models.AIPlan).filter_by(user_id=user_id, status="active").count() == 1
        assert db.get(models.OnboardingProgress, user_id).stage == "COMPLETED"


def test_concurrent_confirmation_and_calendar_replay(engine):
    from concurrent.futures import ThreadPoolExecutor

    from app import lifecycle

    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as db:
        user, deployment, notice = subject(db)
        deployment.default_timezone = "Europe/Kyiv"
        privacy.acknowledge_notice(db, user.id, notice.id, "parallel:notice")
        progress = onboarding.setup_progress(db, user)
        onboarding.handle_action(db, user, progress, progress.revision, "start")
        screen = onboarding.handle_text(user, progress, "23:59")
        revision = progress.revision
        action = screen.keyboard.inline_keyboard[0][0].callback_data.split(":")[2]
        user_id = user.id
        db.commit()

    def confirm():
        with factory() as db:
            access.require_access(db, user_id)
            user = db.get(models.User, user_id)
            progress = onboarding.setup_progress(db, user)
            if progress is None:
                result = onboarding.recover_confirmation(db, user, revision, action)
            else:
                _, result = onboarding.handle_action(db, user, progress, revision, action)
            db.commit()
            return result.plan_id

    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(lambda _: confirm(), range(2)))
    assert ids[0] == ids[1]
    with factory() as db:
        plan = db.get(models.AIPlan, ids[0])
        original_version = plan.version
        result = lifecycle.change_schedule_calendar(
            db, user_id=user_id, plan_id=plan.id, plan_version=original_version,
            source_operation_id="calendar:retry-proof", active_days=["MON", "WED", "SAT"],
        )
        db.commit()
        dates = [(step.id, step.scheduled_for, step.expires_at) for day in plan.days for step in day.steps]
    with factory() as db:
        result = lifecycle.change_schedule_calendar(
            db, user_id=user_id, plan_id=ids[0], plan_version=original_version,
            source_operation_id="calendar:retry-proof", active_days=["MON", "WED", "SAT"],
        )
        assert result.duplicate and result.code == "replayed"
        plan = db.get(models.AIPlan, ids[0])
        assert [(step.id, step.scheduled_for, step.expires_at) for day in plan.days for step in day.steps] == dates
        assert db.query(models.AIPlan).filter_by(user_id=user_id).count() == 1
        db.commit()


async def test_failed_confirmation_rolls_back_the_entire_first_plan(engine, monkeypatch):
    from app import telegram
    from app.plan_finalization import FinalizationError

    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as db:
        user, deployment, notice = subject(db)
        deployment.default_timezone = "Europe/Kyiv"
        privacy.acknowledge_notice(db, user.id, notice.id, "rollback:notice")
        progress = onboarding.setup_progress(db, user)
        onboarding.handle_action(db, user, progress, progress.revision, "start")
        screen = onboarding.handle_text(user, progress, "14:30")
        callback_data = screen.keyboard.inline_keyboard[0][0].callback_data
        tg_id, user_id, revision = user.tg_id, user.id, progress.revision
        db.commit()

    original = onboarding.activate_plan

    def fail_after_finalization(*args, **kwargs):
        original(*args, **kwargs)
        raise FinalizationError("simulated failure after finalization, before commit")

    monkeypatch.setattr(onboarding, "activate_plan", fail_after_finalization)
    monkeypatch.setattr(telegram, "SessionLocal", factory)
    callback = SimpleNamespace(
        data=callback_data, from_user=SimpleNamespace(id=tg_id),
        message=SimpleNamespace(chat=SimpleNamespace(id=tg_id, type="private"), answer=AsyncMock()),
        answer=AsyncMock(),
    )
    await telegram.setup_action(callback)
    assert "Спробуй підтвердити" in callback.answer.call_args.args[0]
    with factory() as db:
        assert db.query(models.AIPlan).filter_by(user_id=user_id).count() == 0
        assert db.query(models.PlanLifecycleOperation).filter_by(user_id=user_id).count() == 0
        progress = db.get(models.OnboardingProgress, user_id)
        assert progress.stage == "CONFIRM" and progress.revision == revision
        assert progress.completed_at is None


async def test_revoked_actor_retains_rights_but_cannot_use_old_plan_buttons(engine, monkeypatch):
    from app import telegram

    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as db:
        user, deployment, notice = subject(db)
        privacy.acknowledge_notice(db, user.id, notice.id, "revoke:notice")
        user_id, tg_id = user.id, user.tg_id
        enrollment = db.query(models.DeploymentEnrollment).filter_by(user_id=user_id).one()
        db.get(models.AccessEntitlement, enrollment.entitlement_id).revoked_at = privacy.utcnow()
        db.commit()

    monkeypatch.setattr(telegram, "SessionLocal", factory)
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=tg_id), chat=SimpleNamespace(id=tg_id, type="private"),
        text="Підтримка", message_id=10, answer=AsyncMock(),
    )
    await telegram.menu_text(message)
    assert privacy.SUPPORT_CONTACT in message.answer.call_args.args[0]
    await telegram.menu_action(SimpleNamespace(
        from_user=message.from_user, message=message, data="menu:plan:pause:1:1",
        id="old-plan", answer=AsyncMock(),
    ))
    assert "недоступний" in message.answer.call_args.args[0]
    await telegram.menu_action(SimpleNamespace(
        from_user=message.from_user, message=message, data="menu:export",
        id="export-after-revoke", answer=AsyncMock(),
    ))
    assert "Запит" in message.answer.call_args.args[0]
    with factory() as db:
        assert db.query(models.PrivacyRequest).filter_by(user_id=user_id, kind="export").count() == 1
        assert db.query(models.AIPlan).filter_by(user_id=user_id).count() == 0
        assert db.query(models.ChatHistory).filter_by(user_id=user_id).count() == 0


def test_direct_feedback_keeps_exact_text_and_enforces_ownership(engine):
    import pytest
    from sqlalchemy.exc import DBAPIError
    from app import menu_input

    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as db:
        user, deployment, notice = subject(db)
        privacy.acknowledge_notice(db, user.id, notice.id, "direct-feedback:notice")
        menu_input.open_prompt(db, user, notice.id, "feedback", floor=30)
        row = db.get(models.MenuInput, user.id)
        assert row.enrollment_id == access.require_access(db, user.id).id
        menu_input.handle_text(db, user, notice.id, 31, "  Точний текст відгуку.  ")
        db.commit()
        request = privacy.receive_request(
            db, tg_id=user.tg_id, kind="export", source_operation_id="direct-feedback:export",
        )
        db.commit()
        exported = privacy.export_request(db, request.id)
        assert exported["feedback_events"][0]["extracted_text"] == "  Точний текст відгуку.  "
        assert exported["chat_history"] == []
        with pytest.raises(DBAPIError):
            with db.begin_nested():
                db.add(models.FeedbackEvent(
                    user_id=user.id, source="product_feedback", source_operation_id="telegram:feedback:0:32",
                    telegram_message_id=32, value="submitted", extracted_text="forged source",
                ))
                db.flush()


async def test_setup_day_draft_survives_profile_zone_edit_and_stop(engine, monkeypatch):
    from app import telegram

    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as db:
        user, deployment, notice = subject(db)
        deployment.default_timezone = "Europe/Kyiv"
        privacy.acknowledge_notice(db, user.id, notice.id, "setup-helper:notice")
        progress = onboarding.setup_progress(db, user)
        onboarding.handle_action(db, user, progress, progress.revision, "start")
        onboarding.handle_text(user, progress, "14:30")
        onboarding.handle_action(db, user, progress, progress.revision, "settings")
        onboarding.handle_action(db, user, progress, progress.revision, "days")
        onboarding.handle_action(db, user, progress, progress.revision, "day_5")
        original_mask = progress.draft_days
        user_id, tg_id = user.id, user.tg_id
        db.commit()
    monkeypatch.setattr(telegram, "SessionLocal", factory)
    monkeypatch.setattr(access, "outbound_claim", lambda _user_id: True)
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=tg_id, username=None, first_name=None),
        chat=SimpleNamespace(type="private", id=tg_id), text="Мої дані", message_id=1,
        answer=AsyncMock(),
    )

    async def click(label):
        keyboard = message.answer.call_args.kwargs["reply_markup"].inline_keyboard
        data = next(button.callback_data for row in keyboard for button in row if button.text == label)
        await telegram.menu_action(SimpleNamespace(
            from_user=message.from_user, message=message, data=data, id=label, answer=AsyncMock(),
        ))

    await telegram.menu_text(message)
    await click("Виправити дані")
    await click("Часовий пояс")
    await click("UTC+03:00")
    assert "Вибери дні" in message.answer.call_args.args[0]
    await telegram.stop_setup(message)
    assert "призупинено" in message.answer.call_args.args[0]
    message.text = "/start"
    await telegram.cmd_start(message)
    assert "Вибери дні" in message.answer.call_args.args[0]
    with factory() as db:
        user = db.get(models.User, user_id)
        assert user.timezone == "Etc/GMT-3"
        assert user.onboarding_progress.stage == "EDIT_DAYS"
        assert user.onboarding_progress.draft_days == original_mask
        assert db.query(models.AIPlan).filter_by(user_id=user_id).count() == 0

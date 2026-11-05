"""Independent-review regressions, using only new UUID PostgreSQL databases."""
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from aiogram.dispatcher.event.handler import FilterObject
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy.orm import sessionmaker

from app import access, db as models, lifecycle, menu_input, onboarding, privacy, scheduler, telegram
from app.lifecycle_reconciliation import reconcile_scheduler_effects
from tests.test_wp_04_2_privacy_postgres import engine, pytestmark, subject  # noqa: F401


def prepared(factory):
    with factory() as db:
        user, deployment, notice = subject(db)
        deployment.default_timezone = "Europe/Kyiv"
        privacy.acknowledge_notice(db, user.id, notice.id, f"review-fixes:notice:{user.id}")
        progress = onboarding.setup_progress(db, user)
        onboarding.handle_action(db, user, progress, progress.revision, "start")
        screen = onboarding.handle_text(user, progress, "23:59")
        progress.last_text_message_id = 100
        db.commit()
        return user.id, user.tg_id, notice.id, screen.keyboard.inline_keyboard[0][0].callback_data


async def test_dispatcher_orders_setup_before_confirm_without_blocking_menu_on_coach(engine, monkeypatch):
    factory = sessionmaker(engine, expire_on_commit=False)
    uid, tg_id, _, confirm = prepared(factory)
    for module in (telegram, models, scheduler):
        monkeypatch.setattr(module, "SessionLocal", factory)
    monkeypatch.setattr(access, "outbound_claim", lambda _: True)
    monkeypatch.setattr(telegram.bot, "session", AsyncMock(return_value=True))
    monkeypatch.setattr(telegram, "_send_agent_response", AsyncMock())
    jobs = BackgroundScheduler()
    jobs.start(paused=True)
    monkeypatch.setattr(scheduler, "scheduler", jobs)
    actor = User(id=tg_id, is_bot=False, first_name="Fixture")
    chat = Chat(id=tg_id, type="private")
    now = datetime.now(timezone.utc)
    text = Message(message_id=102, date=now, chat=chat, from_user=actor, text="Чи можна змінити час?")
    prompt = Message(message_id=101, date=now, chat=chat,
                     from_user=User(id=123456789, is_bot=True, first_name="Bot"), text="Підтвердити")
    callback = CallbackQuery(id="fix-confirm", from_user=actor, chat_instance="fixture", message=prompt, data=confirm)
    parked, release = asyncio.Event(), asyncio.Event()
    original = FilterObject.call

    async def delayed_filter(self, *args, **kwargs):
        if args and isinstance(args[0], Message) and args[0].message_id == 102 and self.magic is not None and not parked.is_set():
            parked.set()
            await release.wait()
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(FilterObject, "call", delayed_filter)
    entered_coach, release_coach = asyncio.Event(), asyncio.Event()

    async def slow_coach(*args, **kwargs):
        entered_coach.set()
        await release_coach.wait()
        return {"reply_text": "fixture reply"}

    coach = AsyncMock(side_effect=slow_coach)
    monkeypatch.setattr(telegram, "handle_incoming_message", coach)
    tasks = []
    try:
        tasks.append(asyncio.create_task(telegram.dp.feed_update(telegram.bot, Update(update_id=1000, message=text))))
        await asyncio.wait_for(parked.wait(), 10)
        tasks.append(asyncio.create_task(telegram.dp.feed_update(telegram.bot, Update(update_id=1001, callback_query=callback))))
        # The second update has entered the real outer middleware but cannot
        # reach activation while the earlier text is parked in a filter.
        await asyncio.sleep(0)
        with factory() as db:
            assert db.get(models.OnboardingProgress, uid).stage == "CONFIRM"
        assert not tasks[1].done()
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 20)
        with factory() as db:
            assert db.get(models.OnboardingProgress, uid).stage == "COMPLETED"
            assert db.get(models.OnboardingProgress, uid).last_text_message_id == 102
            assert db.query(models.AIPlan).filter_by(user_id=uid).count() == 1
            assert db.query(models.ChatHistory).filter_by(user_id=uid).count() == 0
        coach.assert_not_awaited()
        # A genuine post-setup conversation remains available. Waiting for its
        # model response must not hold a menu/rights request behind that call.
        genuine = text.model_copy(update={"message_id": 103, "text": "Нове запитання після налаштувань"})
        tasks.append(asyncio.create_task(telegram.dp.feed_update(telegram.bot, Update(update_id=1002, message=genuine))))
        await asyncio.wait_for(entered_coach.wait(), 10)
        menu = text.model_copy(update={"message_id": 104, "text": "Мій розклад"})
        await asyncio.wait_for(telegram.dp.feed_update(telegram.bot, Update(update_id=1003, message=menu)), 10)
        assert not release_coach.is_set()
        release_coach.set()
        await asyncio.wait_for(tasks[-1], 10)
        assert coach.await_count == 1
    finally:
        release.set()
        release_coach.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        jobs.shutdown()


@pytest.mark.parametrize("choice", [{"active_days": ["MON", "WED", "SAT"]}, {"timezone_override": "Etc/GMT-3"}])
async def test_same_calendar_retry_repairs_jobs_without_reanchoring(engine, monkeypatch, choice):
    factory = sessionmaker(engine, expire_on_commit=False)
    uid, _, _, confirm = prepared(factory)
    jobs = BackgroundScheduler()
    jobs.start(paused=True)
    monkeypatch.setattr(scheduler, "SessionLocal", factory)
    monkeypatch.setattr(models, "SessionLocal", factory)
    monkeypatch.setattr(scheduler, "scheduler", jobs)
    try:
        with factory() as db:
            user = db.get(models.User, uid)
            _, result = onboarding.handle_action(db, user, user.onboarding_progress, int(confirm.split(':')[1]), confirm.split(':')[2])
            db.commit()
        assert reconcile_scheduler_effects(result).external_effects_succeeded
        with factory() as db:
            plan = lifecycle.get_current_plan(db, uid)
            result = lifecycle.change_schedule_calendar(db, user_id=uid, plan_id=plan.id, plan_version=plan.version,
                source_operation_id="calendar:failure", **choice)
            db.commit()
            pid, version = plan.id, plan.version
            dates = {step.id: (step.scheduled_for, step.expires_at) for day in plan.days for step in day.steps}
        schedule_step = scheduler.schedule_plan_step
        monkeypatch.setattr(scheduler, "schedule_plan_step", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("fixture transient jobstore error")))
        hint = await telegram._reconcile_menu_schedule(result)
        assert "повторно застосуй ті самі дні або часовий пояс" in hint
        monkeypatch.setattr(scheduler, "schedule_plan_step", schedule_step)
        with factory() as db:
            retry = lifecycle.change_schedule_calendar(db, user_id=uid, plan_id=pid, plan_version=version,
                source_operation_id="calendar:fresh-retry", **choice)
            db.commit()
            assert retry.code == "unchanged" and not retry.applied and retry.effects
        assert await telegram._reconcile_menu_schedule(retry) == ""
        with factory() as db:
            plan = db.get(models.AIPlan, pid)
            assert plan.version == version
            assert {step.id: (step.scheduled_for, step.expires_at) for day in plan.days for step in day.steps} == dates
            assert all(jobs.get_job(scheduler._generate_step_job_id(step)).next_run_time == step.scheduled_for
                       for day in plan.days for step in day.steps)
    finally:
        jobs.shutdown()


def test_export_includes_retained_menu_state_only_for_verified_subject(engine):
    factory = sessionmaker(engine, expire_on_commit=False)
    uid, tg_id, notice_id, _ = prepared(factory)
    other_uid, _, other_notice_id, _ = prepared(factory)
    with factory() as db:
        for user_id, nid, days in ((uid, notice_id, 127), (other_uid, other_notice_id, 1)):
            user = db.get(models.User, user_id)
            row = menu_input.open_prompt(db, user, nid, "calendar_days", floor=200)
            row.draft_days = days
            row.action = None
            row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.commit()
        case = privacy.receive_request(db, tg_id=tg_id, kind="export", source_operation_id="fix:menu-export")
        db.commit()
        data = privacy.export_request(db, case.id)
        assert len(data["menu_input"]) == 1
        assert data["menu_input"][0]["user_id"] == uid
        assert data["menu_input"][0]["draft_days"] == 127
        assert data["menu_input"][0]["action"] is None
        assert data["menu_input"][0]["expires_at"] < datetime.now(timezone.utc)

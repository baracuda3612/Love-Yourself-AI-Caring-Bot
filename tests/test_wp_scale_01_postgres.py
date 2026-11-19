"""Disposable PostgreSQL rehearsals for cross-package runtime isolation."""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.background import BackgroundScheduler
from httpx import ASGITransport, AsyncClient
from sqlalchemy.orm import sessionmaker

from app import access_proof, api, db as models, onboarding, privacy, scheduled_delivery, telemetry
from tests.test_wp_04_2_privacy_postgres import engine, pytestmark, subject  # noqa: F401


async def test_first_plan_is_scheduled_before_due_after_delivery_reenable(engine, monkeypatch):
    from app import access_http, scheduler, telegram

    factory = sessionmaker(engine, expire_on_commit=False)
    for module in (access_http, models, scheduler, telegram):
        monkeypatch.setattr(module, "SessionLocal", factory)
    monkeypatch.setattr(access_http.settings, "ACCESS_OPERATOR_SECRET", "disposable-operator")
    monkeypatch.setattr(access_proof, "rate_limit", lambda *args, **kwargs: None)
    monkeypatch.setattr(scheduler, "_event_loop", asyncio.get_running_loop())

    with factory.begin() as db:
        user, deployment, notice = subject(db)
        deployment.delivery_enabled = False
        privacy.acknowledge_notice(db, user.id, notice.id, "scale:first-plan:notice")
        user_id, tg_id, deployment_id = user.id, user.tg_id, deployment.id

    jobs = BackgroundScheduler(
        jobstores={
            "default": SQLAlchemyJobStore(engine=engine),
            "delivery_recovery": MemoryJobStore(),
            "completion_recovery": MemoryJobStore(),
        },
        timezone="UTC",
    )
    jobs.start(paused=True)
    monkeypatch.setattr(scheduler, "scheduler", jobs)
    actor = SimpleNamespace(id=tg_id, username=None, first_name=None)
    message = SimpleNamespace(
        from_user=actor, chat=SimpleNamespace(id=tg_id, type="private"),
        text="/start", message_id=1, answer=AsyncMock(),
    )

    async def click():
        keyboard = message.answer.call_args.kwargs["reply_markup"].inline_keyboard
        await telegram.setup_action(SimpleNamespace(
            data=keyboard[0][0].callback_data, from_user=actor, message=message, answer=AsyncMock(),
        ))

    try:
        await telegram.cmd_start(message)
        await click()
        message.text = "14:34"
        message.message_id += 1
        await telegram.on_text(message)
        await click()

        with factory() as db:
            assert db.get(models.OnboardingProgress, user_id).stage == "COMPLETED"
            plan = db.query(models.AIPlan).filter_by(user_id=user_id, status="active").one()
            assert db.query(models.PlanLifecycleOperation).filter_by(
                user_id=user_id, source_operation_id=onboarding.FIRST_PLAN_OPERATION,
            ).count() == 1
            first = min((step for day in plan.days for step in day.steps), key=lambda step: step.scheduled_for)
            step_id, due = first.id, first.scheduled_for
            job_id = scheduler._generate_step_job_id(first)
            assert due > datetime.now(timezone.utc)
            assert db.query(models.ExerciseDelivery).filter_by(plan_step_id=step_id).count() == 0

        async with AsyncClient(transport=ASGITransport(app=api.app), base_url="http://testserver") as client:
            reply = await client.post(
                "/access/deployment/controls",
                headers={"Authorization": "Bearer disposable-operator"},
                json={"deployment_id": deployment_id, "enrollment_open": True, "delivery_enabled": True},
            )
        assert reply.status_code == 200
        assert datetime.now(timezone.utc) < due
        with factory() as db:
            assert db.get(models.Deployment, deployment_id).delivery_enabled
        exact_job = jobs.get_job(job_id)
        assert exact_job is not None
        assert exact_job.next_run_time == due
        assert exact_job.args == (tg_id, "", step_id)

        # Invoke the exact persisted job at its recorded due instant. The bot
        # transport is controlled and the claim/receipt transactions are real.
        class DueClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return due.astimezone(tz) if tz else due.replace(tzinfo=None)

        monkeypatch.setattr(scheduler, "datetime", DueClock)
        monkeypatch.setattr(scheduled_delivery, "datetime", DueClock)
        monkeypatch.setattr(telemetry, "datetime", DueClock)
        receipt = SimpleNamespace(
            chat=SimpleNamespace(id=tg_id), message_id=123, date=due,
        )
        send = AsyncMock(return_value=receipt)
        monkeypatch.setattr(telegram, "bot", SimpleNamespace(
            id=1, send_message=send, send_animation=send,
        ))
        await asyncio.to_thread(exact_job.func, *exact_job.args)
        await asyncio.to_thread(exact_job.func, *exact_job.args)
        send.assert_awaited_once()
        with factory() as db:
            attempt = db.query(models.ExerciseDelivery).filter_by(plan_step_id=step_id).one()
            assert (attempt.attempt, attempt.state, attempt.message_id) == (1, "delivered", 123)
            assert attempt.started_at == due
            assert db.get(models.AIPlanStep, step_id).step_status == "delivered"
            assert db.query(models.UserEvent).filter_by(plan_step_id=step_id, event_name="task_delivered").count() == 1
    finally:
        jobs.shutdown()

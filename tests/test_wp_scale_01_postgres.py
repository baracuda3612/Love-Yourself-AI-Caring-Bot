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
    monkeypatch.setattr(scheduler, "ReceiptSessionLocal", factory)
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


async def test_enrollment_gate_progresses_during_another_owners_transaction(engine, monkeypatch):
    from uuid import uuid4

    from app import telegram
    from tests.access_fixtures import enroll

    factory = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(telegram, "SessionLocal", factory)
    with factory.begin() as db:
        first, deployment, notice = subject(db)
        second = models.User(tg_id=uuid4().int % 10**12, is_active=True)
        db.add(second)
        db.flush()
        enroll(db, second, target=deployment)
        privacy.acknowledge_notice(db, first.id, notice.id, "scale:first-owner")
        first_id, second_tg_id = first.id, second.tg_id

    callback = SimpleNamespace(
        data="task_complete:1", from_user=SimpleNamespace(id=second_tg_id),
        message=SimpleNamespace(chat=SimpleNamespace(id=second_tg_id, type="private")),
        answer=AsyncMock(),
    )
    handler = AsyncMock(return_value="entered")
    holder = factory()
    gate = None
    try:
        privacy.pinned_notice(holder, first_id, acknowledged=True)
        gate = asyncio.create_task(telegram.EnrollmentMiddleware()(handler, callback, {}))
        assert await asyncio.wait_for(asyncio.shield(gate), 1.0) == "entered"
        handler.assert_awaited_once_with(callback, {})
    finally:
        holder.close()
        if gate is not None:
            await gate


async def test_delivery_receipt_has_checkout_capacity_while_general_pool_is_full(engine, monkeypatch):
    from contextlib import ExitStack
    from time import monotonic

    from sqlalchemy import create_engine

    from app import scheduler, telegram
    from tests.test_wp_03_4_postgres_delivery import active_step

    ordinary = create_engine(engine.url, pool_size=2, max_overflow=0, pool_timeout=2)
    receipts = create_engine(engine.url, pool_size=1, max_overflow=0, pool_timeout=2)
    app_sessions = sessionmaker(ordinary, expire_on_commit=False)
    receipt_sessions = sessionmaker(receipts, expire_on_commit=False)
    monkeypatch.setattr(scheduler, "SessionLocal", app_sessions)
    monkeypatch.setattr(scheduler, "ReceiptSessionLocal", receipt_sessions)
    monkeypatch.setattr(scheduler, "_event_loop", asyncio.get_running_loop())

    with app_sessions.begin() as db:
        user, _, step = active_step(db)
        user.profile.active_days = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
        tg_id, step_id = user.tg_id, step.id

    sending = asyncio.Event()
    confirm = asyncio.Event()

    async def send(**_kwargs):
        sending.set()
        await confirm.wait()
        return SimpleNamespace(chat=SimpleNamespace(id=tg_id), message_id=4321, date=datetime.now(timezone.utc))

    transport = AsyncMock(side_effect=send)
    monkeypatch.setattr(telegram, "bot", SimpleNamespace(
        id=1, send_message=transport, send_animation=transport,
    ))
    send_job = asyncio.create_task(asyncio.to_thread(scheduler.send_scheduled_message, tg_id, "", step_id))
    try:
        await asyncio.wait_for(sending.wait(), 2)
        with ExitStack() as full:
            full.enter_context(ordinary.connect())
            full.enter_context(ordinary.connect())
            assert ordinary.pool.checkedout() == 2
            start = monotonic()
            confirm.set()
            while True:
                with engine.connect() as conn:
                    state = conn.execute(
                        models.ExerciseDelivery.__table__.select().where(models.ExerciseDelivery.plan_step_id == step_id)
                    ).one()
                if state.state == "delivered":
                    assert state.message_id == 4321
                    break
                assert monotonic() - start < 1.5, "receipt waited for the occupied general pool"
                await asyncio.sleep(0.01)
            print(f"receipt_write_with_general_pool_full_ms={(monotonic() - start)*1000:.1f}")
    finally:
        confirm.set()
        await send_job
        ordinary.dispose()
        receipts.dispose()
    transport.assert_awaited_once()


async def test_erasure_resumes_jobstore_scan_at_persisted_cursor_after_restart(engine, monkeypatch):
    from datetime import timedelta

    from app import scheduler, session_memory
    from app.runtime_jobstore import RuntimeJobStore
    from tests.test_wp_03_5_postgres_completion import source_plan

    factory = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(scheduler, "SessionLocal", factory)
    monkeypatch.setattr(models, "SessionLocal", factory)
    monkeypatch.setattr(session_memory, "SessionMemory", lambda: SimpleNamespace(delete_user=AsyncMock()))
    with factory.begin() as db:
        user, plan = source_plan(db)
        step = plan.days[0].steps[0]
        request = privacy.receive_request(
            db, tg_id=user.tg_id, kind="deletion", source_operation_id="scale:jobstore-erase",
        )
        privacy.begin_account_deletion(db, request.id)
        ids = (user.id, plan.id, step.id, user.tg_id, request.id)
    user_id, plan_id, step_id, chat_id, request_id = ids

    def start():
        store = RuntimeJobStore(engine=engine, tablename="wp_scale_job_scan")
        jobs = BackgroundScheduler(jobstores={"default": store})
        jobs.start(paused=True)
        monkeypatch.setattr(scheduler, "scheduler", jobs)
        monkeypatch.setattr(scheduler, "_application_scheduler", jobs)
        monkeypatch.setattr(scheduler, "jobstores", {"default": store})
        return jobs

    jobs = start()
    tomorrow = datetime.now(timezone.utc) + timedelta(days=1)
    unrelated_ids = {f"a-{number:03}" for number in range(127)} | {"z-unrelated"}
    try:
        for job_id in unrelated_ids:
            jobs.add_job("app.scheduler:check_plan_completions", "date", id=job_id, run_date=tomorrow)
        jobs.add_job("app.scheduler:check_plan_completions", "date", id=f"plan_{plan_id}_old", run_date=tomorrow)
        jobs.add_job("app.scheduler:send_scheduled_message", "date", id="z-custom-legacy",
                     args=[chat_id, "", step_id], run_date=tomorrow)
        await scheduler.privacy_maintenance()
        assert jobs.get_job(f"plan_{plan_id}_old") is None
        assert jobs.get_job("z-custom-legacy") is not None
        with factory() as db:
            request = db.get(models.PrivacyRequest, request_id)
            assert request.state == "deleting"
            assert request.evidence["job_scan_after"] == f"plan_{plan_id}_old"
            assert db.get(models.User, user_id) is not None
    finally:
        jobs.shutdown()

    jobs = start()
    try:
        await scheduler.privacy_maintenance()
        assert {job.id for job in jobs.get_jobs()} == unrelated_ids
        with factory() as db:
            assert db.get(models.User, user_id) is None
            request = db.get(models.PrivacyRequest, request_id)
            assert request.state == "completed"
            assert request.evidence == {"result": "account_erased"}
    finally:
        jobs.shutdown()


async def test_feedback_commit_does_not_wait_for_the_previous_telegram_edit(engine, monkeypatch):
    from datetime import timedelta
    from threading import Event

    from app import exercise_status, lifecycle, scheduler, telegram
    from tests.test_wp_03_4_postgres_delivery import deliver
    from tests.test_wp_03_5_postgres_completion import source_plan

    factory = sessionmaker(engine, expire_on_commit=False)
    for module in (scheduler, telegram):
        monkeypatch.setattr(module, "SessionLocal", factory)
    monkeypatch.setattr(scheduler, "_enable_delivery_recovery", lambda: None)

    with factory.begin() as db:
        user, plan = source_plan(db)
        step = plan.days[0].steps[0]
        now = datetime.now(timezone.utc)
        step.scheduled_for = now - timedelta(minutes=1)
        step.expires_at = now + timedelta(hours=1)
        db.flush()
        deliver(db, step)
        lifecycle.transition_plan_step(
            db, user_id=user.id, step_id=step.id, target_status="completed",
            source_operation_id="scale:project-completed",
        )
        tg_id, step_id = user.tg_id, step.id

    editing = asyncio.Event()
    release = Event()
    runtime_loop = asyncio.get_running_loop()
    projections = []

    class Bot:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def edit_message_text(self, **kwargs):
            projections.append(kwargs["reply_markup"])
            if len(projections) == 1:
                runtime_loop.call_soon_threadsafe(editing.set)
                assert await asyncio.to_thread(release.wait, 3)

    monkeypatch.setattr(exercise_status, "Bot", Bot)
    worker = asyncio.create_task(asyncio.to_thread(scheduler.reconcile_terminal_step_keyboards, [step_id]))
    try:
        await asyncio.wait_for(editing.wait(), 2)
        callback = SimpleNamespace(
            id="scale-feedback", data=f"task_feedback:{step_id}:better",
            from_user=SimpleNamespace(id=tg_id), answer=AsyncMock(),
        )
        await asyncio.wait_for(telegram.handle_task_feedback(callback), 1.0)
        callback.answer.assert_awaited_once_with("Відгук збережено: краще")
        assert not worker.done()
        with factory() as db:
            assert db.query(models.FeedbackEvent).filter_by(plan_step_id=step_id, source="exercise_efficacy").one().value == "better"
    finally:
        release.set()
        await worker

    await asyncio.to_thread(scheduler.reconcile_terminal_step_keyboards, [step_id])
    assert len(projections) == 2 and projections[0] is not None and projections[1] is None
    with factory() as db:
        receipt = db.query(models.ExerciseDelivery).filter_by(plan_step_id=step_id).one()
        assert receipt.visible_status == "completed" and receipt.visible_feedback == "better"

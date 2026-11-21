"""Disposable PostgreSQL rehearsals for cross-package runtime isolation."""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.background import BackgroundScheduler
from httpx import ASGITransport, AsyncClient
from sqlalchemy.orm import sessionmaker

from app import access_proof, api, db as models, onboarding, privacy, scheduled_delivery, telemetry
from tests.test_wp_04_2_privacy_postgres import engine, pytestmark, subject  # noqa: F401


@pytest.mark.parametrize("saturated", [False, True])
async def test_first_plan_is_scheduled_before_due_after_delivery_reenable(engine, monkeypatch, saturated):
    from threading import Event
    from time import monotonic
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
        privacy.acknowledge_notice(db, user.id, notice.id, f"scale:first-plan:notice:{user.id}")
        user_id, tg_id, deployment_id = user.id, user.tg_id, deployment.id

    def start_scheduler():
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
        return jobs

    jobs = start_scheduler()
    release = Event()
    held = [telegram.schedule_effects_lane.submit(release.wait, 5) for _ in range(2)] if saturated else []
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
            assert (plan.schedule_reconcile_at is not None) == saturated
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
        if saturated:
            assert jobs.get_job(job_id) is None
            jobs.shutdown()
            jobs = start_scheduler()
            recovery_start = monotonic()
            await asyncio.to_thread(scheduler.dispatch_plan_schedule_recovery)
            while True:
                with factory() as db:
                    repaired = db.get(models.AIPlan, plan.id).schedule_reconcile_at is None
                if repaired:
                    break
                assert monotonic() - recovery_start < 3.0
                await asyncio.sleep(0.02)
            print(f"admitted_schedule_repair_after_restart_ms={(monotonic() - recovery_start)*1000:.1f}")
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
        release.set()
        for worker in held:
            worker.result(timeout=3)
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


async def test_dispatcher_action_arriving_before_deadline_wins_over_expiry(engine, monkeypatch):
    from datetime import timedelta
    from threading import Event

    from aiogram.methods import AnswerCallbackQuery
    from aiogram.types import Update

    from app import scheduler, step_action_ingress, telegram
    from tests.test_wp_03_4_postgres_delivery import deliver
    from tests.test_wp_03_5_postgres_completion import source_plan

    factory = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(telegram, "SessionLocal", factory)
    monkeypatch.setattr(scheduler, "SessionLocal", factory)
    monkeypatch.setattr(scheduler, "_expiry_after", 0)
    monkeypatch.setattr(scheduler, "_submit_continuation", lambda *_a: None)
    monkeypatch.setattr(scheduler, "enable_completion_recovery", lambda: None)
    monkeypatch.setattr(scheduler, "request_plan_completion", lambda *_a: None)
    monkeypatch.setattr(scheduler, "_dispatch_terminal_projections", lambda _ids: None)
    monkeypatch.setattr(scheduler, "_enable_delivery_recovery", lambda: None)
    monkeypatch.setattr(telegram, "_project_step_status", AsyncMock())

    with factory.begin() as db:
        def delivered_step():
            owner, plan = source_plan(db)
            step = plan.days[0].steps[0]
            now = datetime.now(timezone.utc)
            step.scheduled_for = now - timedelta(minutes=1)
            step.expires_at = now + timedelta(hours=1)
            db.flush()
            deliver(db, step)
            return owner, step

        first, first_step = delivered_step()
        second, second_step = delivered_step()
        overdue, overdue_step = delivered_step()
        received_at = datetime.now(timezone.utc)
        deadline = received_at + timedelta(milliseconds=250)
        first_step.expires_at = overdue_step.expires_at = deadline

    time_at_ingress = received_at

    class TestClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return time_at_ingress.astimezone(tz) if tz else time_at_ingress.replace(tzinfo=None)

    monkeypatch.setattr(step_action_ingress, "datetime", TestClock)

    answers = {}

    async def transport(_bot, method, **_kwargs):
        assert isinstance(method, AnswerCallbackQuery)
        answers[method.callback_query_id] = method.text
        return True

    monkeypatch.setattr(type(telegram.bot), "__call__", transport)

    async def click(owner, step, update_id, query_id):
        actor = {"id": owner.tg_id, "is_bot": False, "first_name": "Test"}
        update = Update.model_validate({
            "update_id": update_id,
            "callback_query": {
                "id": query_id, "from": actor, "chat_instance": str(owner.tg_id),
                "data": f"task_complete:{step.id}",
                "message": {"message_id": 777, "date": received_at,
                            "chat": {"id": owner.tg_id, "type": "private"}},
            },
        })
        await telegram.dp.feed_update(telegram.bot, update)

    waiting_on_owner, authorized, release_action = Event(), asyncio.Event(), asyncio.Event()
    require_enrollment, run_db = telegram._require_enrollment, telegram.run_db

    def tracked_access(tg_id):
        if tg_id == first.tg_id:
            waiting_on_owner.set()
        return require_enrollment(tg_id)

    async def held_after_access(operation, *args):
        result = await run_db(operation, *args)
        if operation is tracked_access and args == (first.tg_id,):
            authorized.set()
            await release_action.wait()
        return result

    monkeypatch.setattr(telegram, "_require_enrollment", tracked_access)
    monkeypatch.setattr(telegram, "run_db", held_after_access)
    holder = factory()
    callback = None
    try:
        holder.query(models.User).filter_by(id=first.id).with_for_update().one()
        callback = asyncio.create_task(click(first, first_step, 11, "timely"))
        assert await asyncio.to_thread(waiting_on_owner.wait, 3)
        await asyncio.sleep(max(0, (deadline - datetime.now(timezone.utc)).total_seconds()) + 0.01)
        time_at_ingress = datetime.now(timezone.utc)

        await asyncio.wait_for(click(second, second_step, 12, "unrelated"), 3)
        assert answers["unrelated"] == "Виконано"
        await asyncio.wait_for(asyncio.to_thread(scheduler.expire_overdue_steps), 3)
        with factory() as db:
            assert db.get(models.AIPlanStep, overdue_step.id).step_status == "expired"

        holder.rollback()
        await asyncio.wait_for(authorized.wait(), 3)
        # No DB lock now protects A. Its already-received update must still win.
        await asyncio.to_thread(scheduler.expire_overdue_steps)
        with factory() as db:
            assert db.get(models.AIPlanStep, first_step.id).step_status == "delivered"
        release_action.set()
        await asyncio.wait_for(asyncio.shield(callback), 3)
        await click(first, first_step, 13, "duplicate")
        await click(overdue, overdue_step, 14, "late")
        assert answers == {"unrelated": "Виконано", "timely": "Виконано",
                           "duplicate": "Виконано", "late": "Час дії завершився"}
        with factory() as db:
            assert db.get(models.AIPlanStep, first_step.id).terminal_at == received_at
            first_events = db.query(models.UserEvent.event_name).filter_by(plan_step_id=first_step.id)
            assert first_events.filter_by(event_name="task_completed").count() == 1
            assert first_events.filter_by(event_name="task_ignored").count() == 0
    finally:
        holder.close()
        release_action.set()
        if callback is not None:
            await callback


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
    from time import monotonic

    from app import scheduler, session_memory
    from app.runtime_jobstore import RuntimeJobStore
    from tests.test_wp_03_5_postgres_completion import source_plan

    factory = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(scheduler, "SessionLocal", factory)
    monkeypatch.setattr(scheduler, "_event_loop", asyncio.get_running_loop())
    monkeypatch.setattr(models, "SessionLocal", factory)
    monkeypatch.setattr(session_memory, "SessionMemory", lambda: SimpleNamespace(delete_user=AsyncMock()))
    monkeypatch.setattr(scheduler, "_PRIVACY_JOB_PAGES_PER_PASS", 1)
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
        jobs = BackgroundScheduler(jobstores={"default": store, "maintenance": MemoryJobStore()})
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
        await asyncio.to_thread(scheduler.run_privacy_maintenance)
        followup = jobs.get_job("privacy_scan_followup", jobstore="maintenance")
        assert followup is not None
        assert followup.next_run_time < datetime.now(timezone.utc) + timedelta(seconds=2)
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
        start = monotonic()
        await scheduler.privacy_maintenance()
        print(f"privacy_scan_resumed_after_restart_ms={(monotonic()-start)*1000:.1f}")
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


_timer_probe = None


def _run_timer_probe(number):
    release, started, done, lock = _timer_probe
    with lock:
        started.append(number)
    assert release.wait(5)
    with lock:
        done.append(number)


def test_busy_timer_executor_keeps_excess_jobs_in_persistent_store(engine, monkeypatch):
    from datetime import timedelta
    from threading import Event, Lock
    from time import monotonic, sleep
    import sys

    from app.runtime_jobstore import DeliveryJobExecutor, RuntimeJobStore

    release, overflow, lock = Event(), Event(), Lock()
    started, done = [], []
    monkeypatch.setattr(sys.modules[__name__], "_timer_probe", (release, started, done, lock))
    executor = DeliveryJobExecutor(4, overflow)
    store = RuntimeJobStore(engine=engine, tablename="wp_scale_timer_backlog", delivery_capacity=executor.available)
    jobs = BackgroundScheduler(jobstores={"default": store}, executors={"scheduled_delivery": executor})
    jobs.start(paused=True)

    def wait_until(condition):
        deadline = monotonic() + 4
        while not condition():
            assert monotonic() < deadline
            sleep(0.02)

    try:
        due = datetime.now(timezone.utc) - timedelta(seconds=1)
        for number in range(8):
            jobs.add_job(_run_timer_probe, "date", id=f"timer-{number}", args=[number], run_date=due,
                         executor="scheduled_delivery", misfire_grace_time=None)
        jobs.resume()
        wait_until(lambda: len(started) == 4 and len(jobs.get_jobs()) == 4)
        assert {job.id for job in jobs.get_jobs()} == {f"timer-{number}" for number in range(4, 8)}
        start = monotonic()
        release.set()
        wait_until(lambda: len(done) == 8)
        print(f"persisted_timer_backlog_drained_ms={(monotonic()-start)*1000:.1f}")
        assert sorted(done) == list(range(8))
        assert not overflow.is_set()
        wait_until(lambda: not jobs.get_jobs())
    finally:
        release.set()
        jobs.shutdown()


async def test_format_and_calendar_recover_exact_jobs_without_replaying_user_action(engine, monkeypatch):
    from time import monotonic

    from app import lifecycle, scheduler
    from tests.test_wp_03_5_postgres_completion import source_plan

    factory = sessionmaker(engine, expire_on_commit=False)
    for module in (models, scheduler):
        monkeypatch.setattr(module, "SessionLocal", factory)
    jobs = BackgroundScheduler(
        jobstores={"default": SQLAlchemyJobStore(engine=engine, tablename="wp_scale_format_jobs")},
        timezone="UTC",
    )
    jobs.start(paused=True)
    monkeypatch.setattr(scheduler, "scheduler", jobs)

    async def repair(plan_id):
        deadline = monotonic() + 3
        while True:
            await asyncio.to_thread(scheduler.dispatch_plan_schedule_recovery)
            with factory() as db:
                if db.get(models.AIPlan, plan_id).schedule_reconcile_at is None:
                    return
            assert monotonic() < deadline
            await asyncio.sleep(0.02)

    try:
        with factory.begin() as db:
            user, first = source_plan(db)
            user.profile.evening_slot_collected = True
            db.flush()
            user_id, first_id = user.id, first.id
        assert scheduler.reconcile_plan_schedule(first_id).failed_ids == ()
        old_jobs = {job.id for job in jobs.get_jobs()}
        assert old_jobs

        with factory.begin() as db:
            switch = lifecycle.switch_plan_format(
                db, user_id=user_id, target_plan_type="MEDIUM", source_operation_id="scale:switch",
            )
            assert switch.code == "applied"
            new_id = switch.plan_id
            assert db.get(models.AIPlan, new_id).schedule_switch_source == "scale:switch"
        assert {job.id for job in jobs.get_jobs()} == old_jobs
        await repair(new_id)
        with factory.begin() as db:
            plan = db.get(models.AIPlan, new_id)
            assert plan.schedule_switch_source is None
            assert lifecycle._switch_schedule_ready(db, user_id, new_id)
            initial = {scheduler._generate_step_job_id(step): step.scheduled_for
                       for day in plan.days for step in day.steps}
            assert {job.id: job.next_run_time for job in jobs.get_jobs()} == initial
            calendar = lifecycle.change_schedule_calendar(
                db, user_id=user_id, plan_id=new_id, plan_version=plan.version,
                source_operation_id="scale:calendar", active_days=["MON", "WED", "FRI"],
            )
            assert calendar.details["updated_step_ids"]
            expected = {scheduler._generate_step_job_id(step): step.scheduled_for
                        for day in plan.days for step in day.steps}
            assert expected != initial and plan.schedule_reconcile_at is not None
        assert {job.id: job.next_run_time for job in jobs.get_jobs()} == initial
        await repair(new_id)
        assert {job.id: job.next_run_time for job in jobs.get_jobs()} == expected
        with factory() as db:
            receipts = db.query(models.PlanLifecycleOperation).filter(
                models.PlanLifecycleOperation.user_id == user_id,
                models.PlanLifecycleOperation.operation.in_(["switch_plan_format", "change_schedule_calendar", "switch_schedule_ready"]),
            ).all()
            assert {receipt.operation for receipt in receipts} == {"switch_plan_format", "change_schedule_calendar", "switch_schedule_ready"}
    finally:
        jobs.shutdown()


async def test_switch_completion_preserves_concurrent_time_edit_repair(engine, monkeypatch):
    from time import monotonic

    from app import lifecycle, scheduler
    from app.plan_runtime.tools import _finish_plan_format_result
    from tests.test_wp_03_5_postgres_completion import source_plan

    factory = sessionmaker(engine, expire_on_commit=False)
    for module in (models, scheduler):
        monkeypatch.setattr(module, "SessionLocal", factory)
    jobs = BackgroundScheduler(
        jobstores={"default": SQLAlchemyJobStore(engine=engine, tablename="wp_scale_switch_race")},
        timezone="UTC",
    )
    jobs.start(paused=True)
    monkeypatch.setattr(scheduler, "scheduler", jobs)

    with factory.begin() as db:
        user, _ = source_plan(db)
        user.profile.evening_slot_collected = True
        db.flush()
        switch = lifecycle.switch_plan_format(
            db, user_id=user.id, target_plan_type="MEDIUM", source_operation_id="scale:race-switch",
        )
        assert switch.code == "applied"
        user_id, plan_id = user.id, switch.plan_id

    record_ready = lifecycle.record_switch_schedule_ready
    edited_jobs = {}

    def interleave(db, **kwargs):
        # The switch already proved its timers. Save a later time edit in
        # another transaction and leave its immediate effects unrun.
        with factory.begin() as editing_db:
            change = lifecycle.change_delivery_time(
                editing_db, user_id=user_id, slot="DAY", hhmm="18:20",
                source_operation_id="scale:concurrent-time",
            )
            assert change.details["updated_step_ids"]
            plan = editing_db.get(models.AIPlan, plan_id)
            edited_jobs.update({scheduler._generate_step_job_id(step): step.scheduled_for
                                for day in plan.days for step in day.steps})
        return record_ready(db, **kwargs)

    monkeypatch.setattr(lifecycle, "record_switch_schedule_ready", interleave)
    try:
        outcome = await asyncio.to_thread(_finish_plan_format_result, switch, pending_status="pending")
        assert outcome["status"] == "ok"
        assert {job.id: job.next_run_time for job in jobs.get_jobs()} != edited_jobs
        with factory() as db:
            plan = db.get(models.AIPlan, plan_id)
            assert lifecycle._switch_schedule_ready(db, user_id, plan_id)
            assert plan.schedule_switch_source is None
            assert plan.schedule_reconcile_at is not None

        deadline = monotonic() + 3
        while True:
            await asyncio.to_thread(scheduler.dispatch_plan_schedule_recovery)
            with factory() as db:
                if db.get(models.AIPlan, plan_id).schedule_reconcile_at is None:
                    break
            assert monotonic() < deadline
            await asyncio.sleep(0.02)

        assert {job.id: job.next_run_time for job in jobs.get_jobs()} == edited_jobs
    finally:
        jobs.shutdown()

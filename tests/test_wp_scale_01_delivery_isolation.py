"""Exercise delivery isolation at the persistent timer and recovery boundaries."""

import asyncio
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from threading import Event

from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from app import db as models, scheduler
from app.blocking_db import BlockingLane
from app.exercise_delivery import ExerciseSendResult
from app.runtime_jobstore import DeliveryJobExecutor, RuntimeJobStore
from app.scheduled_delivery import claim_send, finish_send
from tests.test_wp_03_5_postgres_completion import source_plan
from tests.test_wp_04_2_privacy_postgres import engine, pytestmark  # noqa: F401


def exercise_due(db, due):
    owner, plan = source_plan(db, work_days=["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"])
    step = plan.days[0].steps[0]
    step.scheduled_for = due - timedelta(seconds=1)
    step.expires_at = due + timedelta(hours=1)
    db.flush()
    return owner, step


def test_scheduler_survives_failed_jobstore_peek():
    failed, timer_ran = Event(), Event()

    class FaultyStore(MemoryJobStore):
        def get_next_run_time(self):
            if not failed.is_set():
                failed.set()
                raise OperationalError("next timer", {}, OSError("transient database outage"))
            return super().get_next_run_time()

    jobs = type(scheduler.scheduler)(jobstores={"default": FaultyStore()}, jobstore_retry_interval=0.05)
    jobs.add_job(timer_ran.set, "date", run_date=datetime.now(timezone.utc) + timedelta(milliseconds=250))
    jobs.start()
    try:
        assert failed.wait(1)
        assert timer_ran.wait(2)
    finally:
        jobs.shutdown()


def test_checking_recovery_again_keeps_the_original_retry_deadline(engine, monkeypatch):
    factory = sessionmaker(engine)
    started = datetime.now(timezone.utc)
    with factory() as db:
        monkeypatch.setattr(scheduler, "SessionLocal", lambda: nullcontext(db))
        _, step = exercise_due(db, started)
        claim = claim_send(db, step.id, now=started)
        finish_send(db, claim.attempt_id, ExerciseSendResult(
            "failed", "text", claim.presentation, "probe", retry_after=2,
        ), now=started)
        db.flush()
        assert scheduler._delivery_recovery_time(started + timedelta(seconds=10)) == started + timedelta(seconds=30)


async def test_recovery_skips_locked_attempt_owner_and_reconciles_next(engine, monkeypatch):
    factory = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(scheduler, "SessionLocal", factory)
    monkeypatch.setattr(scheduler, "_recovery_cursors", {"stale": 0, "retry": 0, "keyboard": 0})
    monkeypatch.setattr(scheduler, "_request_delivery_recovery", lambda: None)
    past = datetime.now(timezone.utc) - timedelta(minutes=3)
    with factory.begin() as db:
        first, first_step = exercise_due(db, past)
        _, other_step = exercise_due(db, past)
        first_claim = claim_send(db, first_step.id, now=past)
        other_claim = claim_send(db, other_step.id, now=past)

    with factory() as holder:
        holder.get(models.User, first.id, with_for_update=True)
        scan = asyncio.create_task(asyncio.to_thread(scheduler.reconcile_scheduled_deliveries, wait=False))
        try:
            await asyncio.wait_for(asyncio.shield(scan), 2)
            with factory() as db:
                assert db.get(models.ExerciseDelivery, first_claim.attempt_id).state == "in_flight"
                assert db.get(models.ExerciseDelivery, other_claim.attempt_id).state == "uncertain"
        finally:
            holder.rollback()
            await scan
    await asyncio.to_thread(scheduler.reconcile_scheduled_deliveries, wait=False)
    with factory() as db:
        assert db.get(models.ExerciseDelivery, first_claim.attempt_id).state == "uncertain"


async def test_locked_delivery_owner_leaves_unclaimed_work_for_recovery(engine, monkeypatch):
    factory = sessionmaker(engine, expire_on_commit=False)
    for name in ("SessionLocal", "ReceiptSessionLocal"):
        monkeypatch.setattr(scheduler, name, factory)
    monkeypatch.setattr(scheduler, "_event_loop", asyncio.get_running_loop())
    monkeypatch.setattr(scheduler, "_request_delivery_recovery", lambda: None)
    monkeypatch.setattr(scheduler, "_enable_delivery_recovery", lambda: None)
    monkeypatch.setattr(scheduler, "_due_after", None)
    due = datetime.now(timezone.utc)
    with factory.begin() as db:
        first, step = exercise_due(db, due)

    async def send(_bot, chat_id, presentation, **_kwargs):
        assert chat_id == first.tg_id
        return ExerciseSendResult("delivered", "text", presentation, "probe", chat_id, 777)

    monkeypatch.setattr(scheduler, "send_exercise", send)
    with factory() as holder:
        holder.get(models.User, first.id, with_for_update=True)
        delivery = asyncio.create_task(asyncio.to_thread(scheduler.send_scheduled_message, first.tg_id, "", step.id))
        try:
            await asyncio.wait_for(asyncio.shield(delivery), 1.5)
            with factory() as db:
                assert db.query(models.ExerciseDelivery).filter_by(plan_step_id=step.id).count() == 0
        finally:
            holder.rollback()
            await delivery
    await asyncio.to_thread(scheduler.reconcile_scheduled_deliveries)
    with factory() as db:
        receipt = db.query(models.ExerciseDelivery).filter_by(plan_step_id=step.id).one()
        assert receipt.state == "delivered" and receipt.message_id == 777


async def test_slow_retry_alarm_does_not_hold_four_timers(engine, monkeypatch):
    factory = sessionmaker(engine, expire_on_commit=False)
    for name in ("SessionLocal", "ReceiptSessionLocal"):
        monkeypatch.setattr(scheduler, name, factory)
    monkeypatch.setattr(scheduler, "_event_loop", asyncio.get_running_loop())
    executor = DeliveryJobExecutor(4, Event())
    store = RuntimeJobStore(engine=engine, tablename="wp_scale_wake_isolation", delivery_capacity=executor.available)
    jobs = BackgroundScheduler(
        jobstores={"default": store, "delivery_recovery": MemoryJobStore()},
        executors={"scheduled_delivery": executor},
    )
    jobs.start(paused=True)
    monkeypatch.setattr(scheduler, "scheduler", jobs)
    wake_lane = BlockingLane(1, "test-delivery-wake")
    monkeypatch.setattr(scheduler, "_delivery_wake_lane", wake_lane)
    monkeypatch.setattr(scheduler, "_delivery_wake_requested", Event())

    due = datetime.now(timezone.utc)
    with factory.begin() as db:
        early = [exercise_due(db, due) for _ in range(4)]
        healthy, healthy_step = exercise_due(db, due)
    unavailable = {owner.tg_id for owner, _ in early}
    alarm_waiting, allow_alarm, alarm_done = Event(), Event(), Event()
    alarm_time = scheduler._delivery_recovery_time

    def held_alarm(now):
        alarm_waiting.set()
        try:
            assert allow_alarm.wait(10)
            return alarm_time(now)
        finally:
            alarm_done.set()

    monkeypatch.setattr(scheduler, "_delivery_recovery_time", held_alarm)

    async def send(_bot, chat_id, presentation, **_kwargs):
        if chat_id in unavailable:
            return ExerciseSendResult("failed", "text", presentation, "probe", retry_after=30)
        assert chat_id == healthy.tg_id
        return ExerciseSendResult("delivered", "text", presentation, "probe", chat_id, 777)

    monkeypatch.setattr(scheduler, "send_exercise", send)
    try:
        for index, (owner, step) in enumerate(early):
            jobs.add_job(
                "app.scheduler:send_scheduled_message", "date", id=f"due-{index}",
                args=[owner.tg_id, "", step.id], run_date=due,
                executor="scheduled_delivery", misfire_grace_time=None,
            )
        jobs.add_job(
            "app.scheduler:send_scheduled_message", "date", id="healthy",
            args=[healthy.tg_id, "", healthy_step.id], run_date=due + timedelta(milliseconds=1),
            executor="scheduled_delivery", misfire_grace_time=None,
        )
        jobs.resume()
        assert await asyncio.to_thread(alarm_waiting.wait, 4)
        async with asyncio.timeout(2):
            while True:
                with factory() as db:
                    receipt = db.query(models.ExerciseDelivery).filter_by(plan_step_id=healthy_step.id).first()
                    if receipt is not None and receipt.state == "delivered":
                        assert receipt.message_id == 777
                        break
                await asyncio.sleep(0.025)
        assert not allow_alarm.is_set()
    finally:
        jobs.pause()
        allow_alarm.set()
        async with asyncio.timeout(8):
            while executor.available() != 4:
                await asyncio.sleep(0.025)
            while not wake_lane.available() or scheduler._delivery_wake_requested.is_set():
                await asyncio.sleep(0.025)
        await asyncio.to_thread(jobs.shutdown)
        wake_lane.shutdown()
        if alarm_waiting.is_set():
            assert await asyncio.to_thread(alarm_done.wait, 4)

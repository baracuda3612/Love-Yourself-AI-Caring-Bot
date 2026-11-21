"""Verify that unrelated owners progress while WP-SCALE-01 waits are held."""

import asyncio
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from threading import Event, current_thread
from uuid import uuid4

import pytest
from aiogram.methods import SendMessage
from aiogram.types import Update
from apscheduler.schedulers.background import BackgroundScheduler
from httpx import ASGITransport, AsyncClient
from sqlalchemy.orm import sessionmaker

from app import access, access_http, api, db as models, exercise_status, scheduler, telegram
from app.blocking_db import _ingress
from tests.access_fixtures import enroll
from tests.test_wp_03_4_postgres_delivery import deliver
from tests.test_wp_03_5_postgres_completion import source_plan
from tests.test_wp_04_2_privacy_postgres import engine, pytestmark, subject  # noqa: F401


async def wait_for_state(check):
    async with asyncio.timeout(8):
        while not check():
            await asyncio.sleep(0.025)


async def test_roster_apply_cannot_fill_telegram_workers_in_other_deployment(engine, monkeypatch):
    factory = sessionmaker(engine, expire_on_commit=False)
    for module in (access_http, telegram):
        monkeypatch.setattr(module, "SessionLocal", factory)
    monkeypatch.setattr(access_http.settings, "ACCESS_OPERATOR_SECRET", "serial-probe-operator")
    monkeypatch.setattr(access_http.access_proof, "rate_limit", lambda *_a, **_kw: None)
    headers = {"Authorization": "Bearer serial-probe-operator"}

    with factory.begin() as db:
        first, deployment, _ = subject(db)
        same_group = [first.tg_id]
        for _ in range(7):
            user = models.User(tg_id=uuid4().int % 10**12, is_active=True)
            db.add(user)
            db.flush()
            enroll(db, user, target=deployment)
            same_group.append(user.tg_id)
        other, other_deployment, _ = subject(db)
        assert other_deployment.id != deployment.id

    replies = defaultdict(list)

    async def transport(_bot, method, **_kwargs):
        assert isinstance(method, SendMessage)
        replies[method.chat_id].append(method.text)
        return True

    monkeypatch.setattr(telegram.bot.session, "make_request", transport)

    async def open_privacy(tg_id):
        update = Update.model_validate({
            "update_id": tg_id, "message": {
                "message_id": 1, "date": 1, "text": "/privacy",
                "from": {"id": tg_id, "first_name": "Test", "is_bot": False},
                "chat": {"id": tg_id, "type": "private"},
            },
        })
        await telegram.dp.feed_update(telegram.bot, update)

    held, release = Event(), Event()
    lock_deployment = access.lock_deployment

    def gated_deployment(*args, **kwargs):
        row = lock_deployment(*args, **kwargs)
        if current_thread().name.startswith("http-roster") and row.id == deployment.id:
            held.set()
            assert release.wait(12)
        return row

    async with AsyncClient(transport=ASGITransport(app=api.app), base_url="http://testserver") as client:
        preview = await client.post("/access/roster/preview", headers=headers, json={
            "deployment_id": deployment.id, "mode": "full_snapshot", "expected_total": 1,
            "source_ref": "serial-probe", "rows": [{"email": "probe@example.org", "action": "add"}],
        })
        assert preview.status_code == 200
        monkeypatch.setattr(access, "lock_deployment", gated_deployment)
        apply = asyncio.create_task(client.post(
            "/access/roster/apply", headers=headers,
            json={"deployment_id": deployment.id, **preview.json()},
        ))
        try:
            assert await asyncio.to_thread(held.wait, 4)
            # Affected transactions release their workers while the exclusive
            # roster lock is still held. An unrelated group can use the lane.
            for tg_id in same_group:
                await asyncio.wait_for(open_privacy(tg_id), 2)
                assert "Зараз багато запитів" in replies[tg_id][-1]
            assert _ingress.available() == 8
            await asyncio.wait_for(open_privacy(other.tg_id), 2)
            assert any("Версія:" in reply for reply in replies[other.tg_id])
            assert not apply.done()
        finally:
            release.set()
            response = await apply
        assert response.status_code == 200
        await open_privacy(first.tg_id)
        assert not any("Версія:" in reply for reply in replies[first.tg_id])


def plan_step(db, due, *, delivered=False):
    owner, plan = source_plan(db, work_days=["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"])
    step = plan.days[0].steps[0]
    now = datetime.now(timezone.utc)
    step.scheduled_for = now - timedelta(minutes=1) if delivered else due
    step.expires_at = now + timedelta(hours=1)
    db.flush()
    if delivered:
        deliver(db, step)
        step.expires_at = due
    return owner, step


async def test_expiry_keeps_running_while_one_telegram_edit_waits(engine, monkeypatch):
    factory = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(scheduler, "SessionLocal", factory)
    monkeypatch.setattr(scheduler, "_submit_continuation", lambda *_: None)
    monkeypatch.setattr(scheduler, "enable_completion_recovery", lambda: None)
    monkeypatch.setattr(scheduler, "_enable_delivery_recovery", lambda: None)
    monkeypatch.setattr(scheduler, "_expiry_after", 0)
    past = datetime.now(timezone.utc) - timedelta(seconds=1)
    with factory.begin() as db:
        first, first_step = plan_step(db, past, delivered=True)
        other, _ = plan_step(db, past, delivered=True)

    first_edit, release, other_edit = Event(), Event(), Event()

    class Bot:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def edit_message_text(self, chat_id, **_kwargs):
            if chat_id == first.tg_id:
                first_edit.set()
                assert await asyncio.to_thread(release.wait, 10)
            if chat_id == other.tg_id:
                other_edit.set()
            return True

    monkeypatch.setattr(exercise_status, "Bot", Bot)
    scan = asyncio.create_task(asyncio.to_thread(scheduler.expire_overdue_steps))
    try:
        assert await asyncio.to_thread(first_edit.wait, 5)
        assert await asyncio.to_thread(other_edit.wait, 2)
        with factory.begin() as db:
            _, later_step = plan_step(db, past, delivered=True)
        await asyncio.wait_for(asyncio.to_thread(scheduler.expire_overdue_steps), 2)
        with factory() as db:
            assert db.get(models.AIPlanStep, later_step.id).step_status == "expired"
            assert db.get(models.AIPlanStep, first_step.id).tg_message_id is not None
    finally:
        release.set()
        await scan
        await wait_for_state(lambda: not scheduler._projection_inflight)


@pytest.mark.parametrize("hold_plan", [False, True])
async def test_startup_restores_next_owner_while_first_is_locked(engine, monkeypatch, hold_plan):
    factory = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(scheduler, "SessionLocal", factory)
    monkeypatch.setattr(scheduler, "expire_overdue_steps", lambda: None)
    monkeypatch.setattr(scheduler, "_enable_delivery_recovery", lambda: None)
    monkeypatch.setattr(scheduler, "enable_completion_recovery", lambda: None)
    jobs = BackgroundScheduler(timezone="UTC")
    jobs.start(paused=True)
    monkeypatch.setattr(scheduler, "scheduler", jobs)
    try:
        now = datetime.now(timezone.utc)
        with factory.begin() as db:
            first, first_step = plan_step(db, now + timedelta(minutes=4))
            _, other_step = plan_step(db, now + timedelta(minutes=5))
            first_job = scheduler._generate_step_job_id(first_step)
            other_job = scheduler._generate_step_job_id(other_step)
            first_plan_id = first_step.day.plan_id

        with factory() as holder:
            holder.get(models.User, first.id, with_for_update=True)
            deferred = Event()
            if hold_plan:
                holder.get(models.AIPlan, first_plan_id, with_for_update=True)
                restore_batch = scheduler._restore_future_job_batch

                def observed_batch(after):
                    cursor, needs_rescan = restore_batch(after)
                    if needs_rescan:
                        deferred.set()
                    return cursor, needs_rescan

                monkeypatch.setattr(scheduler, "_restore_future_job_batch", observed_batch)
            restore = asyncio.create_task(scheduler.restore_scheduled_timers())
            try:
                await wait_for_state(lambda: jobs.get_job(other_job) is not None)
                assert jobs.get_job(first_job) is None
                if hold_plan:
                    assert await asyncio.to_thread(deferred.wait, 3)
                else:
                    await asyncio.wait_for(asyncio.shield(restore), 3)
                    with factory() as db:
                        assert db.get(models.AIPlan, first_plan_id).schedule_reconcile_at is not None
            finally:
                holder.rollback()
                await asyncio.wait_for(restore, 8)
        if not hold_plan:
            await asyncio.to_thread(scheduler.dispatch_plan_schedule_recovery)
        await wait_for_state(lambda: jobs.get_job(first_job) is not None)
        assert jobs.get_job(first_job).next_run_time == first_step.scheduled_for
        await wait_for_state(lambda: not scheduler._schedule_repair_inflight)
    finally:
        jobs.shutdown()

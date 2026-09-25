"""Opt-in disposable PostgreSQL outcome rehearsal for WP-02.3.

Run with WP02_3_POSTGRES_REHEARSAL=1 against compose.test.yml after Alembic
upgrade. Every test uses one outer transaction and rolls back its seed rows.
"""

from __future__ import annotations

import os
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, func
from sqlalchemy.orm import Session

from app import lifecycle, lifecycle_reconciliation, scheduler as scheduler_module
from app.db import (
    AIPlan,
    AIPlanDay,
    AIPlanStep,
    ContentLibrary,
    OnboardingProgress,
    PlanLifecycleOperation,
    User,
    UserEvent,
    UserProfile,
)
from app.plan_drafts.plan_builder_v5 import get_default_builder
from app.telemetry import log_user_event


pytestmark = pytest.mark.skipif(
    os.environ.get("WP02_3_POSTGRES_REHEARSAL") != "1",
    reason="requires the explicitly selected disposable PostgreSQL rehearsal",
)

_TEST_URL = (
    "postgresql+psycopg2://love_yourself_test:love_yourself_test@"
    "127.0.0.1:55432/love_yourself_test"
)


@pytest.fixture
def pg_session():
    engine = create_engine(_TEST_URL)
    connection = engine.connect()
    transaction = connection.begin()
    db = Session(bind=connection)
    try:
        yield db
    finally:
        db.close()
        transaction.rollback()
        connection.close()
        engine.dispose()


def _seed_current_plan(db: Session, *, medium: bool = False):
    start = datetime(2026, 9, 1, 8, tzinfo=timezone.utc)
    user = User(tg_id=990230000 + (1 if medium else 0), is_active=True, timezone="UTC")
    db.add(user)
    db.flush()
    profile = UserProfile(
        user_id=user.id,
        daily_time_slots={"DAY": "14:00", "EVENING": "20:30"},
        evening_slot_collected=medium,
        active_days=["MON", "TUE", "WED", "THU", "FRI"],
    )
    db.add(profile)
    plan = AIPlan(
        user_id=user.id,
        title="WP-02.3 disposable source",
        status="active",
        cycle_number=1,
        activated_at=start,
        start_date=start,
        version=1,
        duration="MEDIUM" if medium else "SHORT",
        total_days=14 if medium else 7,
        preferred_time_slots=["DAY", "EVENING"] if medium else ["DAY"],
    )
    db.add(plan)
    db.flush()
    steps = []
    for day_number in (1, 2, 3):
        day = AIPlanDay(plan_id=plan.id, day_number=day_number)
        db.add(day)
        db.flush()
        for order, slot in enumerate(("DAY", "EVENING") if medium else ("DAY",)):
            scheduled = start + timedelta(days=day_number, hours=6 if slot == "DAY" else 12)
            step = AIPlanStep(
                day_id=day.id,
                title=f"day {day_number} {slot}",
                order_in_day=order,
                time_slot=slot,
                scheduled_for=scheduled,
                expires_at=scheduled.replace(hour=23, minute=59, second=59),
                step_status="pending",
                version=1,
            )
            db.add(step)
            steps.append(step)
    db.flush()
    return user, profile, plan, steps


def _seed_disposable_builder_library(db: Session) -> None:
    """Satisfy legacy exercise FKs within this test's rolled-back transaction."""
    for exercise in get_default_builder().exercises:
        db.add(ContentLibrary(
            id=exercise.id,
            content_version=1,
            internal_name=exercise.title or exercise.id,
            category="test",
            difficulty=1,
            energy_cost="test",
            logic_tags={},
            content_payload={"title": exercise.title},
            is_active=exercise.is_active,
        ))
    db.flush()


def test_pause_resume_reanchors_remaining_rows_and_receipts(pg_session):
    db = pg_session
    user, _profile, plan, steps = _seed_current_plan(db)
    delivered_at = datetime(2026, 9, 2, 14, tzinfo=timezone.utc)
    steps[0].step_status = "delivered"
    delivered_expiry = steps[0].expires_at
    db.flush()

    paused = lifecycle.transition_current_plan(
        db, user_id=user.id, operation="pause", source_operation_id="wp023:pause"
    )
    db.flush()
    assert paused.status == "paused"
    assert paused.effects[0].kind == "reconcile_plan_schedule"
    assert lifecycle.complete_current_plan_if_ready(
        db, user_id=user.id, plan_id=plan.id, source_operation_id="wp023:complete"
    ) is None

    resumed = lifecycle.transition_current_plan(
        db,
        user_id=user.id,
        operation="resume",
        source_operation_id="wp023:resume",
        occurred_at=datetime(2026, 9, 4, 18, tzinfo=timezone.utc),
    )
    db.flush()
    assert resumed.status == "active"
    assert steps[0].scheduled_for == delivered_at
    assert steps[0].expires_at == delivered_expiry
    assert steps[1].scheduled_for == datetime(2026, 9, 7, 14, tzinfo=timezone.utc)
    assert steps[2].scheduled_for == datetime(2026, 9, 8, 14, tzinfo=timezone.utc)
    assert resumed.details["updated_step_ids"] == (steps[1].id, steps[2].id)
    assert db.query(PlanLifecycleOperation).filter(
        PlanLifecycleOperation.user_id == user.id,
        PlanLifecycleOperation.operation.in_(("pause", "resume")),
    ).count() == 2


def test_paused_switch_keeps_delivery_off_until_explicit_resume(pg_session, monkeypatch):
    db = pg_session
    user, _profile, source, source_steps = _seed_current_plan(db, medium=True)
    _seed_disposable_builder_library(db)
    lifecycle.transition_current_plan(
        db, user_id=user.id, operation="pause", source_operation_id="wp023:pause-before-switch"
    )

    switched = lifecycle.switch_plan_format(
        db, user_id=user.id, target_plan_type="SHORT",
        source_operation_id="wp023:paused-switch",
    )
    db.flush()
    replacement = db.get(AIPlan, switched.plan_id)
    resumed_at = max(datetime.now(timezone.utc), replacement.activated_at) + timedelta(minutes=1)
    assert source.status == "abandoned"
    assert all(step.step_status == "canceled" for step in source_steps)
    assert replacement.status == switched.status == "paused"
    assert switched.details["total_days"] == 7
    original_times = {
        step.id: (step.scheduled_for, step.expires_at)
        for day in replacement.days for step in day.steps
    }
    original_version = replacement.version

    jobs = {
        scheduler_module._generate_step_job_id(step): SimpleNamespace(next_run_time=step.scheduled_for)
        for step in source_steps
    }
    stale_job_id = scheduler_module._generate_step_job_id(source_steps[0])

    class _Jobs:
        failed_removals = 2

        def get_job(self, job_id):
            return jobs.get(job_id)

        def remove_job(self, job_id):
            if job_id == stale_job_id and self.failed_removals:
                self.failed_removals -= 1
                raise RuntimeError("temporary job-store failure")
            jobs.pop(job_id, None)

    monkeypatch.setattr(scheduler_module, "scheduler", _Jobs())
    monkeypatch.setattr(scheduler_module, "SessionLocal", lambda: nullcontext(db))
    monkeypatch.setattr(
        scheduler_module, "schedule_plan_step",
        lambda step, _user: jobs.__setitem__(
            scheduler_module._generate_step_job_id(step),
            SimpleNamespace(next_run_time=step.scheduled_for),
        ),
    )
    events = []
    monkeypatch.setattr(
        lifecycle_reconciliation, "_record_activation_event",
        lambda result: events.append(result.plan_id),
    )

    outcome = lifecycle_reconciliation.reconcile_scheduler_effects(switched)
    assert not outcome.external_effects_succeeded
    assert stale_job_id in jobs
    assert events == []
    with pytest.raises(lifecycle.LifecycleTransitionError, match="switch_schedule_pending"):
        lifecycle.transition_current_plan(
            db, user_id=user.id, operation="resume",
            source_operation_id="wp023:resume-switched",
            occurred_at=resumed_at,
        )
    assert replacement.status == "paused"
    assert replacement.version == original_version
    assert {
        step.id: (step.scheduled_for, step.expires_at)
        for day in replacement.days for step in day.steps
    } == original_times
    assert db.query(PlanLifecycleOperation).filter(
        PlanLifecycleOperation.user_id == user.id,
        PlanLifecycleOperation.source_operation_id == "wp023:resume-switched",
    ).count() == 0

    replay = lifecycle.recover_switch_plan_format(
        db, user_id=user.id, switch_source_operation_id="wp023:paused-switch"
    )
    assert replay.duplicate is True
    assert replay.status == "paused"
    assert replay.effects[-1].state is lifecycle.ExternalEffectState.PENDING
    assert lifecycle_reconciliation.reconcile_scheduler_effects(replay).external_effects_succeeded
    assert jobs == {}
    assert events == [replacement.id]

    # A successful scheduler pass without the durable ready marker is not enough.
    with pytest.raises(lifecycle.LifecycleTransitionError, match="switch_schedule_pending"):
        lifecycle.transition_current_plan(
            db, user_id=user.id, operation="resume",
            source_operation_id="wp023:resume-switched",
            occurred_at=resumed_at,
        )
    assert replacement.status == "paused"
    assert replacement.version == original_version
    assert db.query(PlanLifecycleOperation).filter(
        PlanLifecycleOperation.user_id == user.id,
        PlanLifecycleOperation.source_operation_id == "wp023:resume-switched",
    ).count() == 0

    lifecycle.record_switch_schedule_ready(
        db, user_id=user.id, plan_id=replacement.id,
        switch_source_operation_id="wp023:paused-switch",
    )
    resumed = lifecycle.transition_current_plan(
        db, user_id=user.id, operation="resume",
        source_operation_id="wp023:resume-switched",
        occurred_at=resumed_at,
    )
    db.flush()
    assert resumed.status == replacement.status == "active"
    expected_ids = {
        scheduler_module._generate_step_job_id(step)
        for day in replacement.days for step in day.steps
        if step.step_status == "pending"
    }
    assert len(resumed.details["updated_step_ids"]) == len(expected_ids) == 7
    assert lifecycle_reconciliation.reconcile_scheduler_effects(resumed).external_effects_succeeded
    assert set(jobs) == expected_ids
    assert lifecycle_reconciliation.reconcile_scheduler_effects(resumed).external_effects_succeeded
    assert set(jobs) == expected_ids
    paused_again = lifecycle.transition_current_plan(
        db, user_id=user.id, operation="pause",
        source_operation_id="wp023:pause-after-resume",
    )
    assert paused_again.status == "paused"
    switched_again = lifecycle.switch_plan_format(
        db, user_id=user.id, target_plan_type="MEDIUM",
        source_operation_id="wp023:switch-after-resume",
        occurred_at=resumed_at + timedelta(minutes=1),
    )
    assert switched_again.status == "paused"
    assert switched_again.plan_id != replacement.id


def test_switch_collects_evening_then_replaces_one_current_plan_atomically(pg_session):
    db = pg_session
    user, profile, source, source_steps = _seed_current_plan(db)
    _seed_disposable_builder_library(db)
    with pytest.raises(
        lifecycle.LifecycleTransitionError,
        match="switch_recovery_receipt_missing",
    ):
        lifecycle.recover_plan_format_switch(
            db,
            user_id=user.id,
            target_plan_type="MEDIUM",
            expected_evening_time="20:30",
            source_operation_id="wp023:unrelated-switch",
        )
    assert source.status == "active"

    pending = lifecycle.switch_plan_format(
        db,
        user_id=user.id,
        target_plan_type="MEDIUM",
        source_operation_id="wp023:switch",
    )
    assert pending.code == "needs_evening_time"
    assert source.status == "active"
    assert all(step.step_status == "pending" for step in source_steps)

    lost_key_retry = lifecycle.recover_switch_plan_format(
        db, user_id=user.id, switch_source_operation_id="wp023:switch",
    )
    assert lost_key_retry.code == "needs_evening_time"
    assert lost_key_retry.details["switch_source_operation_id"] == "wp023:switch"

    collected = lifecycle.record_evening_time_preference(
        db,
        user_id=user.id,
        hhmm="20:30",
        context="switch",
        pending_source_operation_id="wp023:switch",
        source_operation_id="wp023:evening",
    )
    assert collected.applied is True
    assert profile.evening_slot_collected is True

    switched = lifecycle.switch_plan_format(
        db,
        user_id=user.id,
        target_plan_type="MEDIUM",
        source_operation_id="wp023:switch",
    )
    db.flush()
    assert switched.plan_id != source.id
    assert switched.plan_type == "MEDIUM"
    assert source.status == "abandoned"
    assert all(step.step_status == "canceled" for step in source_steps)
    assert db.query(AIPlan).filter(
        AIPlan.user_id == user.id,
        AIPlan.status.in_(("active", "paused")),
    ).count() == 1

    for _ in range(2):
        log_user_event(
            db, user_id=user.id, event_type="plan_activated",
            event_source="plan_finalization",
            source_operation_id=f"plan-activation:{switched.plan_id}",
            plan_id=switched.plan_id,
            context={"total_days": int(switched.details["total_days"])},
        )
    assert db.query(UserEvent).filter(
        UserEvent.user_id == user.id,
        UserEvent.plan_id == switched.plan_id,
        UserEvent.event_name == "plan_activated",
    ).count() == 1
    assert db.query(func.count(AIPlanStep.id)).join(AIPlanDay).filter(
        AIPlanDay.plan_id == switched.plan_id,
    ).scalar() == 28
    assert {effect.kind for effect in switched.effects} == {
        "cancel_step_jobs", "remove_step_keyboards", "reconcile_plan_schedule"
    }
    with pytest.raises(
        lifecycle.LifecycleTransitionError,
        match="switch_recovery_evening_time_mismatch",
    ):
        lifecycle.recover_plan_format_switch(
            db,
            user_id=user.id,
            target_plan_type="MEDIUM",
            expected_evening_time="21:00",
            source_operation_id="wp023:switch",
        )
    assert db.query(AIPlan).filter(
        AIPlan.user_id == user.id,
        AIPlan.status.in_(("active", "paused")),
    ).count() == 1

    replay = lifecycle.recover_plan_format_switch(
        db,
        user_id=user.id,
        target_plan_type="MEDIUM",
        expected_evening_time="20:30",
        source_operation_id="wp023:switch",
    )
    assert replay.duplicate is True
    assert replay.plan_id == switched.plan_id
    assert replay.code == "replayed"


def test_switch_builder_failure_rolls_back_source_mutation(pg_session, monkeypatch):
    db = pg_session
    user, _profile, source, source_steps = _seed_current_plan(db, medium=True)
    source_id = source.id
    step_ids = [step.id for step in source_steps]
    savepoint = db.begin_nested()
    monkeypatch.setattr(
        "app.plan_drafts.service.create_plan_for_lifecycle",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("builder_failed")),
    )

    with pytest.raises(RuntimeError, match="builder_failed"):
        lifecycle.switch_plan_format(
            db,
            user_id=user.id,
            target_plan_type="SHORT",
            source_operation_id="wp023:failed-switch",
        )
    savepoint.rollback()
    db.expire_all()

    assert db.get(AIPlan, source_id).status == "active"
    assert all(db.get(AIPlanStep, step_id).step_status == "pending" for step_id in step_ids)


def test_switch_receipt_retry_and_pause_wait_for_schedule_proof(pg_session):
    db = pg_session
    user, _profile, source, _steps = _seed_current_plan(db, medium=True)
    _seed_disposable_builder_library(db)
    switched = lifecycle.switch_plan_format(
        db, user_id=user.id, target_plan_type="SHORT",
        source_operation_id="wp023:switch-retry",
    )
    with pytest.raises(lifecycle.LifecycleTransitionError, match="switch_schedule_pending"):
        lifecycle.transition_current_plan(
            db, user_id=user.id, operation="pause", source_operation_id="wp023:early-pause"
        )
    with pytest.raises(lifecycle.LifecycleTransitionError, match="switch_schedule_pending"):
        lifecycle.switch_plan_format(
            db, user_id=user.id, target_plan_type="MEDIUM",
            source_operation_id="wp023:second-switch",
        )
    replay = lifecycle.recover_switch_plan_format(
        db, user_id=user.id, switch_source_operation_id="wp023:switch-retry"
    )
    assert replay.plan_id == switched.plan_id
    assert replay.duplicate is True
    assert source.status == "abandoned"
    assert db.query(AIPlan).filter(AIPlan.user_id == user.id).count() == 2

    lifecycle.record_switch_schedule_ready(
        db, user_id=user.id, plan_id=switched.plan_id,
        switch_source_operation_id="wp023:switch-retry",
    )
    lifecycle.record_switch_schedule_ready(
        db, user_id=user.id, plan_id=switched.plan_id,
        switch_source_operation_id="wp023:switch-retry",
    )
    paused = lifecycle.transition_current_plan(
        db, user_id=user.id, operation="pause", source_operation_id="wp023:pause-after-proof"
    )
    assert paused.status == "paused"
    assert db.query(PlanLifecycleOperation).filter(
        PlanLifecycleOperation.user_id == user.id,
        PlanLifecycleOperation.operation == "switch_schedule_ready",
    ).count() == 1
    assert db.query(AIPlan).filter(
        AIPlan.user_id == user.id, AIPlan.status.in_(("active", "paused")),
    ).count() == 1


def test_followup_evening_fresh_retry_after_builder_rollback(pg_session, monkeypatch):
    db = pg_session
    user, profile, source, _steps = _seed_current_plan(db)
    db.add(OnboardingProgress(
        user_id=user.id, stage="COMPLETED",
        completed_at=datetime(2026, 9, 1, 8, tzinfo=timezone.utc),
    ))
    source.status = "abandoned"
    source.abandoned_at = datetime(2026, 9, 2, 8, tzinfo=timezone.utc)
    db.flush()
    _seed_disposable_builder_library(db)
    lifecycle.prepare_followup_evening_collection(
        db, user_id=user.id, source_operation_id="wp023:followup-intent"
    )
    first = lifecycle.record_evening_time_preference(
        db, user_id=user.id, hhmm="20:30", context="followup",
        pending_source_operation_id="wp023:followup-intent",
        source_operation_id="wp023:evening-first",
    )
    assert first.applied is True
    assert profile.evening_slot_collected is True

    savepoint = db.begin_nested()
    with monkeypatch.context() as patch:
        patch.setattr(
            "app.plan_drafts.service.create_plan_for_lifecycle",
            lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("builder_failed")),
        )
        with pytest.raises(RuntimeError, match="builder_failed"):
            lifecycle.activate_plan(
                db, user_id=user.id, plan_type="MEDIUM", day_time="14:00",
                evening_time="20:30", source_operation_id="wp023:followup-intent",
                require_plan_history=True, required_previous_status="abandoned",
            )
    savepoint.rollback()
    db.expire_all()
    profile = db.get(UserProfile, profile.id)
    assert profile.evening_slot_collected is True

    with pytest.raises(lifecycle.LifecycleTransitionError, match="followup_evening_time_mismatch"):
        lifecycle.record_evening_time_preference(
            db, user_id=user.id, hhmm="21:00", context="followup",
            pending_source_operation_id="wp023:followup-intent",
            source_operation_id="wp023:evening-drift",
        )

    replay = lifecycle.record_evening_time_preference(
        db, user_id=user.id, hhmm="20:30", context="followup",
        pending_source_operation_id="wp023:followup-intent",
        source_operation_id="wp023:evening-fresh",
    )
    assert replay.duplicate is True
    assert replay.code == "replayed"
    assert profile.daily_time_slots["EVENING"] == "20:30"

    activated = lifecycle.activate_plan(
        db, user_id=user.id, plan_type="MEDIUM", day_time="14:00",
        evening_time="20:30",
        source_operation_id="coach:fresh-after-builder-failure",
        require_plan_history=True, required_previous_status="abandoned",
    )
    assert activated.operation == "activate"
    assert activated.plan_type == "MEDIUM"
    assert activated.plan_id != source.id
    assert db.query(AIPlan).filter(
        AIPlan.user_id == user.id,
        AIPlan.status.in_(("active", "paused")),
    ).count() == 1

    recovered = lifecycle.recover_current_plan_activation(
        db, user_id=user.id, plan_type="MEDIUM",
        required_previous_status="abandoned",
    )
    assert recovered.plan_id == activated.plan_id
    assert recovered.duplicate is True
    assert db.query(AIPlan).filter(AIPlan.user_id == user.id).count() == 2


def test_stale_followup_evening_intent_cannot_collect_with_fresh_call(pg_session):
    db = pg_session
    user, profile, source, _steps = _seed_current_plan(db)
    source.status = "abandoned"
    source.abandoned_at = datetime(2026, 9, 2, 8, tzinfo=timezone.utc)
    db.flush()
    lifecycle.prepare_followup_evening_collection(
        db, user_id=user.id, source_operation_id="wp023:old-intent"
    )
    lifecycle.prepare_followup_evening_collection(
        db, user_id=user.id, source_operation_id="wp023:new-intent"
    )

    with pytest.raises(
        lifecycle.LifecycleTransitionError,
        match="evening_collection_context_superseded",
    ):
        lifecycle.record_evening_time_preference(
            db, user_id=user.id, hhmm="20:30", context="followup",
            pending_source_operation_id="wp023:old-intent",
            source_operation_id="coach:fresh-old-intent",
        )
    assert profile.evening_slot_collected is False
    assert db.query(PlanLifecycleOperation).filter(
        PlanLifecycleOperation.source_operation_id == "coach:fresh-old-intent"
    ).count() == 0


def test_old_switch_retry_after_later_cancellation_is_superseded(pg_session):
    db = pg_session
    user, _profile, _source, _steps = _seed_current_plan(db, medium=True)
    _seed_disposable_builder_library(db)
    switched = lifecycle.switch_plan_format(
        db, user_id=user.id, target_plan_type="SHORT",
        source_operation_id="wp023:old-switch",
    )
    lifecycle.record_switch_schedule_ready(
        db, user_id=user.id, plan_id=switched.plan_id,
        switch_source_operation_id="wp023:old-switch",
    )
    lifecycle.abandon_current_plan(
        db,
        user_id=user.id,
        source_operation_id="wp023:later-cancel",
        occurred_at=db.get(AIPlan, switched.plan_id).activated_at + timedelta(minutes=1),
    )

    with pytest.raises(
        lifecycle.LifecycleTransitionError, match="recoverable_switch_superseded"
    ):
        lifecycle.recover_switch_plan_format(
            db, user_id=user.id, switch_source_operation_id="wp023:old-switch"
        )
    assert db.query(AIPlan).filter(
        AIPlan.user_id == user.id, AIPlan.status.in_(("active", "paused")),
    ).count() == 0


def test_exact_pause_resume_retry_rejects_later_cycle(pg_session):
    db = pg_session
    user, _profile, plan, _steps = _seed_current_plan(db)
    lifecycle.transition_current_plan(
        db, user_id=user.id, operation="pause", source_operation_id="wp023:pause-1"
    )
    replay = lifecycle.recover_plan_action(
        db, user_id=user.id, action="pause",
        original_source_operation_id="wp023:pause-1",
    )
    assert replay.duplicate is True
    assert replay.plan_id == plan.id

    lifecycle.transition_current_plan(
        db, user_id=user.id, operation="resume", source_operation_id="wp023:resume-1",
        occurred_at=datetime(2026, 9, 4, 18, tzinfo=timezone.utc),
    )
    resumed = lifecycle.recover_plan_action(
        db, user_id=user.id, action="resume",
        original_source_operation_id="wp023:resume-1",
    )
    assert resumed.duplicate is True
    with pytest.raises(lifecycle.LifecycleTransitionError, match="retry_action_superseded"):
        lifecycle.recover_plan_action(
            db, user_id=user.id, action="pause",
            original_source_operation_id="wp023:pause-1",
        )

    lifecycle.transition_current_plan(
        db, user_id=user.id, operation="pause", source_operation_id="wp023:pause-2"
    )
    with pytest.raises(lifecycle.LifecycleTransitionError, match="retry_action_superseded"):
        lifecycle.recover_plan_action(
            db, user_id=user.id, action="pause",
            original_source_operation_id="wp023:pause-1",
        )
    with pytest.raises(lifecycle.LifecycleTransitionError, match="retry_action_superseded"):
        lifecycle.recover_plan_action(
            db, user_id=user.id, action="resume",
            original_source_operation_id="wp023:resume-1",
        )
    assert plan.status == "paused"


def test_exact_cancel_retry_cleans_old_steps_without_touching_new_plan(pg_session):
    db = pg_session
    user, _profile, source, _steps = _seed_current_plan(db)
    db.add(OnboardingProgress(
        user_id=user.id, stage="COMPLETED",
        completed_at=datetime(2026, 9, 1, 8, tzinfo=timezone.utc),
    ))
    db.flush()
    _seed_disposable_builder_library(db)
    lifecycle.abandon_current_plan(
        db, user_id=user.id, source_operation_id="wp023:cancel-1"
    )
    replay = lifecycle.recover_plan_action(
        db, user_id=user.id, action="cancel",
        original_source_operation_id="wp023:cancel-1",
    )
    assert replay.duplicate is True
    assert replay.plan_id == source.id
    with pytest.raises(lifecycle.LifecycleTransitionError, match="retry_receipt_action_mismatch"):
        lifecycle.recover_plan_action(
            db, user_id=user.id, action="resume",
            original_source_operation_id="wp023:cancel-1",
        )

    activated = lifecycle.activate_plan(
        db, user_id=user.id, plan_type="SHORT", day_time="14:00",
        evening_time=None, source_operation_id="wp023:followup-1",
        require_plan_history=True, required_previous_status="abandoned",
    )
    assert activated.plan_id != source.id
    old_cleanup = lifecycle.recover_plan_action(
        db, user_id=user.id, action="cancel",
        original_source_operation_id="wp023:cancel-1",
    )
    assert old_cleanup.code == "historical_cleanup"
    assert old_cleanup.plan_id == source.id
    assert db.get(AIPlan, activated.plan_id).status == "active"


def test_exact_followup_retry_records_event_while_paused_without_scheduling(pg_session):
    db = pg_session
    user, _profile, source, _steps = _seed_current_plan(db)
    db.add(OnboardingProgress(
        user_id=user.id, stage="COMPLETED",
        completed_at=datetime(2026, 9, 1, 8, tzinfo=timezone.utc),
    ))
    db.flush()
    _seed_disposable_builder_library(db)
    lifecycle.abandon_current_plan(
        db, user_id=user.id, source_operation_id="wp023:cancel-before-followup"
    )
    followup = lifecycle.activate_plan(
        db, user_id=user.id, plan_type="SHORT", day_time="14:00",
        evening_time=None, source_operation_id="wp023:followup-2",
        require_plan_history=True, required_previous_status="abandoned",
    )
    replay = lifecycle.recover_plan_action(
        db, user_id=user.id, action="followup",
        original_source_operation_id="wp023:followup-2",
    )
    assert replay.duplicate is True
    assert replay.plan_id == followup.plan_id

    lifecycle.transition_current_plan(
        db, user_id=user.id, operation="pause", source_operation_id="wp023:pause-followup"
    )
    paused_replay = lifecycle.recover_plan_action(
        db, user_id=user.id, action="followup",
        original_source_operation_id="wp023:followup-2",
    )
    assert paused_replay.status == "paused"
    assert paused_replay.effects[0].state is lifecycle.ExternalEffectState.NOT_REQUIRED
    lifecycle.abandon_current_plan(
        db, user_id=user.id, source_operation_id="wp023:cancel-followup",
        occurred_at=db.get(AIPlan, followup.plan_id).activated_at + timedelta(minutes=1),
    )
    with pytest.raises(lifecycle.LifecycleTransitionError, match="retry_action_superseded"):
        lifecycle.recover_plan_action(
            db, user_id=user.id, action="followup",
            original_source_operation_id="wp023:followup-2",
        )
    assert source.status == "abandoned"


def test_post_effect_proof_sees_newer_control_and_time_receipts(pg_session):
    db = pg_session
    user, _profile, _plan, _steps = _seed_current_plan(db)
    paused = lifecycle.transition_current_plan(
        db, user_id=user.id, operation="pause", source_operation_id="proof:pause"
    )
    assert lifecycle.read_post_effect_truth(
        db, user_id=user.id, result=paused, source_operation_id="proof:pause"
    ).code == "current"
    resumed = lifecycle.transition_current_plan(
        db, user_id=user.id, operation="resume", source_operation_id="proof:resume"
    )
    assert lifecycle.read_post_effect_truth(
        db, user_id=user.id, result=paused, source_operation_id="proof:pause"
    ).code == "superseded"
    lifecycle.transition_current_plan(
        db, user_id=user.id, operation="pause", source_operation_id="proof:pause-again"
    )
    assert lifecycle.read_post_effect_truth(
        db, user_id=user.id, result=paused, source_operation_id="proof:pause"
    ).code == "superseded"
    lifecycle.transition_current_plan(
        db, user_id=user.id, operation="resume", source_operation_id="proof:resume-again"
    )
    assert lifecycle.read_post_effect_truth(
        db, user_id=user.id, result=resumed, source_operation_id="proof:resume"
    ).code == "superseded"

    user.profile.daily_time_slots = {**user.profile.daily_time_slots, "DAY": "15:30"}
    lifecycle.record_lifecycle_operation(
        db, user_id=user.id, plan_id=_plan.id,
        source_operation_id="proof:time-a", operation="change_day_time",
        result_status="15:30",
    )
    db.flush()
    first_time = lifecycle.LifecycleResult(
        user_id=user.id, plan_id=_plan.id, status="15:30",
        operation="change_day_time",
    )
    assert lifecycle.read_post_effect_truth(
        db, user_id=user.id, result=first_time, source_operation_id="proof:time-a"
    ).code == "current"
    user.profile.daily_time_slots = {**user.profile.daily_time_slots, "DAY": "16:45"}
    lifecycle.record_lifecycle_operation(
        db, user_id=user.id, plan_id=_plan.id,
        source_operation_id="proof:time-b", operation="change_day_time",
        result_status="16:45",
    )
    db.flush()
    truth = lifecycle.read_post_effect_truth(
        db, user_id=user.id, result=first_time, source_operation_id="proof:time-a"
    )
    assert truth.code == "superseded"
    assert truth.authoritative_value == "16:45"


def test_post_effect_proof_describes_old_cancel_and_changed_followup(pg_session):
    db = pg_session
    user, _profile, source, _steps = _seed_current_plan(db)
    db.add(OnboardingProgress(
        user_id=user.id, stage="COMPLETED",
        completed_at=datetime(2026, 9, 1, 8, tzinfo=timezone.utc),
    ))
    db.flush()
    _seed_disposable_builder_library(db)
    canceled, _ = lifecycle.abandon_current_plan(
        db, user_id=user.id, source_operation_id="proof:cancel"
    )
    db.flush()
    assert lifecycle.read_post_effect_truth(
        db, user_id=user.id, result=canceled, source_operation_id="proof:cancel"
    ).historical_cleanup is False
    followup = lifecycle.activate_plan(
        db, user_id=user.id, plan_type="SHORT", day_time="14:00",
        evening_time=None, source_operation_id="proof:followup",
        require_plan_history=True, required_previous_status="abandoned",
    )
    assert lifecycle.read_post_effect_truth(
        db, user_id=user.id, result=canceled, source_operation_id="proof:cancel"
    ).historical_cleanup is True
    assert lifecycle.read_post_effect_truth(
        db, user_id=user.id, result=followup, source_operation_id="proof:followup"
    ).code == "current"
    lifecycle.transition_current_plan(
        db, user_id=user.id, operation="pause", source_operation_id="proof:later-pause"
    )
    assert lifecycle.read_post_effect_truth(
        db, user_id=user.id, result=followup, source_operation_id="proof:followup"
    ).code == "superseded"
    assert db.get(AIPlan, followup.plan_id).status == "paused"
    assert source.status == "abandoned"

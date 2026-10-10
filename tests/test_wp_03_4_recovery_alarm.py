"""The fault alarm survives a DB outage without running in normal traffic."""
from datetime import datetime, timezone
from threading import Event

import pytest
from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.schedulers.background import BackgroundScheduler

from app import scheduler


def test_real_alarm_survives_failed_run_and_stops_after_repair(monkeypatch):
    alarm = BackgroundScheduler(jobstores={'delivery_recovery': MemoryJobStore()}, timezone='UTC')
    monkeypatch.setattr(scheduler, 'scheduler', alarm)
    failed = Event()
    def outage(*args):
        raise RuntimeError('temporary DB outage')
    def recover():
        failed.set(); outage()
    monkeypatch.setattr(scheduler, '_recover_scheduled_deliveries', recover)
    monkeypatch.setattr(scheduler, '_delivery_recovery_time', outage)
    alarm.start()
    try:
        scheduler._enable_delivery_recovery()
        alarm.modify_job('scheduled_delivery_recovery', jobstore='delivery_recovery', next_run_time=datetime.now(timezone.utc))
        assert failed.wait(3)
        job = alarm.get_job('scheduled_delivery_recovery', jobstore='delivery_recovery')
        assert job is not None and job.next_run_time > datetime.now(timezone.utc)
        monkeypatch.setattr(scheduler, '_delivery_recovery_time', lambda now: None)
        scheduler._enable_delivery_recovery()
        assert alarm.get_job('scheduled_delivery_recovery', jobstore='delivery_recovery') is None
    finally:
        alarm.shutdown(wait=True)


def test_recovery_failure_before_claim_retains_an_alarm(monkeypatch):
    alarm = BackgroundScheduler(jobstores={'delivery_recovery': MemoryJobStore()}, timezone='UTC')
    monkeypatch.setattr(scheduler, 'scheduler', alarm)
    def outage(*args): raise RuntimeError('DB unavailable')
    monkeypatch.setattr(scheduler, '_recover_scheduled_deliveries', outage)
    monkeypatch.setattr(scheduler, '_delivery_recovery_time', outage)
    alarm.start(paused=True)
    try:
        with pytest.raises(RuntimeError, match='DB unavailable'):
            scheduler.reconcile_scheduled_deliveries()
        assert alarm.get_job('scheduled_delivery_recovery', jobstore='delivery_recovery') is not None
    finally:
        alarm.shutdown(wait=True)

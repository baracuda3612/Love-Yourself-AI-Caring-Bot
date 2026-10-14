"""Target completion entrances; durable/concurrency evidence lives in PG rehearsal."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from app import orchestrator, scheduler
from app.plan_completion import workflow


def test_no_two_hour_trigger_or_process_local_send_authority():
    assert not hasattr(scheduler, '_maybe_schedule_plan_completion')
    assert not hasattr(orchestrator, '_completion_known_sends')
    assert not hasattr(orchestrator, '_completion_delivery_locks')
    assert 'start_plan:' not in Path('app/orchestrator.py').read_text()


def test_done_skip_and_expiry_reach_explicit_completion():
    source=Path('app/telegram.py').read_text()
    assert 'request_plan_completion, transition.user_id, transition.plan_id' in source
    expiry=Path('app/scheduler.py').read_text().split('def expire_overdue_steps()',1)[1].split('def check_plan_completions()',1)[0]
    assert expiry.index('candidate_db.commit()') < expiry.index('request_plan_completion(')


def test_fixed_cron_only_enters_recovery(monkeypatch):
    calls=[]
    monkeypatch.setattr(scheduler,'reconcile_plan_completions',lambda:calls.append('backstop'))
    scheduler.check_plan_completions()
    assert calls==['backstop']


def test_completion_alarm_parks_and_shuts_down(monkeypatch):
    calls=[]
    job=SimpleNamespace()
    fake=SimpleNamespace(
        get_job=lambda *a,**k:job,
        modify_job=lambda *a,**k:calls.append(('modify',k)),
        remove_job=lambda *a,**k:calls.append(('remove',k)),
    )
    monkeypatch.setattr(scheduler,'scheduler',fake)
    future=datetime.now(timezone.utc)+timedelta(days=3)
    monkeypatch.setattr(scheduler,'_completion_recovery_time',lambda now:future)
    scheduler.enable_completion_recovery()
    assert calls[0][1]['next_run_time']==future
    monkeypatch.setattr(scheduler,'_completion_recovery_time',lambda now:None)
    scheduler.enable_completion_recovery()
    assert calls[-1][0]=='remove'


def test_database_outage_keeps_an_isolated_short_alarm(monkeypatch):
    def outage(now): raise OSError('database unavailable')
    calls=[]
    monkeypatch.setattr(scheduler,'_completion_recovery_time',outage)
    monkeypatch.setattr(scheduler,'scheduler',SimpleNamespace(get_job=lambda *a,**k:None,add_job=lambda *a,**k:calls.append(k)))
    before=datetime.now(timezone.utc)
    scheduler.enable_completion_recovery()
    assert calls[0]['jobstore']=='completion_recovery'
    assert before+timedelta(seconds=29)<calls[0]['next_run_time']<before+timedelta(seconds=32)


@pytest.mark.parametrize('outcome',['sent','uncertain','retryable','failed'])
async def test_transport_retains_telegram_outcome_without_implicit_resend(monkeypatch,outcome):
    from app import db, telegram
    from aiogram.exceptions import TelegramRetryAfter, TelegramForbiddenError
    from aiogram.methods import SendMessage
    claim=workflow.ReportClaim(11,22,'factual closing report')
    class FakeDB:
        def commit(self): pass
    @contextmanager
    def session(): yield FakeDB()
    monkeypatch.setattr(db,'SessionLocal',session)
    monkeypatch.setattr(workflow,'claim_report',lambda *a,**k:claim)
    finishes=[]
    monkeypatch.setattr(workflow,'finish_report',lambda *a,**k:finishes.append(k))
    monkeypatch.setattr(scheduler,'enable_completion_recovery',lambda:None)
    send=AsyncMock(return_value=SimpleNamespace(message_id=44,chat=SimpleNamespace(id=22)))
    if outcome=='uncertain': send.side_effect=TimeoutError('unknown')
    elif outcome=='retryable': send.side_effect=TelegramRetryAfter(method=SendMessage(chat_id=22,text='x'),message='slow',retry_after=60)
    elif outcome=='failed': send.side_effect=TelegramForbiddenError(method=SendMessage(chat_id=22,text='x'),message='blocked')
    monkeypatch.setattr(telegram,'bot',SimpleNamespace(send_message=send))
    result=await workflow.deliver_report(1,11)
    assert send.await_count==1 and finishes[0]['outcome']==outcome
    assert result.succeeded==(outcome=='sent')


async def test_receipt_failure_only_retries_receipt(monkeypatch):
    from app import db, telegram
    class FakeDB:
        def commit(self): pass
    @contextmanager
    def session(): yield FakeDB()
    monkeypatch.setattr(db,'SessionLocal',session)
    monkeypatch.setattr(workflow,'claim_report',lambda *a,**k:workflow.ReportClaim(11,22,'x'))
    calls=[]
    def finish(*a,**k):
        calls.append(k)
        if len(calls)==1: raise OSError('receipt commit failed')
    monkeypatch.setattr(workflow,'finish_report',finish)
    monkeypatch.setattr(scheduler,'enable_completion_recovery',lambda:None)
    send=AsyncMock(return_value=SimpleNamespace(message_id=44,chat=SimpleNamespace(id=22)))
    monkeypatch.setattr(telegram,'bot',SimpleNamespace(send_message=send))
    result=await workflow.deliver_report(1,11)
    assert result.succeeded and send.await_count==1 and len(calls)==2

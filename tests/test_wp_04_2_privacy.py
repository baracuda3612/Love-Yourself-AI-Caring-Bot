"""Request-boundary privacy and failure contracts without external API calls."""
import json
import logging
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import ai, privacy, orchestrator, telegram, scheduler
from app.workers import coach_agent as coach
from app.ux import pulse_prompt
from app.logging.privacy_logging import install_privacy_logging


async def test_actual_coach_provider_request_omits_account_identity_and_bounds_history(monkeypatch):
    create = AsyncMock(return_value=SimpleNamespace(output_text='ok',usage=None,output=[]))
    monkeypatch.setattr(coach,'async_client',SimpleNamespace(responses=SimpleNamespace(create=create)))
    identifiers=['9876543210123','Direct First Name','@direct_username','employee@employer.test','Private Employer']
    payload={'user_id':222,'tg_id':identifiers[0],'first_name':identifiers[1],
        'username':identifiers[2],'email':identifiers[3],'employer':identifiers[4],
        'profile':{'email':identifiers[3]},'current_mode':'ACTIVE','message_text':'A normal Coach question',
        'short_term_history':[{'role':'user','content':'x'*6000} for _ in range(40)]}
    result=await coach.coach_agent(payload)
    assert result['reply_text']=='ok'
    request=create.call_args.kwargs
    assert request['store'] is False
    assert not {'user','metadata','conversation','previous_response_id','background'} & request.keys()
    encoded=json.dumps(request,ensure_ascii=False)
    assert all(identifier not in encoded for identifier in identifiers)
    history=[m for m in request['input'] if m['role'] in {'user','assistant'}]
    assert len(history)<=21 and all(len(m['content'])<=4000 for m in history)


async def test_every_responses_entry_explicitly_disables_storage(monkeypatch):
    create=AsyncMock(return_value=SimpleNamespace(output_text='ok',usage=None,output=[]))
    client=SimpleNamespace(responses=SimpleNamespace(create=create))
    monkeypatch.setattr(ai,'async_client',client)
    await ai._call_openai([{'role':'user','content':'bounded fixture'}])
    assert create.call_args.kwargs['store'] is False
    await pulse_prompt._complete(client,'static system','static quote')
    assert create.call_args.kwargs['store'] is False


async def test_unacknowledged_turn_never_reaches_cache_context_or_provider(monkeypatch):
    memory=SimpleNamespace(append_message=AsyncMock())
    monkeypatch.setattr(orchestrator,'session_memory',memory)
    monkeypatch.setattr(orchestrator,'_auto_complete_plan_if_needed_for_user_id',AsyncMock())
    monkeypatch.setattr(orchestrator,'SessionLocal',lambda:nullcontext(object()))
    def deny(*a,**k):raise privacy.NoticeRequired()
    monkeypatch.setattr(privacy,'pinned_notice',deny)
    context=AsyncMock();provider=AsyncMock()
    monkeypatch.setattr(orchestrator,'build_user_context',context)
    monkeypatch.setattr(orchestrator,'coach_agent',provider)
    reply=await orchestrator.handle_incoming_message(1,'private unsaved turn')
    assert '/privacy' in reply['reply_text']
    memory.append_message.assert_not_awaited();context.assert_not_awaited();provider.assert_not_awaited()


async def test_unacknowledged_telegram_text_is_not_persisted(monkeypatch):
    class DB:
        def add(self,row):pytest.fail('unacknowledged freeform persisted')
    monkeypatch.setattr(telegram,'SessionLocal',lambda:nullcontext(DB()))
    monkeypatch.setattr(telegram,'_ensure_user',lambda *a:(SimpleNamespace(id=1),False))
    def deny(*a,**k):raise privacy.NoticeRequired()
    monkeypatch.setattr(privacy,'pinned_notice',deny)
    show=AsyncMock();monkeypatch.setattr(telegram,'_show_privacy',show)
    await telegram.on_text(SimpleNamespace(text='unsaved freeform',from_user=SimpleNamespace(id=1)))
    show.assert_awaited_once()


def test_runtime_logging_drops_content_urls_provider_and_sql_exception_bodies():
    previous=logging.getLogRecordFactory()
    try:
        install_privacy_logging()
        factory=logging.getLogRecordFactory()
        error=ValueError('private SQL bound text')
        record=factory('app.private',logging.ERROR,__file__,10,
            'https://host/report/private-token %s',('private conversation',),
            (ValueError,error,None))
        assert 'private-token' not in record.getMessage()
        assert 'private conversation' not in record.getMessage()
        assert record.exc_info is None and record.exc_text is None
        assert 'ValueError' in record.getMessage()
        safe=factory('app.scheduler',logging.INFO,__file__,10,'privacy_retention_removed=%d',(25,),None)
        assert safe.getMessage()=='privacy_retention_removed=25'
    finally:
        logging.setLogRecordFactory(previous)


def test_privacy_job_erasure_removes_custom_legacy_and_canonical_jobs(monkeypatch):
    ids={'plan_7_day_1_step_4','custom-old-job','completion_7','completion_retry_7','completion_receipt_retry_7','plan_70_day_1_step_5','privacy_maintenance'}
    class DB:
        def query(self,*a):return self
        def join(self,*a):return self
        def filter(self,*a):return self
        def __iter__(self):return iter([(4,)])
    monkeypatch.setattr(scheduler,'SessionLocal',lambda:nullcontext(DB()))
    monkeypatch.setattr(scheduler,'scheduler',SimpleNamespace(
        get_jobs=lambda:[SimpleNamespace(id=job,args=(100,'',4) if job=='custom-old-job' else (),func_ref='app.scheduler:send_scheduled_message') for job in ids],remove_job=lambda job:ids.remove(job)))
    scheduler.cancel_privacy_jobs(1,[7])
    assert ids=={'plan_70_day_1_step_5','privacy_maintenance'}

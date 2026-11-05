"""Disposable UX adapter. Real bot handlers; captured Telegram transport only."""
import os, asyncio, json, html
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
os.environ['WP04_2_TEST_ADMIN_URL']='postgresql://love_yourself_test:love_yourself_test@127.0.0.1:55432/love_yourself_test'
import tests.conftest
from tests.test_wp_04_2_privacy_postgres import fresh_database, subject
from app import telegram, scheduler, db as models, access, privacy, user_menu, access_http, api
from app.content_library import load_content_library
from sqlalchemy.orm import sessionmaker
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.testclient import TestClient
import uvicorn

messages=[]; events=[]; reply=[]; counter=5
async def answer(text='', **kw):
    global counter, reply
    counter+=1
    markup=kw.get('reply_markup')
    data=markup.model_dump(exclude_none=True) if markup else {}
    if 'keyboard' in data: reply=data['keyboard']
    messages.append({'id':counter,'text':text,'markup':data})
    return SimpleNamespace(message_id=counter,chat=message.chat)
async def cb_answer(text='',**kw):
    if text: messages.append({'text':'[Callback] '+text,'markup':{}})
async def no_coach(*args, **kw): raise RuntimeError('Coach must not run in this UX scenario')

def state(): return {'messages':messages,'keyboard':reply}
app=FastAPI()
@app.get('/ux')
def ux(): return HTMLResponse(Path(__file__).with_name('ux_adapter.html').read_text())
@app.get('/ux/state')
def get_state(): return state()
@app.post('/ux/action')
async def action(request:Request):
    global counter
    data=await request.json(); counter+=1; message.message_id=counter
    events.append(data)
    if 'callback' in data:
        cb=SimpleNamespace(data=data['callback'],from_user=actor,message=message,id=str(uuid4()),answer=cb_answer)
        if cb.data.startswith('setup:'): await telegram.setup_action(cb)
        else: await telegram.menu_action(cb)
    else:
        message.text=data['text']
        messages.append({'text':html.escape(message.text),'markup':{},'user':True})
        if message.text=='/start': await telegram.cmd_start(message)
        elif message.text=='/stop': await telegram.stop_setup(message)
        elif message.text in user_menu.MENU_TEXTS: await telegram.menu_text(message)
        else: await telegram.on_text(message)
    Path(__file__).with_name('session.json').write_text(json.dumps({'events':events,**state()},ensure_ascii=False,indent=2))
    return state()

# Web presentation fixtures: no SMTP, provider, real proof or enrollment calls.
for name in ('SMTP_HOST','SMTP_FROM','SMTP_USERNAME','SMTP_PASSWORD','GOOGLE_CLIENT_ID','GOOGLE_CLIENT_SECRET'):
    setattr(access_http.settings,name,'fixture-only')
access_http.settings.MICROSOFT_CLIENT_ID=''; access_http.settings.MICROSOFT_CLIENT_SECRET=''
client=TestClient(api.app)
headers=dict(client.get('/enroll/123').headers)
secure={k:v for k,v in headers.items() if k in ('content-security-policy','referrer-policy','x-content-type-options','cache-control')}
@app.get('/enroll/privacy-policy')
def policy():
    response=client.get('/enroll/privacy-policy')
    return HTMLResponse(response.text,headers=secure)
@app.get('/enroll/{dep}')
def entry(dep:int):return HTMLResponse(access_http._page(dep,'b'*43),headers=secure)
@app.post('/enroll/{dep}/email')
def email(dep:int):return HTMLResponse(access_http._page(dep,'b'*43,note='Якщо адресу можна підтвердити, введи код із листа.',email_step='code'),headers=secure)
@app.post('/enroll/{dep}/verify')
def code(dep:int):return access_http._handoff(dep,'ly1_'+'a'*43)
@app.get('/expired')
def expired():return access_http.enrollment_error(123)

with fresh_database() as (engine,url):
    factory=sessionmaker(engine,expire_on_commit=False)
    with factory() as db:
        load_content_library(db)
        user, deployment, notice=subject(db)
        deployment.default_timezone='Europe/Kyiv'
        privacy.acknowledge_notice(db,user.id,notice.id,'fixture:ux')
        actor=SimpleNamespace(id=user.tg_id,username=None,first_name=None)
        db.commit()
    message=SimpleNamespace(from_user=actor,chat=SimpleNamespace(id=actor.id,type='private'),text='/start',message_id=5,answer=answer)
    telegram.SessionLocal=factory; models.SessionLocal=factory; scheduler.SessionLocal=factory
    access.outbound_claim=lambda _:True
    telegram.handle_incoming_message=no_coach
    jobs=BackgroundScheduler();jobs.start(paused=True);scheduler.scheduler=jobs
    asyncio.run(telegram.cmd_start(message))
    print('UX READY: http://127.0.0.1:8766/ux (disposable database, captured transport)',flush=True)
    try: uvicorn.run(app,host='127.0.0.1',port=8766,log_level='warning')
    finally: jobs.shutdown()

"""Enrollment and access-only operator surfaces. No employer behavior views."""
import hmac
import html
import secrets
from urllib.parse import parse_qsl

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app import access, access_proof
from app.config import settings
from app.db import SessionLocal

router = APIRouter()
NEUTRAL = 'Доступ зараз недоступний. Спробуй знову зі сторінки входу.'
COOKIE = 'ly_enrollment_browser'


def _operator(request):
    supplied = request.headers.get('authorization', '')
    expected = 'Bearer ' + settings.ACCESS_OPERATOR_SECRET
    if not settings.ACCESS_OPERATOR_SECRET or not hmac.compare_digest(supplied, expected):
        raise access.AccessDenied()
    access_proof.rate_limit('operator:' + (request.client.host if request.client else 'unknown'), limit=30)
    return access.digest(settings.ACCESS_OPERATOR_SECRET, 'operator-id')


def _browser(request):
    browser = request.cookies.get(COOKIE, '')
    if not 32 <= len(browser) <= 128:
        raise access.AccessDenied()
    return browser


async def _form(request, deployment_id):
    pairs = parse_qsl((await request.body()).decode(), keep_blank_values=True, strict_parsing=True)
    form = dict(pairs)
    browser = _browser(request)
    if (len(pairs) != len(form) or request.headers.get('origin') != settings.APP_BASE_URL
        or not hmac.compare_digest(form.get('csrf', ''), access.digest(f'{browser}:{deployment_id}', 'csrf'))):
        raise access.AccessDenied()
    return form, browser


def _page(deployment_id, browser, *, note=''):
    csrf = access.digest(f'{browser}:{deployment_id}', 'csrf')
    return f'''<!doctype html><html lang="uk"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
+<title>LY Workday — вхід</title><main><h1>LY Workday</h1>
+<p>Корпоративна адреса використовується для перевірки доступу, його адміністрування та безпеки. Роботодавець не отримує індивідуальну активність чи статус входу.</p>
+<p>{html.escape(note)}</p>
+<form method="post" action="/enroll/{deployment_id}/email"><input type="hidden" name="csrf" value="{csrf}"><label>Email <input type="email" name="email" required maxlength="254"></label><button>Отримати код</button></form>
+<form method="post" action="/enroll/{deployment_id}/verify"><input type="hidden" name="csrf" value="{csrf}"><label>Код із листа <input name="code" required maxlength="128" autocomplete="one-time-code"></label><button>Підтвердити</button></form>
+<form method="post" action="/enroll/{deployment_id}/oidc/google"><input type="hidden" name="csrf" value="{csrf}"><button>Google Workspace</button></form>
+<form method="post" action="/enroll/{deployment_id}/oidc/microsoft"><input type="hidden" name="csrf" value="{csrf}"><button>Microsoft 365</button></form></main></html>'''.replace('\n+', '\n')


def _handoff(raw):
    link = f'https://t.me/{settings.BOT_USERNAME}?start={raw}'
    return HTMLResponse(f'<!doctype html><html lang="uk"><meta charset="utf-8"><title>LY Workday</title><a rel="noreferrer" href="{html.escape(link, quote=True)}">Відкрити Telegram</a><p>Посилання одноразове, чинне 10 хвилин.</p></html>')


@router.get('/enroll/{deployment_id}', response_class=HTMLResponse)
def enrollment_page(deployment_id: int):
    # Identical page for unknown deployments; no roster or joining status leak.
    browser = secrets.token_urlsafe(32)
    response = HTMLResponse(_page(deployment_id, browser))
    response.set_cookie(COOKIE, browser, max_age=600, httponly=True, secure=not settings.IS_DEV, samesite='lax', path='/enroll')
    return response


@router.post('/enroll/{deployment_id}/email')
async def email_start(deployment_id: int, request: Request):
    form, browser = await _form(request, deployment_id)
    access_proof.rate_limit('email-ip:' + (request.client.host if request.client else 'unknown'), limit=5, window=600)
    email = access.normalize_email(form.get('email'))
    try:
        with SessionLocal() as db:
            raw = access_proof.begin_email(db, deployment_id=deployment_id, email=email, browser=browser)
            db.commit()
        # Both roster and non-roster addresses receive the same challenge.
        access_proof.send_email(email, raw)
    except Exception:
        # No secret/SMTP/provider exception logging or response detail.
        pass
    return HTMLResponse(_page(deployment_id, browser, note='Якщо адресу можна підтвердити, введи код із листа.'))


@router.post('/enroll/{deployment_id}/verify')
async def email_verify(deployment_id: int, request: Request):
    form, browser = await _form(request, deployment_id)
    access_proof.rate_limit('verify:' + browser, limit=10, window=600)
    with SessionLocal() as db:
        raw = access_proof.finish_email(db, deployment_id=deployment_id, raw=form.get('code', ''), browser=browser)
        db.commit()
    return _handoff(raw)


@router.post('/enroll/{deployment_id}/oidc/{provider}')
async def oidc_start(deployment_id: int, provider: str, request: Request):
    _, browser = await _form(request, deployment_id)
    access_proof.rate_limit('oidc-start:' + browser, limit=10, window=600)
    access_proof.rate_limit('oidc-ip:' + (request.client.host if request.client else 'unknown'), limit=10, window=600)
    with SessionLocal() as db:
        url = access_proof.begin_oidc(db, deployment_id=deployment_id, provider=provider, browser=browser)
        db.commit()
    return RedirectResponse(url, status_code=303)


@router.get('/enroll/{deployment_id}/oidc/{provider}/callback')
def oidc_callback(deployment_id: int, provider: str, request: Request):
    browser = _browser(request)
    access_proof.rate_limit('oidc-callback:' + browser, limit=10, window=600)
    params = request.query_params
    if len(params.multi_items()) != len(params) or not 1 <= len(params.get('code', '')) <= 4096:
        raise access.AccessDenied()
    with SessionLocal() as db:
        prepared = access_proof.prepare_oidc(db, deployment_id=deployment_id, provider=provider,
            state=params.get('state', ''), browser=browser)
        db.commit()
    claims = access_proof.verify_oidc(prepared, state=params.get('state', ''), code=params['code'])
    with SessionLocal() as db:
        result = access_proof.finish_oidc(db, prepared=prepared, claims=claims, browser=browser)
        db.commit()
    if 'handoff' in result:
        return _handoff(result['handoff'])
    if result.get('email_required'):
        return HTMLResponse(_page(deployment_id, browser,
            note='Microsoft підтвердив вхід. Вкажи адресу для доступу й підтвердь її кодом із листа.'))
    try:
        access_proof.send_email(result['email'], result['challenge'])
    except Exception:
        pass
    return HTMLResponse(_page(deployment_id, browser, note='Введи код підтвердження адреси з листа.'))


@router.post('/access/roster/preview')
async def roster_preview(request: Request):
    operator = _operator(request)
    body = await request.json()
    if set(body) != {'deployment_id', 'mode', 'rows', 'expected_total', 'source_ref'}:
        raise access.AccessDenied()
    with SessionLocal() as db:
        preview = access.preview_roster(db, **body, operator=operator)
        db.commit()
    return {'version_id': preview.version_id, 'counts': preview.counts, 'confirmation': preview.confirmation}


@router.post('/access/roster/apply')
async def roster_apply(request: Request):
    operator = _operator(request)
    body = await request.json()
    if set(body) != {'deployment_id', 'version_id', 'confirmation', 'counts'}:
        raise access.AccessDenied()
    with SessionLocal() as db:
        version = access.apply_roster(db, **body, operator=operator)
        db.commit()
    return {'accepted_count': version.eligible_count}


@router.post('/access/testnet/handoff')
async def founder_handoff(request: Request):
    operator = _operator(request)
    body = await request.json()
    if set(body) != {'deployment_id', 'label'}:
        raise access.AccessDenied()
    with SessionLocal() as db:
        entitlement = access.founder_grant(db, **body, operator=operator)
        raw = access.issue_handoff(db, entitlement.id)
        db.commit()
    return {'telegram_url': f'https://t.me/{settings.BOT_USERNAME}?start={raw}'}


@router.post('/access/revoke')
async def revoke(request: Request):
    operator = _operator(request)
    body = await request.json()
    if set(body) != {'entitlement_id'}:
        raise access.AccessDenied()
    with SessionLocal() as db:
        access.revoke_entitlement(db, **body, operator=operator)
        db.commit()
    return JSONResponse({'accepted': True})


@router.post('/access/enrollment/replace')
async def replace_enrollment(request: Request):
    operator = _operator(request)
    body = await request.json()
    if set(body) != {'user_id', 'expected_enrollment_id', 'handoff', 'replacement_mode'}:
        raise access.AccessDenied()
    with SessionLocal() as db:
        access.end_enrollment_for_replacement(db, **body, operator=operator)
        db.commit()
    return {'accepted': True}


@router.post('/access/deployment/controls')
async def deployment_controls(request: Request):
    operator = _operator(request)
    body = await request.json()
    if set(body) != {'deployment_id', 'enrollment_open', 'delivery_enabled'}:
        raise access.AccessDenied()
    with SessionLocal() as db:
        access.set_deployment_controls(db, **body, operator=operator)
        db.commit()
    return {'accepted': True}

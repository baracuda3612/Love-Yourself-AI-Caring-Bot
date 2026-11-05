"""Enrollment and access-only operator surfaces. No employer behavior views."""
import asyncio
import hmac
import html
import secrets
import re
from pathlib import Path
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
    try:
        pairs = parse_qsl((await request.body()).decode(), keep_blank_values=True, strict_parsing=True)
    except ValueError:
        raise access.AccessDenied() from None
    form = dict(pairs)
    browser = _browser(request)
    if (len(pairs) != len(form) or request.headers.get('origin') != settings.APP_BASE_URL
        or not hmac.compare_digest(form.get('csrf', ''), access.digest(f'{browser}:{deployment_id}', 'csrf'))):
        raise access.AccessDenied()
    return form, browser


ENROLLMENT_STYLE = """:root {
  color-scheme: light;
  --ink: #2e1a0e;
  --muted: #786b60;
  --paper: #faf7f2;
  --line: #e5ddd2;
  --accent: #7547a5;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--paper);
  color: var(--ink);
  font: 16px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}
.layout { min-height: 100svh; display: grid; grid-template-columns: minmax(300px, 42%) 1fr; }
.brand {
  display: flex; flex-direction: column; justify-content: space-between;
  padding: 48px clamp(28px, 5vw, 80px);
  background: #2e1a0e; color: #f8f4f1;
}
.brand-logo svg { display: block; width: 174px; height: auto; }
.brand-copy { margin: 64px 0; max-width: 380px; }
.brand-copy h2 { font: 400 clamp(40px, 4vw, 60px)/1.12 Georgia, serif; letter-spacing: -.025em; margin: 0 0 24px; }
.brand-copy p { color: #d2c4b6; max-width: 310px; margin: 0; font-size: 17px; }
.brand-footer { margin: 0; color: #c4a8e8; font-size: 13px; letter-spacing: .12em; text-transform: uppercase; }
main { display: flex; align-items: center; justify-content: center; padding: 48px 32px; }
.entry { width: 100%; max-width: 430px; }
.product-name { margin: 0 0 28px; font-weight: 650; font-size: 14px; letter-spacing: .04em; }
h1 { margin: 0 0 16px; font-size: clamp(27px, 3vw, 34px); font-weight: 600; line-height: 1.2; letter-spacing: -.035em; }
p { margin: 0 0 18px; }
.entry > p { color: var(--muted); }
form { display: grid; gap: 10px; margin: 28px 0 16px; }
label { font-size: 14px; font-weight: 600; }
input, button, .primary { width: 100%; font: inherit; min-height: 50px; border-radius: 10px; }
input { border: 1px solid #baada0; background: #fff; padding: 12px 14px; color: var(--ink); }
input:focus { outline: 3px solid #d9c6ed; outline-offset: 2px; border-color: var(--accent); }
button, .primary {
  border: 1px solid var(--accent); background: var(--accent); color: #fff;
  padding: 12px 18px; font-weight: 600; cursor: pointer; text-align: center; text-decoration: none;
}
button:hover, .primary:hover { background: #63358f; }
button:focus-visible, a:focus-visible { outline: 3px solid #b294d5; outline-offset: 3px; }
.provider { margin-top: 14px; }
.separator { display: flex; align-items: center; gap: 14px; color: var(--muted); font-size: 13px; margin: 24px 0 8px; }
.separator::before, .separator::after { content: ""; height: 1px; flex: 1; background: var(--line); }
.success-mark {
  display: grid; place-items: center; width: 46px; height: 46px; margin-bottom: 24px;
  border-radius: 50%; background: #e8f0e8; color: #42674a; font-size: 24px;
}
.policy-copy p { white-space: pre-line; overflow-wrap: anywhere; }
.help-link { display: inline-block; margin-top: 12px; font-size: 13px; }
.provider button { color: var(--ink); background: transparent; border-color: var(--line); }
.provider button:hover { background: #f0eae0; }
.provider button span { float: left; width: 22px; font-size: 18px; }
.primary { display: block; margin: 28px 0 20px; }
a { color: var(--accent); text-underline-offset: 3px; }
[role="status"] { padding: 14px 16px; border-left: 3px solid var(--accent); background: #f0e8f7; color: var(--ink); border-radius: 0 8px 8px 0; }
.privacy-note { padding-top: 24px; margin-top: 28px; border-top: 1px solid var(--line); font-size: 13px; color: var(--muted); }
@media (max-width: 760px) {
  .layout { display: block; }
  .brand { padding: 24px; gap: 24px; }
  .brand-logo svg { width: 138px; }
  .brand-copy { margin: 0; max-width: none; }
  .brand-copy h2 { font-size: 32px; margin: 0; }
  .brand-copy p, .brand-footer { display: none; }
  main { padding: 30px 24px 40px; align-items: start; }
  .product-name { margin-bottom: 20px; }
}
"""

_BRAND_LOGO = (Path(__file__).parent / "assets" / "logo_long.svg").read_text(encoding="utf-8")
_BRAND_LOGO = _BRAND_LOGO[_BRAND_LOGO.index("<svg"):]
_BRAND_LOGO = re.sub(r"<defs>.*?</defs>", "", _BRAND_LOGO, flags=re.S)
_BRAND_LOGO = _BRAND_LOGO.replace('class="cls-1"', 'fill="#F8F4F1"')


def _shell(title, body):
    return f"""<!doctype html>
<html lang="uk"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)} · LY Workday</title>
<style>{ENROLLMENT_STYLE}</style></head><body>
<div class="layout">
<aside class="brand" aria-label="Love Yourself">
  <div class="brand-logo" role="img" aria-label="Love Yourself">{_BRAND_LOGO}</div>
  <div class="brand-copy"><h2>Пауза серед<br>робочого дня.</h2>
  <p>Тридцять секунд, щоб перервати робочий потік і відчути свій стан.</p></div>
  <p class="brand-footer">LY Workday · by Love Yourself</p>
</aside>
<main><div class="entry"><p class="product-name">LY Workday</p>{body}
<footer class="privacy-note">Робоча адреса потрібна для перевірки доступу.
<a class="help-link" href="/enroll/privacy-policy" target="_blank" rel="noopener noreferrer">Політика конфіденційності</a><br>
<a class="help-link" href="mailto:hello@loveyourselfua.com">Звернутися до підтримки</a></footer>
</div></main></div></body></html>"""


def _page(deployment_id, browser, *, note='', email_step='address'):
    csrf = html.escape(access.digest(f'{browser}:{deployment_id}', 'csrf'), quote=True)
    hidden = f'<input type="hidden" name="csrf" value="{csrf}">'
    base = f'/enroll/{deployment_id}'
    methods = []
    if email_step == 'code':
        methods.append(f'<form method="post" action="{base}/verify">{hidden}'
            '<label for="code">Код із листа</label>'
            '<input id="code" name="code" required maxlength="128" autocomplete="one-time-code">'
            '<button>Підтвердити код</button></form>'
            f'<p><a href="{base}">Почати вхід заново</a></p>')
    else:
        if all((settings.SMTP_HOST, settings.SMTP_FROM, settings.SMTP_USERNAME, settings.SMTP_PASSWORD)):
            methods.append(f'<form method="post" action="{base}/email">{hidden}'
                '<label for="email">Робоча електронна адреса</label>'
                '<input id="email" type="email" name="email" required maxlength="254" autocomplete="email" placeholder="name@company.com">'
                '<button>Отримати код</button></form>')
        has_email = bool(methods)
        has_providers = False
        for provider, label in (('google', 'Google Workspace'), ('microsoft', 'Microsoft 365')):
            prefix = provider.upper()
            if getattr(settings, f'{prefix}_CLIENT_ID') and getattr(settings, f'{prefix}_CLIENT_SECRET'):
                if has_email and not has_providers:
                    methods.append('<div class="separator"><span>або</span></div>')
                has_providers = True
                methods.append(f'<form class="provider" method="post" action="{base}/oidc/{provider}">{hidden}'
                    f'<button>Увійти через {label}</button></form>')
    if not methods:
        methods.append('<p>Спосіб входу зараз недоступний. Спробуй пізніше або звернися до підтримки.</p>')
    announcement = f'<p role="status">{html.escape(note)}</p>' if note else ''
    return _shell('Вхід', '<h1>Вхід до LY Workday</h1>'
        '<p>Підтвердь робочий акаунт, щоб відкрити бота в Telegram.</p>'
        + announcement + ''.join(methods))


def enrollment_error(deployment_id, *, rate_limited=False):
    note = 'Забагато спроб. Спробуй трохи пізніше.' if rate_limited else (
        'Не вдалося підтвердити доступ. Почни вхід заново, щоб отримати новий код або посилання.'
    )
    heading = "Забагато спроб входу" if rate_limited else "Не вдалося увійти"
    return HTMLResponse(
        _shell('Вхід', f'<h1>{heading}</h1><p role="status">{note}</p>'
               f'<a class="primary" href="/enroll/{deployment_id}">Повернутися до входу</a>'),
        status_code=429 if rate_limited else 403,
    )


def _handoff(deployment_id, raw):
    link = html.escape(f'https://t.me/{settings.BOT_USERNAME}?start={raw}', quote=True)
    return HTMLResponse(_shell('Відкрити Telegram',
        '<div class="success-mark" aria-hidden="true">✓</div><h1>Доступ підтверджено</h1><p>Відкрий бота в Telegram і натисни «Старт», якщо Telegram покаже цю кнопку.</p>'
        f'<a class="primary" rel="noreferrer" href="{link}">Відкрити Telegram</a>'
        '<p>Посилання одноразове й діє 10 хвилин.</p>'
        f'<p><a href="/enroll/{deployment_id}">Повернутися до входу</a></p>'))


@router.get('/enroll/privacy-policy', response_class=HTMLResponse)
async def enrollment_privacy_policy():
    # Temporary reader draft authorized by the founder on 2026-11-05.
    # WP-10.1 replaces this asset with the approved policy before launch.
    text = (Path(__file__).parent / 'assets' / 'privacy_policy_uk.md').read_text(encoding='utf-8')
    blocks = []
    for block in text.strip().split('\n\n'):
        if block.startswith('# '):
            blocks.append(f'<h1>{html.escape(block[2:])}</h1>')
        elif block.startswith('## '):
            blocks.append(f'<h2>{html.escape(block[3:])}</h2>')
        else:
            safe = html.escape(block)
            safe = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', safe)
            blocks.append(f'<p>{safe}</p>')
    return HTMLResponse(_shell('Політика конфіденційності', '<article class="policy-copy">' + ''.join(blocks) + '</article>'))


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
    return HTMLResponse(_page(deployment_id, browser, note='Якщо адресу можна підтвердити, введи код із листа.', email_step='code'))


@router.post('/enroll/{deployment_id}/verify')
async def email_verify(deployment_id: int, request: Request):
    form, browser = await _form(request, deployment_id)
    access_proof.rate_limit('verify:' + browser, limit=10, window=600)
    with SessionLocal() as db:
        raw = access_proof.finish_email(db, deployment_id=deployment_id, raw=form.get('code', ''), browser=browser)
        db.commit()
    return _handoff(deployment_id, raw)


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
        return _handoff(deployment_id, result['handoff'])
    if result.get('email_required'):
        return HTMLResponse(_page(deployment_id, browser,
            note='Microsoft підтвердив вхід. Вкажи адресу для доступу й підтвердь її кодом із листа.'))
    try:
        access_proof.send_email(result['email'], result['challenge'])
    except Exception:
        pass
    return HTMLResponse(_page(deployment_id, browser, note='Введи код підтвердження адреси з листа.', email_step='code'))


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
        wake_delivery = access.set_deployment_controls(db, **body, operator=operator)
        db.commit()
    if wake_delivery:
        from app.scheduler import wake_scheduled_delivery_recovery
        await asyncio.to_thread(wake_scheduled_delivery_recovery)
    return {'accepted': True}

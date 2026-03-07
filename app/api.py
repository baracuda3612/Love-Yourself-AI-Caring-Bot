from dataclasses import asdict
from pathlib import Path
from typing import Dict

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app.config import settings
from app.db import AIPlan, SessionLocal, User
from app.plan_completion.cta import get_next_plan_recommendation
from app.plan_completion.metrics import build_completion_metrics
from app.plan_completion.pulse import build_pulse_data
from app.plan_completion.report import _pick_observation, build_completion_report
from app.plan_completion.timeline import get_plan_timeline
from app.plan_completion.tokens import verify_report_token
from app.scheduler import cancel_plan_step_jobs, reschedule_plan_steps
from app.time_slots import TimeSlotError, update_user_time_slots

app = FastAPI()
try:
    templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
except AssertionError:
    templates = None

_ASSETS = Path(__file__).parent / "assets"
try:
    _LOGO_LONG = (_ASSETS / "logo_long.svg").read_text(encoding="utf-8")
    _LOGO_ICON = (_ASSETS / "logo_icon.svg").read_text(encoding="utf-8")
except FileNotFoundError:
    _LOGO_LONG = _LOGO_ICON = ""


def _first_line(text: str) -> str:
    return text.split("\n")[0] if text else ""


if templates is not None:
    templates.env.filters["first_line"] = _first_line


def _deep_link(params: dict) -> str:
    return (
        f"https://t.me/{settings.BOT_USERNAME}"
        f"?start=newplan_{params['duration']}_{params['load']}_{params['focus']}"
    )


FOCUS_LABELS = {
    "cognitive": "Когнітивне",
    "rest": "Відновлення",
    "body": "Тіло",
    "emotional": "Емоційне",
    "mixed": "Змішане",
}
SLOT_LABELS = {"MORNING": "Ранок", "DAY": "День", "EVENING": "Вечір"}
DUR_LABELS = {
    "SHORT": "7 днів",
    "MEDIUM": "14 днів",
    "STANDARD": "21 день",
    "LONG": "30 днів",
}


FOCUS_LABELS_PULSE = {
    "cognitive": "Когнітивне відновлення",
    "rest": "Відновлення",
    "body": "Фізичне",
    "emotional": "Емоційне",
    "mixed": "Комплексне",
}

DUR_LABELS_PULSE = {
    "SHORT": "7-денний план",
    "MEDIUM": "14-денний план",
    "STANDARD": "21-денний план",
    "LONG": "90-денний план",
}


class TimeSlotsPayload(BaseModel):
    MORNING: str
    DAY: str
    EVENING: str

    def to_dict(self) -> Dict[str, str]:
        return {
            "MORNING": self.MORNING,
            "DAY": self.DAY,
            "EVENING": self.EVENING,
        }


@app.post("/user/time-slots")
def set_user_time_slots(
    payload: TimeSlotsPayload,
    user_id: int = Query(..., description="User ID to update"),
) -> Dict[str, int]:
    with SessionLocal() as db:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            raise HTTPException(status_code=404, detail="user_not_found")
        try:
            updated_step_ids, active_step_ids = update_user_time_slots(
                db, user, payload.to_dict()
            )
        except TimeSlotError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        db.commit()

    cancel_plan_step_jobs(active_step_ids)
    reschedule_plan_steps(active_step_ids)

    return {"updated_steps": len(updated_step_ids)}


@app.get("/report/{token}", response_class=HTMLResponse)
async def completion_report(request: Request, token: str):
    plan_id = verify_report_token(token, settings.REPORT_TOKEN_SECRET)
    if plan_id is None:
        return HTMLResponse("<h1>Посилання недійсне або застаріле.</h1>", status_code=404)

    with SessionLocal() as db:
        plan = db.query(AIPlan).filter(AIPlan.id == plan_id).first()
        if not plan:
            return HTMLResponse("<h1>План не знайдено.</h1>", status_code=404)
        user_id = plan.user_id

        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            return HTMLResponse("<h1>Користувача не знайдено.</h1>", status_code=404)

        try:
            metrics = build_completion_metrics(db, user_id, plan_id)
        except Exception:
            return HTMLResponse("<h1>Дані плану недоступні.</h1>", status_code=404)

        persona = "empath"
        if isinstance(user.profile, dict):
            persona = user.profile.get("persona", "empath")

        report_text = build_completion_report(metrics, persona)
        observation = _pick_observation(metrics)
        cta = get_next_plan_recommendation(metrics)
        timeline = get_plan_timeline(db, user_id, plan_id)

    if templates is None:
        return HTMLResponse(
            f"<h1>{_first_line(report_text)}</h1><p>{round(metrics.completion_rate * 100)}%</p>",
            status_code=200,
        )

    tier_class = {
        "STRONG": "tier-strong",
        "NEUTRAL": "tier-neutral",
        "WEAK": "tier-weak",
    }.get(metrics.outcome_tier, "tier-neutral")

    return templates.TemplateResponse(
        "completion_report.html",
        {
            "request": request,
            "tier_class": tier_class,
            "report_text": report_text,
            "observation": observation,
            "completion_rate": round(metrics.completion_rate * 100),
            "total_days": metrics.total_days,
            "completed_days": round(metrics.completion_rate * metrics.total_days),
            "best_streak": metrics.best_streak,
            "adaptation_count": metrics.adaptation_count,
            "dominant_slot": SLOT_LABELS.get(metrics.dominant_time_slot or "", ""),
            "focus_label": FOCUS_LABELS.get(
                (metrics.focus or "").lower(), metrics.focus or ""
            ),
            "duration_label": DUR_LABELS.get(metrics.duration or "", ""),
            "cta_button1_text": cta.button1_text,
            "cta_button2_text": cta.button2_text,
            "cta_button1_link": _deep_link(cta.button1_params),
            "cta_button2_link": _deep_link(cta.button2_params),
            "timeline": [{"day": d.day, "status": d.status} for d in timeline],
            "logo_long_svg": _LOGO_LONG,
            "logo_icon_svg": _LOGO_ICON,
        },
    )


@app.get("/pulse/{token}", response_class=HTMLResponse)
async def pulse_report(request: Request, token: str):
    plan_id = verify_report_token(token, settings.REPORT_TOKEN_SECRET)
    if plan_id is None:
        return HTMLResponse("<h1>Посилання недійсне або застаріле.</h1>", status_code=404)

    with SessionLocal() as db:
        plan = db.query(AIPlan).filter(AIPlan.id == plan_id).first()
        if not plan:
            return HTMLResponse("<h1>План не знайдено.</h1>", status_code=404)

        if plan.status != "active":
            return HTMLResponse("<h1>План не активний.</h1>", status_code=404)

        try:
            data = build_pulse_data(plan_id, db)
        except Exception:
            return HTMLResponse("<h1>Дані плану недоступні.</h1>", status_code=404)

    if templates is None:
        return HTMLResponse(f"<h1>Pulse: день {data.active_day_number}</h1>")

    return templates.TemplateResponse(
        "pulse.html",
        {
            "request": request,
            "plan_name": DUR_LABELS_PULSE.get(plan.duration or "", "План"),
            "plan_focus": FOCUS_LABELS_PULSE.get((plan.focus or "").lower(), ""),
            "active_day_number": data.active_day_number,
            "plan_total_days": data.plan_total_days,
            "plan_percent": data.plan_percent,
            "week_number": data.week_number,
            "window_done": data.window_done,
            "window_total": data.window_total,
            "phrase": data.phrase,
            "days_json": [asdict(d) for d in data.days],
            "logo_long_svg": _LOGO_LONG,
            "logo_icon_svg": _LOGO_ICON,
        },
    )

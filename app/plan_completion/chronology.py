"""Calendar rules for the explicit source-plan completion seam."""
from datetime import datetime, timedelta, timezone

from app.active_days import next_active_date, resolve_active_days, resolve_timezone
from app.time_slots import canonicalize_hhmm, compute_scheduled_for


def utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def report_due_at(user, last_terminal, *, now):
    """Actions close quickly; no-action uses expiry's next workday and saved DAY."""
    if last_terminal.step_status != 'expired':
        return now
    if last_terminal.expires_at is None:
        raise ValueError('expiry_missing')
    return next_workday_day_at(user, utc(last_terminal.expires_at))


def next_workday_day_at(user, after):
    profile = user.profile
    slots = getattr(profile, 'daily_time_slots', None) or {}
    if 'DAY' not in slots:
        raise ValueError('saved_day_time_missing')
    day_time = canonicalize_hhmm(slots['DAY'])
    tz = resolve_timezone(user.timezone)
    report_day = next_active_date(utc(after).astimezone(tz).date() + timedelta(days=1), resolve_active_days(profile))
    return compute_scheduled_for(
        plan_start=after, day_number=1, time_slot='DAY', timezone_name=user.timezone,
        daily_time_slots={'DAY': day_time}, anchor_date=report_day,
    )


def last_terminal_step(db, plan_id):
    """Legacy unknown timestamps cannot hide the latest proven terminal fact."""
    from app.db import AIPlanDay, AIPlanStep
    return (db.query(AIPlanStep).join(AIPlanDay)
        .filter(AIPlanDay.plan_id == plan_id)
        .order_by(AIPlanStep.terminal_at.desc().nullslast(), AIPlanStep.id.desc()).populate_existing().first())

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
    profile = user.profile
    slots = getattr(profile, 'daily_time_slots', None) or {}
    if 'DAY' not in slots:
        raise ValueError('saved_day_time_missing')
    day_time = canonicalize_hhmm(slots['DAY'])
    tz = resolve_timezone(user.timezone)
    expiry = last_terminal.expires_at
    if expiry is None:
        raise ValueError('expiry_missing')
    report_day = next_active_date(utc(expiry).astimezone(tz).date() + timedelta(days=1), resolve_active_days(profile))
    return compute_scheduled_for(
        plan_start=now, day_number=1, time_slot='DAY', timezone_name=user.timezone,
        daily_time_slots={'DAY': day_time}, anchor_date=report_day,
    )

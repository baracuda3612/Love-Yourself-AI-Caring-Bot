"""Canonical plan duration rules and helpers."""

from __future__ import annotations

CANONICAL_PLAN_DAYS = frozenset({7, 14, 21, 90})
DAYS_TO_DURATION = {
    7: "SHORT",
    14: "MEDIUM",
    21: "STANDARD",
    90: "LONG",
}
DURATION_TO_DAYS = {value: key for key, value in DAYS_TO_DURATION.items()}


class InvalidDurationError(ValueError):
    """Duration is not canonical."""


def normalize_duration_value(duration: str | int) -> str:
    """Normalize duration input to canonical enum name."""
    if isinstance(duration, int):
        enum_name = DAYS_TO_DURATION.get(duration)
        if enum_name is None:
            raise InvalidDurationError(f"invalid duration days: {duration}")
        return enum_name

    if isinstance(duration, str):
        candidate = duration.strip().upper()
        if candidate in DURATION_TO_DAYS:
            return candidate
        if candidate.isdigit():
            enum_name = DAYS_TO_DURATION.get(int(candidate))
            if enum_name is not None:
                return enum_name

    raise InvalidDurationError(f"invalid duration value: {duration}")


def assert_canonical_total_days(total_days: int) -> None:
    """Raise if total_days is not canonical."""
    if total_days not in CANONICAL_PLAN_DAYS:
        raise InvalidDurationError(f"invalid total_days={total_days}")

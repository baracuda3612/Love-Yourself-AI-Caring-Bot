"""Side-effect-free delivery eligibility guards."""

from __future__ import annotations


_NO_SEND_STEP_STATUSES = frozenset(
    {"completed", "skipped", "expired", "canceled", "delivered"}
)


def can_send_step_status(status: str | None) -> bool:
    """Reject terminal and already-delivered work, including after a restore."""

    return status not in _NO_SEND_STEP_STATUSES

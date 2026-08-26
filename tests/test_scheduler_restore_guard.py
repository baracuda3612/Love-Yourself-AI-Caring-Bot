from __future__ import annotations

import pytest

from app.delivery_guards import can_send_step_status


@pytest.mark.parametrize(
    "status", ["completed", "skipped", "expired", "canceled", "delivered"]
)
def test_restore_guard_rejects_terminal_and_already_delivered_steps(
    status: str,
) -> None:
    assert can_send_step_status(status) is False


@pytest.mark.parametrize("status", [None, "pending", "scheduled"])
def test_restore_guard_allows_unsent_step_statuses(status: str | None) -> None:
    assert can_send_step_status(status) is True

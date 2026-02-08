"""Plan execution guards and invariants."""

from app.db import AIPlanStep


def is_step_terminal(step: AIPlanStep) -> bool:
    """
    Check if step is in terminal state.

    Terminal = completed OR skipped (mutually exclusive final states).
    Once terminal, step cannot be modified by normal user actions.

    Returns:
        True if step is completed or skipped.
    """
    return bool(step.is_completed or step.skipped)


def is_plan_active(step: AIPlanStep) -> bool:
    """
    Check if plan is in ACTIVE state (can execute tasks).

    Requirements:
    - plan.status == "active"
    - user.current_state == "ACTIVE"

    Returns:
        True if both conditions met.
    """
    if not step.day or not step.day.plan:
        return False

    plan = step.day.plan
    user = plan.user

    if not user:
        return False

    return plan.status == "active" and user.current_state == "ACTIVE"


def get_terminal_reason(step: AIPlanStep) -> str | None:
    """
    Get reason why step is terminal (for user feedback).

    Returns:
        "completed" | "skipped" | None.
    """
    if step.is_completed:
        return "completed"
    if step.skipped:
        return "skipped"
    return None


def validate_step_action(step: AIPlanStep) -> tuple[bool, str]:
    """
    Validate if user action on step is allowed.

    Returns:
        (is_allowed, error_message)

    Examples:
        (True, "") - action allowed
        (False, "План зараз не активний") - blocked
        (False, "Завдання вже виконано") - terminal
    """
    if not is_plan_active(step):
        return (False, "План зараз не активний")

    if is_step_terminal(step):
        reason = get_terminal_reason(step)
        if reason == "completed":
            return (False, "Завдання вже виконано")
        if reason == "skipped":
            return (False, "Завдання вже пропущено")
        return (False, "Завдання вже завершене")

    return (True, "")

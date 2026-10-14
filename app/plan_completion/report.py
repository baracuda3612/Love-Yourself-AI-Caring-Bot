"""Factual Telegram closure; final deterministic web/image payload is WP-07.4."""
from app.plan_completion.metrics import CompletionMetrics


def _pick_observation(metrics: CompletionMetrics) -> str:
    # Compatibility for the existing WP-07.4 web view; no inferred observations.
    return f"Виконано вправ: {metrics.total_completed} із {metrics.total_delivered}."


def build_completion_report(metrics: CompletionMetrics, persona: str = '', *, continuation: str | None = None) -> str:
    text = (
        f"✅ План на {metrics.total_days} робочих днів завершено.\n"
        f"Виконано вправ: {metrics.total_completed} із {metrics.total_delivered}.\n"
        f"Пропущено: {metrics.total_skipped}. Без відповіді: {metrics.total_ignored}."
    )
    if continuation:
        text += '\n\n' + continuation
    return text

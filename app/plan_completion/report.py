from __future__ import annotations

from app.plan_completion.metrics import (
    CompletionMetrics,
    STRONG_THRESHOLD,
)

HEADERS: dict[str, dict[str, str]] = {
    "strong": {
        "motivator": "✅ {total_days} днів. {total_completed}/{total_delivered} вправ виконано. Ти довів що можеш — тепер далі.",
        "empath": "✅ План завершено. {total_days} днів разом із собою. {total_completed} вправ з {total_delivered}. Це справжня робота.",
        "rationalist": "✅ {total_days} днів / {total_completed} з {total_delivered} вправ ({rate_pct}%). Факти — на твоєму боці.",
    },
    "neutral": {
        "motivator": "📋 {total_days} днів пройдено. {total_completed} з {total_delivered}. Не ідеально — але ти дійшов до кінця.",
        "empath": "📋 {total_days} днів. {total_completed} вправ з {total_delivered} виконано. Не кожен день виходило — і це нормально. Ти дійшов.",
        "rationalist": "📋 {total_days} днів. {rate_pct}% виконання. Є куди рухатись — але база закладена.",
    },
    "neutral_adapted": {
        "motivator": "📋 {total_days} днів. {total_completed}/{total_delivered}. Ти змінював план по дорозі — і дійшов. Це краща стратегія ніж кинути.",
        "empath": "📋 {total_days} днів. Ти адаптував план під себе {adaptation_count} рази — і завершив. Це важливіше за ідеальний відсоток.",
        "rationalist": "📋 {total_days} днів / {rate_pct}% / {adaptation_count} адаптацій. Гнучкість дозволила дійти до фінішу.",
    },
    "weak": {
        "motivator": "📋 {total_days} днів позаду. {total_completed} з {total_delivered}. Цей формат не зайшов — це інформація, не вирок.",
        "empath": "📋 {total_days} днів. {total_completed} вправ з {total_delivered}. Схоже, цей план і цей момент не збіглись. Буває.",
        "rationalist": "📋 {total_days} днів / {rate_pct}%. Низький показник — сигнал що формат або момент не підійшли.",
    },
}


VALID_PERSONAS = {"motivator", "empath", "rationalist"}


def _outcome_key(metrics: CompletionMetrics) -> str:
    if metrics.outcome_tier == "STRONG":
        return "strong"
    if metrics.outcome_tier == "NEUTRAL" and metrics.had_adaptations:
        return "neutral_adapted"
    if metrics.outcome_tier == "NEUTRAL":
        return "neutral"
    return "weak"


def _pick_observation(metrics: CompletionMetrics) -> str:
    if metrics.completion_rate >= STRONG_THRESHOLD and metrics.best_streak >= 7:
        return "7+ днів поспіль — це вже не випадковість."
    if metrics.had_adaptations and metrics.completion_rate >= 0.70:
        return "Ти змінював план по дорозі — і це спрацювало."
    if metrics.dominant_time_slot == "MORNING" and metrics.completion_rate >= 0.60:
        return "Ранок виявився твоїм часом."
    if metrics.completion_rate >= STRONG_THRESHOLD:
        return "Ти тримав ритм навіть коли було складно."
    if metrics.best_streak >= 7:
        return "Тиждень поспіль — перший реальний поріг пройдено."
    return f"Ти пройшов {metrics.total_days} днів. Це більше ніж більшість."


def build_completion_report(metrics: CompletionMetrics, persona: str) -> str:
    """
    Повертає готовий текст completion message для Telegram.
    Детерміністично. Без LLM. Persona-aware.
    persona: 'motivator' | 'empath' | 'rationalist'
    """
    persona_key = persona if persona in VALID_PERSONAS else "empath"
    header = HEADERS[_outcome_key(metrics)][persona_key]
    formatted = header.format(
        total_days=metrics.total_days,
        total_completed=metrics.total_completed,
        total_delivered=metrics.total_delivered,
        adaptation_count=metrics.adaptation_count,
        rate_pct=round(metrics.completion_rate * 100),
    )
    observation = _pick_observation(metrics)

    lines = [formatted]
    if metrics.best_streak >= 3 or metrics.adaptation_count > 0:
        lines.append(
            f"Streak: {metrics.best_streak} дн. • адаптацій: {metrics.adaptation_count}"
        )

    return "\n".join(lines + ["", observation])

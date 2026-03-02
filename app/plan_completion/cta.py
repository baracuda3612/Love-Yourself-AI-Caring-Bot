from __future__ import annotations

from dataclasses import dataclass

from app.plan_completion.metrics import CompletionMetrics


@dataclass(frozen=True)
class CTARecommendation:
    recommended_duration: str
    recommended_load: str
    recommended_focus: str
    button1_text: str
    button1_params: dict
    button2_text: str
    button2_params: dict


LOAD_ORDER = ["LITE", "MID", "INTENSIVE"]
DURATION_ORDER = ["SHORT", "MEDIUM", "STANDARD", "LONG"]


def _load_up(load: str) -> str:
    idx = LOAD_ORDER.index(load) if load in LOAD_ORDER else 0
    return LOAD_ORDER[min(idx + 1, len(LOAD_ORDER) - 1)]


def _load_down(load: str) -> str:
    idx = LOAD_ORDER.index(load) if load in LOAD_ORDER else 1
    return LOAD_ORDER[max(idx - 1, 0)]


def _duration_up(duration: str) -> str:
    idx = DURATION_ORDER.index(duration) if duration in DURATION_ORDER else 0
    return DURATION_ORDER[min(idx + 1, len(DURATION_ORDER) - 1)]


def get_next_plan_recommendation(metrics: CompletionMetrics) -> CTARecommendation:
    focus = metrics.focus or "mixed"
    load = metrics.load or "LITE"
    duration = metrics.duration or "SHORT"

    if metrics.outcome_tier == "STRONG" and load == "INTENSIVE":
        next_duration = _duration_up(duration)
        return CTARecommendation(
            recommended_duration=next_duration,
            recommended_load="INTENSIVE",
            recommended_focus=focus,
            button1_text="Поглибити →",
            button1_params={"duration": next_duration, "load": "INTENSIVE", "focus": focus},
            button2_text="Повторити цей план",
            button2_params={"duration": duration, "load": load, "focus": focus},
        )

    if metrics.outcome_tier == "STRONG":
        load_up = _load_up(load)
        return CTARecommendation(
            recommended_duration=duration,
            recommended_load=load_up,
            recommended_focus=focus,
            button1_text="Піти далі →",
            button1_params={"duration": duration, "load": load_up, "focus": focus},
            button2_text="Повторити цей план",
            button2_params={"duration": duration, "load": load, "focus": focus},
        )

    if metrics.outcome_tier == "NEUTRAL" and metrics.had_adaptations:
        load_down = _load_down(load)
        return CTARecommendation(
            recommended_duration=duration,
            recommended_load=load_down,
            recommended_focus=focus,
            button1_text="Спробувати легше",
            button1_params={"duration": duration, "load": load_down, "focus": focus},
            button2_text="Повторити цей план",
            button2_params={"duration": duration, "load": load, "focus": focus},
        )

    if metrics.outcome_tier == "NEUTRAL":
        return CTARecommendation(
            recommended_duration="SHORT",
            recommended_load=load,
            recommended_focus=focus,
            button1_text="Почати коротший план",
            button1_params={"duration": "SHORT", "load": load, "focus": focus},
            button2_text="Повторити цей план",
            button2_params={"duration": duration, "load": load, "focus": focus},
        )

    if metrics.outcome_tier == "WEAK" and load != "LITE":
        alt_focus = "rest" if focus != "rest" else "mixed"
        return CTARecommendation(
            recommended_duration="SHORT",
            recommended_load="LITE",
            recommended_focus=focus,
            button1_text="Спробувати легший старт",
            button1_params={"duration": "SHORT", "load": "LITE", "focus": focus},
            button2_text="Змінити напрямок",
            button2_params={"duration": "SHORT", "load": load, "focus": alt_focus},
        )

    alt_focus = "rest" if focus != "rest" else "mixed"
    return CTARecommendation(
        recommended_duration="SHORT",
        recommended_load="LITE",
        recommended_focus=alt_focus,
        button1_text="Спробувати інший напрямок",
        button1_params={"duration": "SHORT", "load": "LITE", "focus": alt_focus},
        button2_text="Спробувати ще раз",
        button2_params={"duration": "SHORT", "load": "LITE", "focus": focus},
    )

PERSONAS = {"motivator", "empath", "rationalist"}
DEFAULT_PERSONA = "empath"

# Shared tone contract — single source of truth for all components
# that generate persona-aware messages (pulse, triggers, future agents)
PERSONA_TONE = {
    "motivator": "короткий, енергійний, без пафосу. Як друг який сам через це пройшов.",
    "empath": "теплий, без терапевтичної мови. Як близька людина яка розуміє.",
    "rationalist": "факт + практична дія. Як розумний друг у неформальній обстановці.",
}


def get_persona(user_profile) -> str:
    if not user_profile:
        return DEFAULT_PERSONA
    raw = getattr(user_profile, "coach_persona", None)
    return raw if raw in PERSONAS else DEFAULT_PERSONA


def get_sent_indices(user_profile) -> list[int]:
    val = getattr(user_profile, "pulse_sent_indices", None) if user_profile else None
    return val if isinstance(val, list) else []


def record_sent_index(db, user_profile, index: int):
    indices = get_sent_indices(user_profile)
    indices.append(index)
    user_profile.pulse_sent_indices = indices[-50:]
    db.add(user_profile)

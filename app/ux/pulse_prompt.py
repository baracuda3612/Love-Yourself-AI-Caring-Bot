from __future__ import annotations

import logging

from app.ai import extract_output_text
from app.config import settings
from app.ux.catalog import get_coach_voice, get_unused_quote
from app.ux.persona import PERSONA_TONE, get_persona, get_sent_indices, record_sent_index

logger = logging.getLogger(__name__)


async def _complete(llm_client, system: str, user: str) -> str:
    if hasattr(llm_client, "complete"):
        return await llm_client.complete(
            system=system,
            user=user,
            max_tokens=150,
            temperature=0.7,
        )

    response = await llm_client.responses.create(
        model=settings.MODEL,
        input=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        max_output_tokens=150,
        temperature=0.7,
    )
    return extract_output_text(response)


# TECH-DEBT TD-8:
# LLM is called per-user per-day.
# Introduce commentary caching per (quote, persona)
# before scaling beyond several thousand users.
async def generate_pulse_message(user_profile, db, llm_client) -> str:
    persona = get_persona(user_profile)
    sent = get_sent_indices(user_profile)
    result = get_unused_quote(persona, sent)

    if not result:
        return get_coach_voice(persona) or ""

    quote_text, author, why, idx = result
    tone = PERSONA_TONE.get(persona, PERSONA_TONE["empath"])

    try:
        commentary = await _complete(
            llm_client=llm_client,
            system=(
                f"Ти — коуч wellbeing-додатку. Тон: {tone}. "
                "Напиши 2-3 речення до цитати. "
                "НЕ починай з 'Ця цитата', 'Сьогодні', 'Доброго ранку'. "
                "НЕ використовуй: валідно, простір, ресурс, інсайт, трансформація."
            ),
            user=(
                f'Цитата: "{quote_text}"\nАвтор: {author}\n'
                f"Контекст чому вона важлива: {why}\n\n"
                "Напиши 2-3 речення коментаря від коуча."
            ),
        )
    except Exception:
        logger.warning("[PULSE] LLM failed, fallback to coach_voice", exc_info=True)
        commentary = get_coach_voice(persona) or ""

    record_sent_index(db, user_profile, idx)
    return f'<i>"{quote_text}"</i>\n— {author}\n\n{commentary}'

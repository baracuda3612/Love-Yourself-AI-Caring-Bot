from openai import OpenAI

from app.config import settings

client = OpenAI(api_key=settings.OPENAI_API_KEY)


def _usage_dict(resp):
    usage = getattr(resp, "usage", None)
    if not usage:
        return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    return {
        "prompt_tokens": getattr(usage, "prompt_tokens", 0),
        "completion_tokens": getattr(usage, "completion_tokens", 0),
        "total_tokens": getattr(usage, "total_tokens", 0),
    }


def _call_openai(messages):
    """Базовий виклик OpenAI; залишається для сумісності клієнта."""
    resp = client.chat.completions.create(
        model=settings.MODEL,
        messages=messages,
        max_tokens=settings.MAX_TOKENS,
        temperature=settings.TEMPERATURE,
    )
    return resp.choices[0].message.content, _usage_dict(resp)


__all__ = ["client", "_call_openai"]

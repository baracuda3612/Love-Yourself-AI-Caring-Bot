from openai import AsyncOpenAI

from app.config import settings

async_client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)


def _usage_dict(resp):
    usage = getattr(resp, "usage", None)
    if not usage:
        return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    prompt_tokens = getattr(usage, "prompt_tokens", getattr(usage, "input_tokens", 0))
    completion_tokens = getattr(
        usage, "completion_tokens", getattr(usage, "output_tokens", 0)
    )
    total_tokens = getattr(usage, "total_tokens", prompt_tokens + completion_tokens)
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
    }


async def _call_openai(messages):
    """Базовий виклик OpenAI; залишається для сумісності клієнта."""
    resp = await async_client.responses.create(
        model=settings.MODEL,
        input=messages,
        max_output_tokens=settings.MAX_TOKENS,
        temperature=settings.TEMPERATURE,
    )
    return resp.output_text or "", _usage_dict(resp)


__all__ = ["async_client", "_call_openai"]

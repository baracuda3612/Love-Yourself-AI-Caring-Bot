from typing import Any

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


def extract_output_text(response: Any) -> str:
    """Return the text output from an OpenAI Responses object.

    The Responses API may expose the generated text via ``output_text``
    (newer SDKs) or nested under ``output[0].content[0].text``. Using this
    helper prevents AttributeErrors that would otherwise force the
    orchestrator to fall back to the default coach agent.
    """

    text = getattr(response, "output_text", None)
    if text:
        return text

    try:
        output = getattr(response, "output", None)
        if output:
            content = getattr(output[0], "content", None)
            if content:
                nested_text = getattr(content[0], "text", None)
                if nested_text:
                    return nested_text
    except Exception:
        # Unexpected SDK structures are handled gracefully downstream.
        pass

    return ""


def extract_tool_call(response: Any) -> dict | None:
    """Return the first tool/function call from an OpenAI Responses object, or None.

    Responses API: tool calls appear as items in response.output with
    type == "function_call". Each item has .name and .arguments (JSON string).
    Returns {"name": str, "arguments": dict} or None if no tool call present.
    """
    import json

    try:
        output = getattr(response, "output", None)
        if not output:
            return None
        for item in output:
            if getattr(item, "type", None) == "function_call":
                name = getattr(item, "name", None)
                raw_args = getattr(item, "arguments", None) or "{}"
                try:
                    arguments = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                except (ValueError, TypeError):
                    arguments = {}
                if name:
                    return {"name": name, "arguments": arguments}
    except Exception:
        pass

    return None


async def _call_openai(messages):
    """Базовий виклик OpenAI; залишається для сумісності клієнта."""
    resp = await async_client.responses.create(
        model=settings.MODEL,
        input=messages,
        max_output_tokens=settings.MAX_TOKENS,
        temperature=settings.TEMPERATURE,
    )
    return extract_output_text(resp), _usage_dict(resp)


__all__ = ["async_client", "_call_openai", "extract_output_text", "extract_tool_call"]

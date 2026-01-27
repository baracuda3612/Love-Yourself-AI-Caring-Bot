import json
from typing import Any

from app.ai import extract_output_text


def log_llm_response_shape(logger, response: Any, agent: str) -> None:
    payload = {
        "event_type": "router_llm_response_shape",
        "agent": agent,
        "response_type": type(response).__name__,
        "has_output_text": hasattr(response, "output_text"),
        "has_output": hasattr(response, "output"),
        "output_type": type(getattr(response, "output", None)).__name__,
        "output_len": (
            len(response.output)
            if hasattr(response, "output") and isinstance(response.output, list)
            else None
        ),
    }
    logger.info(json.dumps(payload))


def log_llm_text_candidates(logger, response: Any, agent: str) -> None:
    candidates = []

    text = extract_output_text(response)
    if text:
        candidates.append({"source": "extract_output_text", "preview": text[:300]})

    if hasattr(response, "output_text") and response.output_text:
        candidates.append({
            "source": "response.output_text",
            "preview": str(response.output_text)[:300],
        })

    output = getattr(response, "output", None)
    if isinstance(output, list):
        for i, item in enumerate(output[:2]):
            content = getattr(item, "content", None)
            if isinstance(content, list):
                for j, part in enumerate(content[:2]):
                    part_text = getattr(part, "text", None)
                    if part_text:
                        candidates.append({
                            "source": f"output[{i}].content[{j}].text",
                            "preview": part_text[:300],
                        })

    logger.info(
        json.dumps(
            {
                "event_type": "router_llm_text_candidates",
                "agent": agent,
                "candidates": candidates,
            },
            ensure_ascii=False,
        )
    )

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
    # Shapes only; generated text and tool arguments must never enter logs.
    logger.info("llm_text_available=%s", bool(extract_output_text(response)))

import json
import logging
from typing import Any, Dict

logger = logging.getLogger("router")


def log_router_decision(data: Dict[str, Any]) -> None:
    try:
        logger.info(json.dumps(data, ensure_ascii=False))
    except Exception:
        logger.info(str(data))

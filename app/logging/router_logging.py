import json
import logging
import sys
from typing import Any, Dict

logger = logging.getLogger("router")
logger.setLevel(logging.INFO)

# Configure a dedicated stdout handler so Railway captures router logs.
if not logger.handlers:
    handler = logging.StreamHandler(stream=sys.stdout)
    formatter = logging.Formatter("[ROUTER] %(asctime)s %(levelname)s: %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)

# Avoid duplicate messages from the root logger.
logger.propagate = False


def log_router_decision(data: Dict[str, Any]) -> None:
    try:
        logger.info(json.dumps(data, ensure_ascii=False))
    except Exception:
        logger.info(str(data))

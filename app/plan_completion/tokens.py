"""HMAC-signed report tokens. Stateless — no DB required."""

import base64
import hashlib
import hmac


def make_report_token(plan_id: int, secret: str) -> str:
    msg = f"{plan_id}".encode()
    sig = hmac.new(secret.encode(), msg, hashlib.sha256).digest()
    raw = f"{plan_id}:".encode() + sig
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def verify_report_token(token: str, secret: str) -> int | None:
    """Returns plan_id if valid, None otherwise."""
    try:
        padded = token + "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(padded)
        prefix, sig = raw[:-32], raw[-32:]
        plan_id = int(prefix.decode().rstrip(":"))
        expected = hmac.new(secret.encode(), f"{plan_id}".encode(), hashlib.sha256).digest()
        if hmac.compare_digest(sig, expected):
            return plan_id
    except Exception:
        return None
    return None

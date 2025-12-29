"""Helpers for loading the burnout recovery content library."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from sqlalchemy.orm import Session

from app.db import ContentLibrary


def _normalize_payload(raw: dict) -> dict:
    payload = dict(raw)
    payload.pop("id", None)
    return payload


def load_content_library(db: Session, source_path: str | Path) -> int:
    """Load or refresh the content library entries from JSON."""
    path = Path(source_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    inventory: Iterable[dict] = data.get("inventory", [])
    updated = 0

    for entry in inventory:
        content_id = entry["id"]
        existing = db.get(ContentLibrary, content_id)
        payload = _normalize_payload(entry)

        if existing:
            existing.content_version = int(entry.get("content_version") or existing.content_version or 1)
            existing.internal_name = entry.get("internal_name", existing.internal_name)
            existing.category = entry.get("category", existing.category)
            existing.difficulty = int(entry.get("difficulty", existing.difficulty or 1))
            existing.energy_cost = entry.get("energy_cost", existing.energy_cost)
            existing.logic_tags = entry.get("logic_tags", existing.logic_tags)
            existing.content_payload = entry.get("content_payload", payload)
            existing.is_active = bool(entry.get("is_active", True))
        else:
            db.add(
                ContentLibrary(
                    id=content_id,
                    content_version=int(entry.get("content_version") or 1),
                    internal_name=entry.get("internal_name", content_id),
                    category=entry.get("category", "somatic"),
                    difficulty=int(entry.get("difficulty") or 1),
                    energy_cost=entry.get("energy_cost", "LOW"),
                    logic_tags=entry.get("logic_tags", {}),
                    content_payload=entry.get("content_payload", payload),
                    is_active=bool(entry.get("is_active", True)),
                )
            )
        updated += 1

    return updated

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from scripts.inspect_database_schema import _database_url, _static_select


class RecordingCursor:
    def __init__(self) -> None:
        self.statements: list[tuple[str, object]] = []

    def execute(self, statement: str, params: object = None) -> None:
        self.statements.append((statement, params))

    def fetchall(self) -> list[dict[str, object]]:
        return []


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO users DEFAULT VALUES",
        "UPDATE users SET current_state = 'ACTIVE'",
        "DELETE FROM users",
        "ALTER TABLE users ADD COLUMN unsafe integer",
    ],
)
def test_static_query_guard_rejects_mutation(statement: str) -> None:
    with pytest.raises(ValueError, match="read-only"):
        _static_select(RecordingCursor(), statement)


def test_static_query_guard_executes_select_without_row_content() -> None:
    cursor = RecordingCursor()

    assert _static_select(cursor, "SELECT COUNT(*) AS row_count FROM users") == []
    assert cursor.statements == [("SELECT COUNT(*) AS row_count FROM users", None)]


def test_inspector_contract_excludes_personal_row_projection() -> None:
    source = Path("scripts/inspect_database_schema.py").read_text(encoding="utf-8")

    assert "set_session(readonly=True" in source
    assert "SELECT *" not in source.upper()
    assert "tg_id" not in source
    assert "username" not in source
    assert "first_name" not in source
    assert "mood_note" not in source
    assert "chat_history.text" not in source
    assert '"apscheduler_jobs"' in source
    assert '"owned_by": "APScheduler"' in source


def test_database_url_file_is_read_without_shell_expansion(tmp_path: Path) -> None:
    env_file = tmp_path / "database.env"
    env_file.write_text("\nDATABASE_URL=postgresql://example.invalid/test\n", encoding="utf-8")

    value = _database_url(argparse.Namespace(database_url_file=env_file))

    assert value == "postgresql://example.invalid/test"

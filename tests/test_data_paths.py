from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from pillow_assistant.core import data_paths
from pillow_assistant.core.data_paths import (
    DATA_DIR_ENV,
    MIGRATION_MARKER,
    resolve_database_path,
    user_data_dir,
)
from storage import Storage


def _database(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE sample (value TEXT NOT NULL)")
        conn.execute("INSERT INTO sample(value) VALUES (?)", (value,))


def _value(path: Path) -> str:
    with sqlite3.connect(path) as conn:
        row = conn.execute("SELECT value FROM sample").fetchone()
    return row[0]


def test_new_install_uses_user_data_directory(tmp_path):
    data_dir = tmp_path / "user-data"
    path = resolve_database_path(tmp_path / "missing" / "assistant.db", data_dir)

    assert path == data_dir / "assistant.db"
    assert data_dir.is_dir()
    assert not path.exists()


def test_existing_new_database_takes_precedence(tmp_path):
    legacy = tmp_path / "legacy" / "assistant.db"
    target = tmp_path / "user-data" / "assistant.db"
    _database(legacy, "legacy")
    _database(target, "new")

    path = resolve_database_path(legacy, target.parent)

    assert path == target
    assert _value(path) == "new"
    assert _value(legacy) == "legacy"


def test_legacy_database_is_copied_and_preserved(tmp_path):
    legacy = tmp_path / "legacy" / "assistant.db"
    data_dir = tmp_path / "user-data"
    _database(legacy, "legacy-value")

    path = resolve_database_path(legacy, data_dir)

    assert path == data_dir / "assistant.db"
    assert _value(path) == "legacy-value"
    assert _value(legacy) == "legacy-value"
    marker = json.loads((data_dir / MIGRATION_MARKER).read_text("utf-8"))
    assert marker["migrated_from"] == str(legacy.resolve())
    assert marker["migrated_to"] == str(path.resolve())
    assert marker["legacy_preserved"] is True


def test_environment_override_selects_data_directory(tmp_path, monkeypatch):
    override = tmp_path / "custom-data"
    monkeypatch.setenv(DATA_DIR_ENV, str(override))

    assert user_data_dir() == override.resolve()


def test_failed_migration_keeps_using_legacy_database(tmp_path, monkeypatch):
    legacy = tmp_path / "legacy" / "assistant.db"
    data_dir = tmp_path / "user-data"
    _database(legacy, "still-available")

    def fail_copy(_source, _target):
        raise sqlite3.OperationalError("simulated migration failure")

    monkeypatch.setattr(data_paths, "_copy_sqlite_database", fail_copy)
    path = resolve_database_path(legacy, data_dir)

    assert path == legacy
    assert _value(path) == "still-available"
    assert not (data_dir / "assistant.db").exists()


def test_application_configuration_survives_migration(tmp_path):
    legacy = tmp_path / "legacy" / "assistant.db"
    legacy_storage = Storage(legacy)
    legacy_storage.ensure_schema()
    legacy_storage.replace_model_configs([
        {
            "provider": "OpenAI",
            "model_type": "llm",
            "display_name": "primary",
            "model": "gpt-test",
        }
    ])
    legacy_storage.mark_initialized()

    migrated = resolve_database_path(legacy, tmp_path / "user-data")
    migrated_storage = Storage(migrated)

    assert migrated_storage.get_model_config("primary")["model"] == "gpt-test"
    assert migrated_storage.is_first_run() is False

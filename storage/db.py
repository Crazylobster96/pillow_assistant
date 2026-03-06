from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable, List, Optional


class Storage:
    """Lightweight SQLite wrapper for application configuration state."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        if not self.db_path.parent.exists():
            self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def ensure_schema(self) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS model_configs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    provider TEXT NOT NULL,
                    model_type TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    base_url TEXT,
                    api_key TEXT,
                    extra TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS app_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def is_first_run(self) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT value FROM app_meta WHERE key = ?", ("initialized",)
            ).fetchone()
        return row is None or row["value"] != "true"

    def mark_initialized(self) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO app_meta(key, value)
                VALUES(?, ?)
                ON CONFLICT(key)
                DO UPDATE SET value = excluded.value
                """,
                ("initialized", "true"),
            )
            conn.commit()

    def has_model_configs(self, model_type: Optional[str] = None) -> bool:
        query = "SELECT COUNT(1) AS total FROM model_configs"
        params: List[str] = []
        if model_type:
            query += " WHERE model_type = ?"
            params.append(model_type)

        with self.connect() as conn:
            row = conn.execute(query, params).fetchone()
        return bool(row and row["total"])

    def list_model_configs(self) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(
                conn.execute(
                    """
                    SELECT id, provider, model_type, display_name, base_url, api_key, extra
                    FROM model_configs
                    ORDER BY created_at DESC
                    """
                )
            )

    def replace_model_configs(self, configs: Iterable[dict]) -> None:
        records = [
            (
                cfg.get("provider", ""),
                cfg.get("model_type", "llm"),
                cfg.get("display_name", ""),
                cfg.get("base_url"),
                cfg.get("api_key"),
                cfg.get("extra"),
            )
            for cfg in configs
        ]

        with self.connect() as conn:
            conn.execute("DELETE FROM model_configs")
            conn.executemany(
                """
                INSERT INTO model_configs(
                    provider, model_type, display_name, base_url, api_key, extra
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                records,
            )
            conn.commit()


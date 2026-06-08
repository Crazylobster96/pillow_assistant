from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable, List, Optional


class Storage:
    """Lightweight SQLite wrapper for application configuration state.

    Since R0 the ``model_configs`` table no longer stores API keys. Keys live in
    the OS keychain (see :class:`storage.vault.Vault`); the DB keeps only the
    non-secret config plus a ``model`` name. The vault key for a config is its
    ``display_name``.
    """

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        if not self.db_path.parent.exists():
            self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    # -- schema -------------------------------------------------------------
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
                    model TEXT,
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
            # Older installs may have a table without the `model` column.
            if "model" not in self._columns(conn, "model_configs"):
                conn.execute("ALTER TABLE model_configs ADD COLUMN model TEXT")
            conn.commit()

    def _columns(self, conn: sqlite3.Connection, table: str) -> set[str]:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {row["name"] for row in rows}

    def migrate_plaintext_keys(self, vault) -> int:
        """Move any legacy plaintext ``api_key`` values into the vault.

        Returns the number of keys migrated. After migration the ``api_key``
        column is dropped by rebuilding the table. Idempotent: a no-op once the
        column is gone.
        """
        with self.connect() as conn:
            cols = self._columns(conn, "model_configs")
            if "api_key" not in cols:
                return 0

            rows = conn.execute(
                "SELECT id, provider, model_type, display_name, base_url, "
                "api_key, extra FROM model_configs ORDER BY created_at"
            ).fetchall()

            migrated = 0
            if vault is not None:
                for row in rows:
                    key = row["api_key"]
                    if key:
                        vault.set_secret(row["display_name"], key)
                        migrated += 1

            has_model = "model" in cols
            conn.execute("ALTER TABLE model_configs RENAME TO model_configs_old")
            conn.execute(
                """
                CREATE TABLE model_configs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    provider TEXT NOT NULL,
                    model_type TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    base_url TEXT,
                    model TEXT,
                    extra TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            model_expr = "model" if has_model else "NULL"
            conn.execute(
                f"""
                INSERT INTO model_configs
                    (id, provider, model_type, display_name, base_url, model, extra)
                SELECT id, provider, model_type, display_name, base_url, {model_expr}, extra
                FROM model_configs_old
                """
            )
            conn.execute("DROP TABLE model_configs_old")
            conn.commit()
            return migrated

    # -- first-run flag -----------------------------------------------------
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

    # -- model configs ------------------------------------------------------
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
                    SELECT id, provider, model_type, display_name, base_url, model, extra
                    FROM model_configs
                    ORDER BY created_at DESC
                    """
                )
            )

    def get_model_config(self, ref: Optional[str]) -> Optional[dict]:
        """Look up a single config by display_name (preferred) or numeric id."""
        if ref is None:
            return None
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT id, provider, model_type, display_name, base_url, model, extra
                FROM model_configs WHERE display_name = ?
                """,
                (str(ref),),
            ).fetchone()
            if row is None and str(ref).isdigit():
                row = conn.execute(
                    """
                    SELECT id, provider, model_type, display_name, base_url, model, extra
                    FROM model_configs WHERE id = ?
                    """,
                    (int(ref),),
                ).fetchone()
        return dict(row) if row is not None else None

    def replace_model_configs(self, configs: Iterable[dict], vault=None) -> None:
        """Replace all configs. Secrets (``api_key``) are routed to the vault,
        keyed by ``display_name``; the DB never stores the key."""
        configs = list(configs)
        new_names = {c.get("display_name", "") for c in configs}

        with self.connect() as conn:
            old_names = {
                r["display_name"]
                for r in conn.execute("SELECT display_name FROM model_configs")
            }

            records = [
                (
                    cfg.get("provider", ""),
                    cfg.get("model_type", "llm"),
                    cfg.get("display_name", ""),
                    cfg.get("base_url"),
                    cfg.get("model"),
                    cfg.get("extra"),
                )
                for cfg in configs
            ]
            conn.execute("DELETE FROM model_configs")
            conn.executemany(
                """
                INSERT INTO model_configs(
                    provider, model_type, display_name, base_url, model, extra
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                records,
            )
            conn.commit()

        if vault is not None:
            # Drop secrets for removed configs; (re)write secrets for current ones.
            for name in old_names - new_names:
                vault.delete_secret(name)
            for cfg in configs:
                name = cfg.get("display_name", "")
                key = cfg.get("api_key")
                if name and key:
                    vault.set_secret(name, key)

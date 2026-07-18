"""Resolve the per-user application database and migrate legacy installs.

Older releases stored ``assistant.db`` beside the installed/source package.
That location may be read-only and can disappear when an environment is
recreated.  New releases use ``~/.pillow/data`` (or ``PILLOW_DATA_DIR``) while
copying an existing legacy database on first launch.  The old database is kept
intact so the migration is non-destructive.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DATA_DIR_ENV = "PILLOW_DATA_DIR"
DATABASE_NAME = "assistant.db"
MIGRATION_MARKER = "migration.json"

log = logging.getLogger(__name__)


def user_data_dir() -> Path:
    """Return the configured per-user data directory."""
    override = os.environ.get(DATA_DIR_ENV, "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return (Path.home() / ".pillow" / "data").resolve()


def _same_path(left: Path, right: Path) -> bool:
    try:
        return left.resolve() == right.resolve()
    except OSError:
        return False


def _copy_sqlite_database(source: Path, target: Path) -> bool:
    """Atomically copy ``source`` into ``target`` using SQLite's backup API.

    Returns ``False`` when another process created the target while the backup
    was running; in that case the concurrently migrated database wins.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        with closing(sqlite3.connect(str(source))) as source_conn:
            with closing(sqlite3.connect(str(temporary))) as target_conn:
                source_conn.backup(target_conn)
                result = target_conn.execute("PRAGMA integrity_check").fetchone()
                if not result or result[0] != "ok":
                    raise sqlite3.DatabaseError("migrated database failed integrity_check")
        if target.exists():
            return False
        temporary.replace(target)
        return True
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _write_migration_marker(source: Path, target: Path) -> None:
    marker = target.parent / MIGRATION_MARKER
    payload = {
        "database": target.name,
        "migrated_from": str(source.resolve()),
        "migrated_to": str(target.resolve()),
        "migrated_at": datetime.now(timezone.utc).isoformat(),
        "legacy_preserved": True,
    }
    try:
        marker.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        # The database migration succeeded; a diagnostic marker must not make
        # the application fall back to the legacy database.
        log.warning("Could not write database migration marker at %s", marker, exc_info=True)


def resolve_database_path(legacy_path: str | Path, data_dir: Optional[str | Path] = None) -> Path:
    """Return the database path, migrating a legacy database when necessary.

    Precedence is: existing new database, migrated legacy database, then a new
    empty database at the new location.  If migration fails, the existing
    legacy database remains in use for this launch instead of presenting the
    user with an apparently fresh installation.
    """
    legacy = Path(legacy_path)
    destination_dir = Path(data_dir).expanduser().resolve() if data_dir else user_data_dir()
    target = destination_dir / DATABASE_NAME

    if _same_path(legacy, target):
        target.parent.mkdir(parents=True, exist_ok=True)
        return target
    if target.is_file():
        return target

    if not legacy.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    try:
        migrated = _copy_sqlite_database(legacy, target)
    except (OSError, sqlite3.Error):
        log.exception("Could not migrate Pillow Assistant database from %s to %s", legacy, target)
        return legacy

    if migrated:
        _write_migration_marker(legacy, target)
    return target

"""Credential vault — keeps API keys out of the application database.

Primary backend is the OS keychain via ``keyring`` (macOS Keychain / Windows
Credential Manager), satisfying NFR-8. When no OS keyring backend is available
(e.g. a headless Linux box without a secret service), it falls back to a
per-user file under ``~/.pillow/secrets.json``. The fallback is base64-obfuscated
rather than encrypted; it exists only so the app remains runnable, and is never
the application database.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Optional

SERVICE = "pillow-oss"


def _looks_usable(keyring_module) -> bool:
    """True if keyring has a real (non-fail) backend installed."""
    try:
        backend = keyring_module.get_keyring()
        from keyring.backends.fail import Keyring as FailKeyring

        return not isinstance(backend, FailKeyring)
    except Exception:
        return False


class Vault:
    def __init__(self, fallback_path: Optional[str | Path] = None) -> None:
        self._keyring = None
        try:
            import keyring  # type: ignore

            if _looks_usable(keyring):
                self._keyring = keyring
        except Exception:
            self._keyring = None

        self._fallback_path = Path(fallback_path) if fallback_path else (Path.home() / ".pillow" / "secrets.json")

    # -- backend detection --------------------------------------------------
    @property
    def uses_os_keyring(self) -> bool:
        return self._keyring is not None

    # -- public API ---------------------------------------------------------
    def set_secret(self, name: str, secret: str) -> None:
        if not name:
            return
        if self._keyring is not None:
            self._keyring.set_password(SERVICE, name, secret)
            return
        data = self._read_fallback()
        data[name] = base64.b64encode(secret.encode("utf-8")).decode("ascii")
        self._write_fallback(data)

    def get_secret(self, name: str) -> Optional[str]:
        if not name:
            return None
        if self._keyring is not None:
            return self._keyring.get_password(SERVICE, name)
        data = self._read_fallback()
        raw = data.get(name)
        if raw is None:
            return None
        try:
            return base64.b64decode(raw.encode("ascii")).decode("utf-8")
        except Exception:
            return None

    def delete_secret(self, name: str) -> None:
        if not name:
            return
        if self._keyring is not None:
            try:
                self._keyring.delete_password(SERVICE, name)
            except Exception:
                pass
            return
        data = self._read_fallback()
        if name in data:
            del data[name]
            self._write_fallback(data)

    # -- fallback file ------------------------------------------------------
    def _read_fallback(self) -> dict[str, str]:
        try:
            return json.loads(self._fallback_path.read_text("utf-8"))
        except (FileNotFoundError, ValueError):
            return {}

    def _write_fallback(self, data: dict[str, str]) -> None:
        self._fallback_path.parent.mkdir(parents=True, exist_ok=True)
        self._fallback_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
        try:
            self._fallback_path.chmod(0o600)
        except OSError:
            pass

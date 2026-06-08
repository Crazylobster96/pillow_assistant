"""Current session state held by the UI.

In R0.5 a Session is a lightweight, in-memory holder of *referenced* files and
folders. References are stored as absolute paths only — files are never copied.
They persist for the life of the session and are cleared when the session ends,
when the bound project is deleted, or when the user removes them manually. The
``project_id`` hook is where R1's project subsystem will bind.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional


class Session:
    def __init__(self, project_id: Optional[str] = None) -> None:
        self.project_id = project_id
        self.session_id: Optional[str] = None  # current conversation within the project
        self._references: list[str] = []

    @property
    def references(self) -> list[str]:
        """A copy of the current reference paths (absolute)."""
        return list(self._references)

    def add_reference(self, path: str | Path) -> bool:
        """Add a path by reference (not copied). Returns True if newly added."""
        p = str(Path(path).expanduser())
        if p not in self._references:
            self._references.append(p)
            return True
        return False

    def remove_reference(self, path: str | Path) -> None:
        p = str(Path(path).expanduser())
        if p in self._references:
            self._references.remove(p)

    def clear(self) -> None:
        self._references.clear()

    def __len__(self) -> int:
        return len(self._references)

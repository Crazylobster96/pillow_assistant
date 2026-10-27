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
        # Files on screen in preview-only windows (the agent's present_windows
        # tiles). They go along with the next question without becoming chips.
        self._displayed: list[str] = []
        # Chips already sent with an earlier question. When the next question
        # lands in another project they belong to the old work, not this one.
        self._sent: set[str] = set()
        # Tools the user allowed for the rest of a conversation, per
        # (project_id, session_id): "allow in this conversation" on the prompt.
        self._allowed_tools: dict[tuple, set[str]] = {}

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
        self._sent.discard(p)

    def take_new(self, refs) -> list[str]:
        """Of ``refs`` (about to be sent), those not sent before; then mark all as sent."""
        new = [p for p in refs if p not in self._sent]
        self._sent.update(refs)
        return new

    def clear(self) -> None:
        self._references.clear()
        self._sent.clear()

    def __bool__(self) -> bool:
        # A session is there even with no chips. Without this, __len__ made an empty
        # one falsy, and `x if session else None` checks lost its project binding.
        return True
    # -- files shown in preview-only windows --------------------------------
    @property
    def displayed(self) -> list[str]:
        return list(self._displayed)

    def set_displayed(self, paths) -> None:
        self._displayed = [str(Path(p).expanduser()) for p in paths if p]

    def clear_displayed(self) -> None:
        self._displayed = []

    def remove_displayed(self, path: str | Path) -> None:
        p = str(Path(path).expanduser())
        if p in self._displayed:
            self._displayed.remove(p)

    def request_references(self) -> list[str]:
        """What a question asked now should carry: the chips, then what is on screen."""
        out = list(self._references)
        out += [p for p in self._displayed if p not in out]
        return out

    # -- per-conversation tool permissions -----------------------------------
    def _conversation(self) -> tuple:
        return (self.project_id, self.session_id)

    def allow_tool(self, name: str) -> None:
        self._allowed_tools.setdefault(self._conversation(), set()).add(name)

    def tool_allowed(self, name: str) -> bool:
        return name in self._allowed_tools.get(self._conversation(), set())

    def __len__(self) -> int:
        return len(self._references)

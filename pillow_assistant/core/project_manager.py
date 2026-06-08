"""Resolve which project a request belongs to (R1).

R1 uses a simple, predictable rule that matches the user's mental model
("当次会话即一个项目"): the session is bound to one project. The first request
of a session creates a project (named from the prompt); subsequent requests
continue it. Clearing the session starts a fresh project next time.

Semantic re-classification into *other* existing projects (FR-11 full) is a
later enhancement; the seam is ``resolve()`` so it can be upgraded in place.
"""

from __future__ import annotations

import re
from typing import Optional

from storage.projects import Project, ProjectStore


def derive_name(prompt: str, limit: int = 18) -> str:
    """A short, human-ish project name from the first line of the prompt."""
    text = (prompt or "").strip().splitlines()[0] if prompt.strip() else ""
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        from pillow_assistant.core.i18n import t
        return t("core.unnamed_task")
    return text if len(text) <= limit else text[:limit] + "…"


class ProjectManager:
    def __init__(self, store: ProjectStore, session) -> None:
        self.store = store
        self.session = session

    def resolve(self, prompt: str) -> Project:
        # NB: use `is not None` — Session defines __len__, so an empty session is
        # falsy and `if self.session` would wrongly create a project every turn.
        current_id = getattr(self.session, "project_id", None) if self.session is not None else None
        project = self.store.get(current_id) if current_id else None
        if project is None:
            project = self.store.create(derive_name(prompt))
            if self.session is not None:
                self.session.project_id = project.id
        else:
            self.store.touch(project)
        return project

    def apply(self, triage_result, prompt: str) -> Project:
        """Resolve a project + session from a triage decision (continue an
        existing project or create a new one), binding both to the session.

        A *new* project starts a new session; *continuing* keeps the current
        session if already bound to that project, else starts a new session in it
        (a project can hold multiple sessions).
        """
        s = self.session
        if getattr(triage_result, "action", "new") == "continue" and triage_result.project_id:
            project = self.store.get(triage_result.project_id)
            if project is not None:
                self.store.touch(project)
                if s is not None:
                    if s.project_id != project.id or not getattr(s, "session_id", None):
                        s.session_id = self.store.new_session_id()
                    s.project_id = project.id
                return project
        name = getattr(triage_result, "name", None) or derive_name(prompt)
        project = self.store.create(name)
        if s is not None:
            s.project_id = project.id
            s.session_id = self.store.new_session_id()
        return project

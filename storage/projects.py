"""Project store (R1++): a project groups a shared workspace and *multiple
conversation sessions*.

Layout per project::

    <base>/<id>/
        project.json          # id, name, created_at, updated_at, last_prompt
        workspace/            # sandbox cwd shared by the project; artifacts land here
        sessions/<sid>.jsonl  # one conversation session's history per file

``index()`` (id/name/last_prompt) lets triage associate a request with a project.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

HISTORY_MAX_TURNS = 12
HISTORY_MAX_CHARS = 6000


@dataclass
class Project:
    id: str
    name: str
    root: Path
    created_at: float
    updated_at: float
    last_prompt: str = ""
    unfinished: bool = False   # last run hit the step limit (work left to resume)

    @property
    def workspace(self) -> Path:
        return self.root / "workspace"

    @property
    def sessions_dir(self) -> Path:
        return self.root / "sessions"


class ProjectStore:
    def __init__(self, base: str | Path) -> None:
        self.base = Path(base)
        self.base.mkdir(parents=True, exist_ok=True)

    def _meta_path(self, root: Path) -> Path:
        return root / "project.json"

    def new_session_id(self) -> str:
        return time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]

    def create(self, name: str) -> Project:
        pid = time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
        root = self.base / pid
        (root / "workspace").mkdir(parents=True, exist_ok=True)
        (root / "sessions").mkdir(parents=True, exist_ok=True)
        now = time.time()
        project = Project(id=pid, name=name or pid, root=root, created_at=now, updated_at=now)
        self._write(project)
        return project

    def _write(self, project: Project) -> None:
        self._meta_path(project.root).write_text(
            json.dumps(
                {"id": project.id, "name": project.name,
                 "created_at": project.created_at, "updated_at": project.updated_at,
                 "last_prompt": project.last_prompt, "unfinished": project.unfinished},
                ensure_ascii=False, indent=2,
            ),
            encoding="utf-8",
        )

    def _load(self, root: Path) -> Optional[Project]:
        meta = self._meta_path(root)
        if not meta.exists():
            return None
        try:
            data = json.loads(meta.read_text("utf-8"))
        except (ValueError, OSError):
            return None
        return Project(
            id=data["id"], name=data.get("name", data["id"]), root=root,
            created_at=data.get("created_at", 0.0), updated_at=data.get("updated_at", 0.0),
            last_prompt=data.get("last_prompt", ""), unfinished=bool(data.get("unfinished", False)),
        )

    def get(self, project_id: Optional[str]) -> Optional[Project]:
        if not project_id:
            return None
        root = self.base / project_id
        return self._load(root) if root.is_dir() else None

    def list(self) -> list[Project]:
        projects = [p for d in self.base.iterdir() if d.is_dir() and (p := self._load(d))]
        return sorted(projects, key=lambda p: p.updated_at, reverse=True)

    def touch(self, project: Project) -> None:
        project.updated_at = time.time()
        self._write(project)

    def set_unfinished(self, project: Project, value: bool) -> None:
        """Mark whether the project has work left to resume (hit step limit)."""
        if project.unfinished == value:
            return
        project.unfinished = value
        self._write(project)

    def rename(self, project_id: str, name: str) -> Optional[Project]:
        project = self.get(project_id)
        if project is None:
            return None
        project.name = name
        self._write(project)
        return project

    def delete(self, project_id: str) -> bool:
        """Permanently remove a project directory (metadata + workspace +
        sessions). Returns True if it existed and was removed."""
        import shutil

        if not project_id:
            return False
        root = self.base / project_id
        if not root.is_dir() or self._meta_path(root).exists() is False:
            return False
        try:
            shutil.rmtree(root)
            return True
        except OSError:
            return False

    def find_by_name(self, name: str) -> list[Project]:
        """Projects whose name matches ``name`` (exact first, else substring)."""
        name = (name or "").strip()
        if not name:
            return []
        projects = self.list()
        exact = [p for p in projects if p.name == name]
        if exact:
            return exact
        low = name.lower()
        return [p for p in projects if low in p.name.lower()]

    def index(self) -> list[dict]:
        return [
            {"id": p.id, "name": p.name, "last_prompt": p.last_prompt, "updated_at": p.updated_at}
            for p in self.list()
        ]

    # -- sessions + history -------------------------------------------------
    def _session_path(self, project: Project, session_id: str) -> Path:
        return project.sessions_dir / f"{session_id}.jsonl"

    def list_sessions(self, project: Project) -> list[dict]:
        """Sessions in a project, newest first: {'id','mtime','first_prompt'}."""
        out: list[dict] = []
        if not project.sessions_dir.is_dir():
            return out
        for f in project.sessions_dir.glob("*.jsonl"):
            first = ""
            try:
                for line in f.read_text("utf-8").splitlines():
                    obj = json.loads(line)
                    if obj.get("role") == "user":
                        first = str(obj.get("content", ""))[:60]
                        break
            except (ValueError, OSError):
                pass
            out.append({"id": f.stem, "mtime": f.stat().st_mtime, "first_prompt": first})
        return sorted(out, key=lambda s: s["mtime"], reverse=True)

    def append_history(self, project: Project, session_id: str, role: str, content: str) -> None:
        project.sessions_dir.mkdir(parents=True, exist_ok=True)
        with self._session_path(project, session_id).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"role": role, "content": content}, ensure_ascii=False) + "\n")

    def record_turn(self, project: Project, session_id: str, user_prompt: str, answer: str) -> None:
        self.append_history(project, session_id, "user", user_prompt)
        self.append_history(project, session_id, "assistant", answer)
        project.last_prompt = user_prompt[:200]
        self.touch(project)

    def _read_session(self, path: Path) -> list[dict]:
        turns: list[dict] = []
        try:
            for line in path.read_text("utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                if isinstance(obj, dict) and obj.get("role") and obj.get("content") is not None:
                    turns.append({"role": obj["role"], "content": str(obj["content"])})
        except (ValueError, OSError):
            return []
        return turns

    def load_history(self, project: Project, session_id: Optional[str] = None,
                     max_turns: int = HISTORY_MAX_TURNS) -> list[dict]:
        """Turns for one session, or (session_id=None) all sessions concatenated."""
        if session_id is not None:
            path = self._session_path(project, session_id)
            turns = self._read_session(path) if path.exists() else []
        else:
            turns = []
            legacy = project.root / "history.jsonl"  # pre-multi-session format
            if legacy.exists():
                turns.extend(self._read_session(legacy))
            if project.sessions_dir.is_dir():
                for f in sorted(project.sessions_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime):
                    turns.extend(self._read_session(f))
        turns = turns[-max_turns:]
        while turns and sum(len(t["content"]) for t in turns) > HISTORY_MAX_CHARS:
            turns.pop(0)
        return turns

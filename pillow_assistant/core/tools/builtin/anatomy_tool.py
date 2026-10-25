"""Anatomy tools: build and check isometric explainer figures, and read the vendored Skill's files.

``anatomy`` runs ``node <script> …`` in ``<workspace>/anatomy/`` (see
``capabilities/anatomy.py``), where the vendored kit and check scripts sit in
the upstream layout. Build scripts are arbitrary host code, so like run_cli it
is SYSTEM permission and confirmed per call; it never goes through a shell.
``action="open"`` shows a built page in the system browser instead.

``anatomy_reference`` is read-only: it serves the Skill's references, kit and
example sources, which file_read cannot reach, and reports the runtime status.
"""

from __future__ import annotations

import asyncio
import subprocess
import webbrowser
from pathlib import Path

from pillow_assistant.capabilities import anatomy
from pillow_assistant.capabilities.tool_manifest import manifest_tool
from pillow_assistant.core.i18n import t
from pillow_assistant.core.tools.base import Permission, ToolContext, ToolResult
from pillow_assistant.core.tools.builtin.cli_tool import MAX_OUTPUT, _safe_environment, _split_command
from pillow_assistant.core.tools.builtin.file_tool import MAX_READ, _under

RUN_TIMEOUT = 1800  # a dense figure's build or the 3D orbit audit takes many minutes
OUTPUT_EXT = {".html", ".svg", ".png", ".pdf", ".webp", ".gif", ".mp4", ".css", ".mjs", ".ts", ".tsx"}
MAX_SCAN = 5000


def _outputs(work: Path) -> dict[str, float]:
    """Figure files under the Anatomy workdir and their mtimes (bounded scan, kit/scripts skipped)."""
    found: dict[str, float] = {}
    try:
        for i, p in enumerate(work.rglob("*")):
            if i >= MAX_SCAN:
                break
            rel = p.relative_to(work).as_posix()
            if rel.split("/", 1)[0] in anatomy.COPIED or p.suffix.lower() not in OUTPUT_EXT:
                continue
            if p.is_file():
                found[rel] = p.stat().st_mtime
    except OSError:
        pass
    return found


@manifest_tool
class AnatomyTool:
    name = "anatomy"
    permission = Permission.SYSTEM

    async def __call__(self, args: dict, ctx: ToolContext) -> ToolResult:
        if not getattr(ctx, "allow_cli", True):
            return ToolResult(ok=False, text=t("tool.cli.disabled"))
        ws = Path(ctx.workspace)
        loop = asyncio.get_event_loop()
        work = await loop.run_in_executor(None, anatomy.prepare_workdir, ws)
        action = str(args.get("action") or "run").strip().lower()
        if action == "open":
            return self._open(work, str(args.get("path") or "").strip())
        if action != "run":
            return ToolResult(ok=False, text=t("tool.anatomy.bad_action"))

        argv = args.get("args") or []
        if isinstance(argv, str):
            try:
                argv = _split_command(argv)
            except ValueError as exc:
                return ToolResult(ok=False, text=t("tool.cli.parse", err=exc))
        if not isinstance(argv, list) or not argv or not all(isinstance(a, (str, int, float)) for a in argv):
            return ToolResult(ok=False, text=t("tool.anatomy.bad_args"))
        argv = [str(a) for a in argv]
        if any("\n" in a or "\r" in a for a in argv):
            return ToolResult(ok=False, text=t("tool.anatomy.bad_args"))
        if argv[0] in ("node", "node.exe"):
            argv = argv[1:]
        if not argv or argv[0].startswith("-"):
            return ToolResult(ok=False, text=t("tool.anatomy.bad_args"))  # no `node -e …`: run files only
        script = (work / argv[0]).resolve()
        if not _under(work, script) or not script.is_file():
            return ToolResult(ok=False, text=t("tool.anatomy.no_script", path=argv[0]))

        node = await loop.run_in_executor(None, anatomy.find_node)
        notes = []
        if node is None:
            try:
                node = await loop.run_in_executor(None, anatomy.install_node)
                notes.append(t("tool.anatomy.node_installed", version=anatomy.NODE_VERSION, path=node))
            except Exception as exc:  # noqa: BLE001 - offline, unsupported platform
                return ToolResult(ok=False, text=t("tool.anatomy.no_node", err=exc))
        env = _safe_environment()
        chrome = anatomy.find_chrome()
        if chrome is not None:
            env["CHROME_PATH"] = str(chrome)
        before = _outputs(work)

        def do():
            try:
                p = subprocess.run([str(node)] + argv, shell=False, cwd=str(work), env=env, capture_output=True,
                                   text=True, encoding="utf-8", errors="replace", timeout=RUN_TIMEOUT)
                return p.returncode, p.stdout, p.stderr
            except subprocess.TimeoutExpired:
                return -1, "", t("tool.cli.timeout", n=RUN_TIMEOUT)
            except Exception as exc:  # noqa: BLE001
                return -1, "", str(exc)

        rc, out, err = await loop.run_in_executor(None, do)
        files = sorted(k for k, v in _outputs(work).items() if before.get(k) != v)
        parts = notes + [f"returncode={rc}"]
        if out:
            parts.append("STDOUT:\n" + out[-MAX_OUTPUT:])
        if err:
            parts.append("STDERR:\n" + err[-MAX_OUTPUT:])
            if "No Chrome found" in err:
                parts.append(t("tool.anatomy.no_chrome"))
        if files:
            parts.append(t("tool.anatomy.outputs", root=work, files=", ".join(files[:50])))
        rel = work.relative_to(ws).as_posix()
        return ToolResult(ok=(rc == 0), text="\n".join(parts), artifacts=[f"{rel}/{f}" for f in files])

    @staticmethod
    def _open(work: Path, raw: str) -> ToolResult:
        path = (work / raw).resolve()
        if not raw or not _under(work, path) or not path.is_file() or path.suffix.lower() not in {".html", ".svg",
                                                                                                   ".pdf"}:
            return ToolResult(ok=False, text=t("tool.anatomy.no_page", path=raw))
        try:
            webbrowser.open(path.as_uri())
        except Exception as exc:  # noqa: BLE001
            return ToolResult(ok=False, text=t("tool.anatomy.open_failed", err=exc))
        return ToolResult(ok=True, text=t("tool.anatomy.opened", path=path))


@manifest_tool
class AnatomyReferenceTool:
    name = "anatomy_reference"
    permission = Permission.READONLY

    async def __call__(self, args: dict, ctx: ToolContext) -> ToolResult:
        root = anatomy.skill_dir()
        raw = str(args.get("path") or "").strip().replace("\\", "/").lstrip("/")
        if raw.startswith(anatomy.SKILL + "/"):
            raw = raw[len(anatomy.SKILL) + 1:]
        if not raw:
            return ToolResult(ok=True, text=self.status())
        target = root / raw
        if not _under(root, target):
            return ToolResult(ok=False, text=t("tool.anatomyref.denied", path=raw))
        if target.is_dir():
            names = sorted(p.relative_to(root).as_posix() + ("/" if p.is_dir() else "") for p in target.iterdir())
            return ToolResult(ok=True, text="\n".join(names))
        if not target.is_file():
            return ToolResult(ok=False, text=t("tool.anatomyref.not_found", path=raw))
        text = target.read_text("utf-8", errors="replace")
        if len(text) > MAX_READ:
            text = text[:MAX_READ] + t("tool.truncated")
        return ToolResult(ok=True, text=text)

    @staticmethod
    def status() -> str:
        info = anatomy.upstream_info()
        node = anatomy.find_node()
        version = anatomy.node_version(node) if node else None
        chrome = anatomy.find_chrome()
        return t("tool.anatomyref.status", version=info.get("version", "?"), repo=info.get("repo", "?"),
                 commit=str(info.get("commit", ""))[:12] or "?",
                 node=f"{node} (v{'.'.join(map(str, version))})" if node and version
                 else t("tool.anatomyref.node_missing", version=anatomy.NODE_VERSION),
                 chrome=chrome or t("tool.anatomyref.chrome_missing"), root=anatomy.skill_dir())

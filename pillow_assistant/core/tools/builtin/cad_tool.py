"""text-to-cad tools: run the pinned cadgen runtime, and read the vendored CAD Skills' files.

``text_to_cad`` executes ``cadgen …`` or ``python <model>.py`` inside the cadgen
environment that the vendored Skills pin (see ``capabilities/text_to_cad.py``).
Model scripts are arbitrary host code, so like run_cli it is SYSTEM permission
and confirmed per call; it never goes through a shell.

``cad_reference`` is read-only: it serves files from the vendored Skills folder
(references, scripts) that file_read cannot reach, and reports the runtime status.

``cad_app`` finds, installs on first use, runs, or opens the desktop apps that
the gcode / bambu-labs Skills hand off to (see ``capabilities/cad_apps.py``).
"""

from __future__ import annotations

from pillow_assistant.capabilities.tool_manifest import manifest_tool

import asyncio
import os
import subprocess
import sys
from pathlib import Path

from pillow_assistant.capabilities import text_to_cad
from pillow_assistant.core.i18n import t
from pillow_assistant.core.tools.base import Permission, ToolContext, ToolResult
from pillow_assistant.core.tools.builtin.cli_tool import MAX_OUTPUT, _safe_environment, _split_command
from pillow_assistant.core.tools.builtin.file_tool import MAX_READ, _is_reference, _under

RUN_TIMEOUT = 900  # first run downloads cadgen + a managed Python; big assemblies build slowly
OUTPUT_EXT = {".step", ".stp", ".stl", ".3mf", ".glb", ".gltf", ".dxf", ".svg", ".pdf", ".png",
              ".urdf", ".srdf", ".sdf", ".gcode", ".mp4", ".gif"}
MAX_SCAN = 5000


def _outputs(ws: Path) -> dict[str, float]:
    """CAD-ish files in the workspace and their mtimes (bounded scan)."""
    found: dict[str, float] = {}
    try:
        for i, p in enumerate(ws.rglob("*")):
            if i >= MAX_SCAN:
                break
            if p.suffix.lower() in OUTPUT_EXT and p.is_file():
                found[p.relative_to(ws).as_posix()] = p.stat().st_mtime
    except OSError:
        pass
    return found


def _skill_dir(name: str) -> Path | None:
    root = text_to_cad.skills_root()
    target = root / name
    return target if name and _under(root, target) and (target / "SKILL.md").is_file() else None


@manifest_tool
class TextToCadTool:
    name = "text_to_cad"
    permission = Permission.SYSTEM

    async def __call__(self, args: dict, ctx: ToolContext) -> ToolResult:
        if not getattr(ctx, "allow_cli", True):
            return ToolResult(ok=False, text=t("tool.cli.disabled"))
        program = str(args.get("program") or "cadgen").strip().lower()
        if program not in {"cadgen", "python"}:
            return ToolResult(ok=False, text=t("tool.cad.bad_program"))
        argv = args.get("args") or []
        if isinstance(argv, str):
            try:
                argv = _split_command(argv)
            except ValueError as exc:
                return ToolResult(ok=False, text=t("tool.cli.parse", err=exc))
        if not isinstance(argv, list) or not all(isinstance(a, (str, int, float)) for a in argv):
            return ToolResult(ok=False, text=t("tool.cad.bad_args"))
        argv = [str(a) for a in argv]
        if any("\n" in a or "\r" in a for a in argv):
            return ToolResult(ok=False, text=t("tool.cad.bad_args"))

        requirements = None
        skill = str(args.get("skill") or "").strip()
        if skill:
            skill_dir = _skill_dir(skill)
            if skill_dir is None:
                return ToolResult(ok=False, text=t("tool.cad.unknown_skill", name=skill))
            # Skill-relative helper paths (scripts/x.py) resolve inside the skill folder.
            argv = [str(skill_dir / a) if not Path(a).is_absolute() and (skill_dir / a).is_file()
                    and not (Path(ctx.workspace) / a).exists() else a for a in argv]
            req = skill_dir / "requirements.txt"
            requirements = req if req.is_file() else None

        if text_to_cad.find_uv() is None:
            return ToolResult(ok=False, text=t("tool.cad.no_uv"))
        env = _safe_environment()
        env.setdefault("DO_NOT_TRACK", "1")  # cadgen telemetry is opt-in here, like the rest of Pillow
        env.setdefault("CADGEN_INSTALL_CHANNEL", "pillow-assistant")
        ws = Path(ctx.workspace)
        before = _outputs(ws)

        def run(cmd):
            try:
                p = subprocess.run(cmd, shell=False, cwd=str(ws), env=env, capture_output=True,
                                   text=True, encoding="utf-8", errors="replace", timeout=RUN_TIMEOUT)
                return p.returncode, p.stdout, p.stderr
            except subprocess.TimeoutExpired:
                return -1, "", t("tool.cli.timeout", n=RUN_TIMEOUT)
            except Exception as exc:  # noqa: BLE001
                return -1, "", str(exc)

        def do():
            # Cache first, so a prewarmed runtime works offline; download only on a miss.
            rc, out, err = run(text_to_cad.cadgen_argv(program, argv, with_requirements=requirements, offline=True))
            if text_to_cad.needs_download(rc, err):
                rc, out, err = run(text_to_cad.cadgen_argv(program, argv, with_requirements=requirements))
            return rc, out, err

        loop = asyncio.get_event_loop()
        rc, out, err = await loop.run_in_executor(None, do)
        files = sorted(k for k, v in _outputs(ws).items() if before.get(k) != v)
        parts = [f"returncode={rc}"]
        if out:
            parts.append("STDOUT:\n" + out[-MAX_OUTPUT:])
        if err:
            parts.append("STDERR:\n" + err[-MAX_OUTPUT:])
        if files:
            parts.append(t("tool.cad.outputs", files=", ".join(files[:50])))
        return ToolResult(ok=(rc == 0), text="\n".join(parts), artifacts=files)


@manifest_tool
class CadReferenceTool:
    name = "cad_reference"
    permission = Permission.READONLY

    async def __call__(self, args: dict, ctx: ToolContext) -> ToolResult:
        root = text_to_cad.skills_root()
        raw = str(args.get("path") or "").strip().replace("\\", "/").lstrip("/")
        if not raw:
            return ToolResult(ok=True, text=self.status(root))
        target = root / raw
        if not _under(root, target):
            return ToolResult(ok=False, text=t("tool.cadref.denied", path=raw))
        if target.is_dir():
            names = sorted(p.relative_to(root).as_posix() + ("/" if p.is_dir() else "")
                           for p in target.iterdir())
            return ToolResult(ok=True, text="\n".join(names))
        if not target.is_file():
            return ToolResult(ok=False, text=t("tool.cadref.not_found", path=raw))
        text = target.read_text("utf-8", errors="replace")
        if len(text) > MAX_READ:
            text = text[:MAX_READ] + t("tool.truncated")
        return ToolResult(ok=True, text=text)

    @staticmethod
    def status(root: Path) -> str:
        from pillow_assistant.capabilities.skill_loader import SkillStore

        info = text_to_cad.upstream_info(root)
        uv = text_to_cad.find_uv()
        skills = "\n".join(f"- {s.name}: {s.description}" for s in SkillStore(root).load()) or "-"
        from pillow_assistant.capabilities import cad_apps

        apps = "\n".join(f"- {spec.title} ({key}): {cad_apps.locate(key) or t('tool.cadapp.not_installed')}"
                         for key, spec in cad_apps.APPS.items())
        return t("tool.cadref.status", version=info.get("version", "?"), cadgen=info.get("cadgen_version", "?"),
                 repo=info.get("repo", "?"), commit=str(info.get("commit", ""))[:12] or "?",
                 uv=" ".join(uv) if uv else t("tool.cad.no_uv"), root=root, skills=skills) + "\n" + \
            t("tool.cadapp.status", apps=apps)


@manifest_tool
class CadAppTool:
    """Locate / install-on-first-use / run / open the desktop apps the gcode and bambu-labs Skills use."""

    name = "cad_app"
    permission = Permission.SYSTEM

    async def __call__(self, args: dict, ctx: ToolContext) -> ToolResult:
        from pillow_assistant.capabilities import cad_apps

        if not getattr(ctx, "allow_cli", True):
            return ToolResult(ok=False, text=t("tool.cli.disabled"))
        key = str(args.get("app") or "").strip().lower()
        if key not in cad_apps.APPS:
            return ToolResult(ok=False, text=t("tool.cadapp.unknown", name=key, apps=", ".join(cad_apps.APPS)))
        action = str(args.get("action") or "ensure").strip().lower()
        spec = cad_apps.APPS[key]
        loop = asyncio.get_event_loop()

        if action == "ensure":
            app = cad_apps.locate(key)
            if app is None:
                try:
                    result = await loop.run_in_executor(None, cad_apps.install, key)
                except Exception as exc:  # noqa: BLE001 - offline, rate limit, unsupported platform
                    return ToolResult(ok=False, text=t("tool.cadapp.install_failed", app=spec.title, err=exc,
                                                       page=spec.download_page))
                if "opened" in result:
                    return ToolResult(ok=False, text=t("tool.cadapp.manual", app=spec.title, page=result["opened"]))
                app = Path(result["path"])
            return ToolResult(ok=True, text=self._located(key, app))

        app = cad_apps.locate(key)
        if action == "run":
            if app is None:
                return ToolResult(ok=False, text=t("tool.cadapp.missing", app=spec.title))
            argv = args.get("args") or []
            if isinstance(argv, str):
                try:
                    argv = _split_command(argv)
                except ValueError as exc:
                    return ToolResult(ok=False, text=t("tool.cli.parse", err=exc))
            if not isinstance(argv, list) or any("\n" in str(a) or "\r" in str(a) for a in argv):
                return ToolResult(ok=False, text=t("tool.cad.bad_args"))
            ws = Path(ctx.workspace)
            before = _outputs(ws)
            cmd = cad_apps.command_for(app) + [str(a) for a in argv]

            def do():
                try:
                    p = subprocess.run(cmd, shell=False, cwd=str(ws), env=_safe_environment(), capture_output=True,
                                       text=True, encoding="utf-8", errors="replace", timeout=RUN_TIMEOUT)
                    return p.returncode, p.stdout, p.stderr
                except subprocess.TimeoutExpired:
                    return -1, "", t("tool.cli.timeout", n=RUN_TIMEOUT)
                except Exception as exc:  # noqa: BLE001
                    return -1, "", str(exc)

            rc, out, err = await loop.run_in_executor(None, do)
            files = sorted(k for k, v in _outputs(ws).items() if before.get(k) != v)
            parts = [f"returncode={rc}"]
            if out:
                parts.append("STDOUT:\n" + out[-MAX_OUTPUT:])
            if err:
                parts.append("STDERR:\n" + err[-MAX_OUTPUT:])
            if files:
                parts.append(t("tool.cad.outputs", files=", ".join(files[:50])))
            return ToolResult(ok=(rc == 0), text="\n".join(parts), artifacts=files)

        if action == "open":
            target = str(args.get("target") or "").strip()
            if target.startswith("bambu-connect://"):
                if "\n" in target or "\r" in target:
                    return ToolResult(ok=False, text=t("tool.cad.bad_args"))
                cmd = None  # handled by the OS URL handler Bambu Connect registers
            else:
                ws = Path(ctx.workspace)
                path = (ws / target) if not Path(target).is_absolute() else Path(target)
                if not target or not (_under(ws, path) or _is_reference(ctx, path)) or not path.is_file():
                    return ToolResult(ok=False, text=t("tool.fr.denied", path=target))
                if app is None:
                    return ToolResult(ok=False, text=t("tool.cadapp.missing", app=spec.title))
                cmd = cad_apps.command_for(app) + [str(path.resolve())]
            try:
                _launch(cmd, target)
            except OSError as exc:
                return ToolResult(ok=False, text=t("tool.cadapp.open_failed", app=spec.title, err=exc))
            return ToolResult(ok=True, text=t("tool.cadapp.opened", app=spec.title, target=target))

        return ToolResult(ok=False, text=t("tool.cadapp.bad_action"))

    @staticmethod
    def _located(key: str, app: Path) -> str:
        from pillow_assistant.capabilities import cad_apps

        lines = [t("tool.cadapp.ready", app=cad_apps.APPS[key].title, path=app,
                   version=cad_apps.installed_version(key) or "-")]
        profiles = cad_apps.profiles_dir(key, app)
        if profiles:
            lines.append(t("tool.cadapp.profiles", path=profiles))
        return "\n".join(lines)


def _launch(cmd: list[str] | None, target: str) -> None:
    """Start a GUI app (or open a URL) without waiting for it."""
    if cmd is None:
        if os.name == "nt":
            os.startfile(target)  # type: ignore[attr-defined]  # noqa: S606
            return
        cmd = ["open" if sys.platform == "darwin" else "xdg-open", target]
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=flags, close_fds=True)

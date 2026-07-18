"""run_cli tool (T1): execute one program in the workspace, with a
dangerous-command denylist, explicit permission confirmation, and a timeout.

This is NOT sandboxed like run_python: it runs a real host executable.  It does
not invoke a command shell, and direct shell launchers are rejected, so pipes,
redirects, command chaining, and shell built-ins are unavailable.  The central
tool policy asks the user before every call; ToolContext.allow_cli can disable
the tool entirely.
"""

from __future__ import annotations

import asyncio
import os
import re
import shlex
import subprocess
from pathlib import Path

from pillow_assistant.core.i18n import t
from pillow_assistant.core.tools.base import Permission, ToolContext, ToolResult

MAX_OUTPUT = 16 * 1024
TIMEOUT = 30

DANGEROUS = [
    r"rm\s+-rf", r"\brmdir\s+/s", r"\bmkfs", r"\bdd\b", r":\(\)\s*\{", r"\bshutdown\b",
    r"\breboot\b", r"\bformat\b", r"\bdel\b\s+/[sq]", r"\bdiskpart\b", r">\s*/dev/sd",
    r"\bchmod\s+-r\s+777\s+/", r"\bchown\s+-r\b.*\s/", r"\bmv\b\s+/\s", r"\bcurl\b.*\|\s*(sh|bash)",
    r"\bwget\b.*\|\s*(sh|bash)",
]

SHELL_LAUNCHERS = {
    "bash", "cmd", "cmd.exe", "dash", "fish", "ksh", "powershell", "powershell.exe",
    "pwsh", "pwsh.exe", "sh", "zsh",
}
SENSITIVE_ENV_PARTS = ("API_KEY", "APIKEY", "AUTHORIZATION", "CREDENTIAL", "PASSWORD", "SECRET", "TOKEN")


def _split_command(command: str) -> list[str]:
    """Parse one command line without enabling shell syntax."""
    if "\n" in command or "\r" in command:
        raise ValueError("line breaks are not allowed")
    args = shlex.split(command, posix=(os.name != "nt"))
    if os.name == "nt":
        # shlex's Windows mode preserves surrounding quotes. Popen expects the
        # unquoted value when argv is supplied as a list.
        args = [
            arg[1:-1] if len(arg) >= 2 and arg[0] == arg[-1] and arg[0] in {'"', "'"} else arg
            for arg in args
        ]
    if not args:
        raise ValueError("empty command")
    return args


def _safe_environment() -> dict[str, str]:
    """Keep the normal runtime environment but do not pass obvious secrets."""
    return {
        key: value
        for key, value in os.environ.items()
        if not any(part in key.upper() for part in SENSITIVE_ENV_PARTS)
    }


class RunCliTool:
    name = "run_cli"
    permission = Permission.SYSTEM
    description = t("tool.cli.desc")
    parameters = {
        "type": "object",
        "properties": {"command": {"type": "string", "description": t("tool.cli.command")}},
        "required": ["command"],
    }

    async def __call__(self, args: dict, ctx: ToolContext) -> ToolResult:
        cmd = (args.get("command") or "").strip()
        if not cmd:
            return ToolResult(ok=False, text=t("tool.cli.empty"))
        if not getattr(ctx, "allow_cli", True):
            return ToolResult(ok=False, text=t("tool.cli.disabled"))
        low = cmd.lower()
        for pat in DANGEROUS:
            if re.search(pat, low):
                return ToolResult(ok=False, text=t("tool.cli.dangerous", cmd=cmd))
        try:
            argv = _split_command(cmd)
        except ValueError as exc:
            return ToolResult(ok=False, text=t("tool.cli.parse", err=exc))
        executable = Path(argv[0]).name.lower()
        if executable in SHELL_LAUNCHERS or Path(argv[0]).suffix.lower() in {".bat", ".cmd"}:
            return ToolResult(ok=False, text=t("tool.cli.shell", name=executable))

        def do():
            try:
                p = subprocess.run(argv, shell=False, cwd=str(ctx.workspace), env=_safe_environment(),
                                   capture_output=True, text=True, timeout=TIMEOUT)
                return p.returncode, p.stdout, p.stderr
            except subprocess.TimeoutExpired:
                return -1, "", t("tool.cli.timeout", n=TIMEOUT)
            except Exception as exc:  # noqa: BLE001
                return -1, "", str(exc)

        loop = asyncio.get_event_loop()
        rc, out, err = await loop.run_in_executor(None, do)
        parts = [f"returncode={rc}"]
        if out:
            parts.append("STDOUT:\n" + out[:MAX_OUTPUT])
        if err:
            parts.append("STDERR:\n" + err[:MAX_OUTPUT])
        return ToolResult(ok=(rc == 0), text="\n".join(parts))

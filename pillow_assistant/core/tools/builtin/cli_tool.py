"""run_cli tool (T1): execute a shell command in the workspace, with a
dangerous-command denylist and a timeout.

Note: this is NOT sandboxed like run_python — it runs real commands on the host.
The denylist + workspace cwd + timeout are guardrails, not a security boundary;
a confirmation / 5-second-undo flow lands with R3, and a config toggle can
disable it via ToolContext.allow_cli.
"""

from __future__ import annotations

import asyncio
import re
import subprocess

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

        def do():
            try:
                p = subprocess.run(cmd, shell=True, cwd=str(ctx.workspace),
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

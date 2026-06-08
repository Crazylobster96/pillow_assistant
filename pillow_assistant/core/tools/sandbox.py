"""Subprocess sandbox for Agent-authored Python (R1).

Design choice (user-approved): a subprocess sandbox rather than a container —
cross-platform and dependency-free. Guarantees provided:

* **Working directory** is pinned to the project ``workspace/``; relative paths
  the code writes land there and are surfaced as artifacts.
* **Timeout**: the process is killed after ``timeout`` seconds.
* **Network**: soft default-deny — a preamble disables ``socket`` so casual
  network use fails. (Determined code could re-import; strong isolation needs
  Docker, which is an optional R4 upgrade.)
* **Resource limits** (POSIX only): address space, CPU seconds, and file size
  via ``setrlimit``. On Windows these are skipped (the timeout still applies).

It is intentionally *not* a security boundary against hostile code; it is a
guardrail that makes accidental damage unlikely and keeps work inside the
project. This matches the OSS needs doc's "零宿主权限起步" with documented limits.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

NETWORK_GUARD = (
    "import socket as _s\n"
    "def _blocked(*a, **k):\n"
    "    raise OSError('network access is disabled in the Pillow sandbox')\n"
    "_s.socket = _blocked\n"
    "_s.create_connection = _blocked\n"
)

MAX_OUTPUT = 16 * 1024  # truncate captured stdout/stderr to keep prompts bounded


@dataclass
class SandboxResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False
    artifacts: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = [f"returncode={self.returncode}" + (" (TIMEOUT)" if self.timed_out else "")]
        if self.stdout:
            parts.append("STDOUT:\n" + self.stdout)
        if self.stderr:
            parts.append("STDERR:\n" + self.stderr)
        if self.artifacts:
            parts.append("ARTIFACTS: " + ", ".join(self.artifacts))
        return "\n".join(parts)


def _limits(memory_mb: int, cpu_seconds: int):
    """Return a preexec_fn applying rlimits on POSIX, else None (Windows)."""
    if os.name != "posix":
        return None
    try:
        import resource
    except ImportError:  # pragma: no cover
        return None

    def apply() -> None:  # pragma: no cover - runs in the child process
        nbytes = memory_mb * 1024 * 1024
        for res, soft in (
            (resource.RLIMIT_AS, nbytes),
            (resource.RLIMIT_CPU, cpu_seconds),
            (resource.RLIMIT_FSIZE, 256 * 1024 * 1024),
        ):
            try:
                resource.setrlimit(res, (soft, soft))
            except (ValueError, OSError):
                pass

    return apply


class Sandbox:
    def __init__(self, workspace: str | Path) -> None:
        self.workspace = Path(workspace)
        self.workspace.mkdir(parents=True, exist_ok=True)

    def _snapshot(self) -> dict[str, float]:
        snap: dict[str, float] = {}
        for p in self.workspace.rglob("*"):
            if p.is_file():
                try:
                    snap[str(p.relative_to(self.workspace))] = p.stat().st_mtime
                except OSError:
                    pass
        return snap

    def run_python(
        self,
        code: str,
        *,
        timeout: int = 30,
        allow_network: bool = False,
        memory_mb: int = 512,
    ) -> SandboxResult:
        before = self._snapshot()

        runner = self.workspace / ".pillow_run.py"
        source = ("" if allow_network else NETWORK_GUARD) + "\n" + code
        runner.write_text(source, encoding="utf-8")

        env = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUNBUFFERED": "1",
            "MPLBACKEND": "Agg",  # headless matplotlib
            "HOME": str(self.workspace),
            "TEMP": str(self.workspace),
            "TMP": str(self.workspace),
        }
        if "SYSTEMROOT" in os.environ:  # required by Python on Windows
            env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]

        timed_out = False
        try:
            proc = subprocess.run(
                [sys.executable, "-I", str(runner)],
                cwd=str(self.workspace),
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
                preexec_fn=_limits(memory_mb, timeout),
            )
            returncode, out, err = proc.returncode, proc.stdout, proc.stderr
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            returncode = -1
            out = exc.stdout or ""
            from pillow_assistant.core.i18n import t
            err = (exc.stderr or "") + t("tool.sandbox_timeout", n=timeout)
            if isinstance(out, bytes):
                out = out.decode("utf-8", "replace")
            if isinstance(err, bytes):
                err = err.decode("utf-8", "replace")
        finally:
            try:
                runner.unlink()
            except OSError:
                pass

        after = self._snapshot()
        artifacts = sorted(
            name for name, mtime in after.items()
            if name != ".pillow_run.py" and before.get(name) != mtime
        )

        return SandboxResult(
            returncode=returncode,
            stdout=out[:MAX_OUTPUT],
            stderr=err[:MAX_OUTPUT],
            timed_out=timed_out,
            artifacts=artifacts,
        )

"""Vendored text-to-cad (https://github.com/earthtojake/text-to-cad) integration helpers.

``skills/text_to_cad/`` is a byte-identical copy of upstream's Skills, refreshed by
``scripts/sync_text_to_cad.py`` (and the scheduled ``sync-text-to-cad`` workflow).
``UPSTREAM.json`` beside them records the release and the exact ``uvx`` invocation
that pins its ``cadgen`` runtime, so Skills and runtime always move together.

The CAD kernel itself (cadgen → build123d → OpenCASCADE) is not a Pillow dependency:
it runs through ``uv`` in its own managed Python 3.13, exactly as upstream does.
"""

from __future__ import annotations

import json
import shutil
from importlib import resources
from pathlib import Path
from typing import Optional

SOURCE_KIND = "text-to-cad"
DEFAULT_UVX_ARGS = ["--no-config", "--managed-python", "--python", "3.13"]


def skills_root() -> Path:
    return Path(str(resources.files("pillow_assistant.capabilities").joinpath("skills", "text_to_cad")))


def upstream_info(root: Optional[Path] = None) -> dict:
    try:
        return json.loads((Path(root or skills_root()) / "UPSTREAM.json").read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def find_uv() -> Optional[list[str]]:
    """Command prefix that runs a tool through uv: ``uvx``, ``uv tool run``, or the pip ``uv`` wheel."""
    uvx = shutil.which("uvx")
    if uvx:
        return [uvx]
    uv = shutil.which("uv")
    if not uv:
        try:  # the `uv` PyPI wheel ships the binary inside site-packages
            from uv import find_uv_bin
            uv = find_uv_bin()
        except Exception:  # noqa: BLE001
            uv = None
    return [uv, "tool", "run"] if uv else None


def cadgen_argv(program: str, args: list[str], *, info: Optional[dict] = None,
                with_requirements: Optional[Path] = None, offline: bool = False) -> Optional[list[str]]:
    """Full argv for ``cadgen <args>`` or ``python <args>`` in the pinned cadgen environment.

    ``offline`` runs from uv's cache only: the pin is exact, so a cached runtime never
    needs the network, and a cache miss fails fast (see ``needs_download``).
    """
    prefix = find_uv()
    if prefix is None:
        return None
    info = upstream_info() if info is None else info
    uvx_args = list(info.get("uvx_args") or [])
    if not uvx_args:
        version = info.get("cadgen_version")
        uvx_args = DEFAULT_UVX_ARGS + ["--from", f"cadgen=={version}" if version else "cadgen"]
    extra = ["--with-requirements", str(with_requirements)] if with_requirements else []
    return (prefix + (["--offline"] if offline else []) + uvx_args[:-2] + extra + uvx_args[-2:]
            + [program] + list(args))


def needs_download(returncode: int, stderr: str) -> bool:
    """Whether an ``offline`` run failed only because uv has not cached the runtime yet."""
    low = (stderr or "").lower()
    return returncode != 0 and ("offline mode" in low or "network was disabled" in low
                                or "--offline" in low or "not found in the cache" in low)


def network_failed(returncode: int, stderr: str) -> bool:
    """Whether an online uv run failed to reach the package index or the Python downloads."""
    low = (stderr or "").lower()
    return returncode != 0 and any(k in low for k in (
        "failed to fetch", "error sending request", "dns error", "tcp connect error", "connection",
        "timed out", "failed to download", "request failed"))

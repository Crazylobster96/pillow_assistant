"""Vendored Anatomy (https://github.com/wheresryan22/anatomy) integration helpers.

``skills/anatomy/anatomy/`` is a copy of upstream's Skill (SKILL.md, kit/,
references/, scripts/, example sources), refreshed by ``scripts/sync_anatomy.py``
and the scheduled sync workflow. ``UPSTREAM.json`` beside it records the release.

Anatomy needs no packages: its kit and checks use only Node's built-in modules,
plus a headless Chrome for the screenshots it verifies a figure with. Pillow
finds Node (bundle, ``PILLOW_NODE``, PATH, or its own copy in ``node_dir()``),
installs a pinned Node on first use (nodejs.org, then the npmmirror mirror, each
download checked against the hashes pinned here), and reuses the Playwright
Chromium the browser tool already ships as the Chrome.

Figures are built in ``<workspace>/anatomy/``, a copy of the upstream layout:
``kit/`` and ``scripts/`` are refreshed from the vendored Skill, and each figure
lives in ``figures/<name>/build.mjs`` so it imports ``../../kit/iso-kit.mjs``
exactly as the upstream examples do.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from importlib import resources
from pathlib import Path
from typing import Callable, Optional

SOURCE_KIND = "anatomy"
SKILL = "anatomy"
WORKDIR = "anatomy"
COPIED = ("kit", "scripts")
MARKER = ".pillow-anatomy.json"

NODE_VERSION = "22.23.3"
NODE_DIST = "https://nodejs.org/dist/"
NODE_MIRRORS = ("https://npmmirror.com/mirrors/node/", "https://mirrors.huaweicloud.com/nodejs/")
# SHA-256 from https://nodejs.org/dist/v22.23.3/SHASUMS256.txt, so a mirror's copy can be trusted.
NODE_ARCHIVES = {
    "windows-x64": ("node-v22.23.3-win-x64.zip",
                    "2b0ff57b049cda1bbcea2240eec20467018713c1efe1f7360c2681859b90ed71"),
    "windows-arm64": ("node-v22.23.3-win-arm64.zip",
                      "33dad22e4cef5ee8f9fbb1b0d037fdacd0e56d12a4580f0d63f68b894deab535"),
    "linux-x64": ("node-v22.23.3-linux-x64.tar.xz",
                  "df450af89261115ef9f9e3830c3eeb2cc9213b63c720b1af623cb5dcbe2e02de"),
    "linux-arm64": ("node-v22.23.3-linux-arm64.tar.xz",
                    "a44aeb94849a299b22df10b9e622ec2f605c2183501bc40590705131de7c740f"),
    "macos-x64": ("node-v22.23.3-darwin-x64.tar.gz",
                  "8a677b0219178efd6eb0e475457c4afb452b521a92f6e67845a73bd85727f2a8"),
    "macos-arm64": ("node-v22.23.3-darwin-arm64.tar.gz",
                    "23b25245dcfb9af7262f8ff142e9e2e0af025368117329e7a7458a51e5922f53"),
}
USER_AGENT = "pillow-assistant"


class AnatomyError(RuntimeError):
    pass


def skills_root() -> Path:
    return Path(str(resources.files("pillow_assistant.capabilities").joinpath("skills", "anatomy")))


def skill_dir(root: Optional[Path] = None) -> Path:
    return Path(root or skills_root()) / SKILL


def upstream_info(root: Optional[Path] = None) -> dict:
    try:
        return json.loads((Path(root or skills_root()) / "UPSTREAM.json").read_text("utf-8"))
    except (OSError, ValueError):
        return {}


# -- Node ---------------------------------------------------------------------
def platform_key() -> str:
    machine = platform.machine().lower()
    arch = "arm64" if machine in {"arm64", "aarch64"} else "x64"
    if sys.platform.startswith("win"):
        return f"windows-{arch}"
    if sys.platform == "darwin":
        return f"macos-{arch}"
    return f"linux-{arch}"


def node_dir() -> Path:
    """Where Pillow keeps its own Node: the bundle's ``node-runtime`` or ``~/.pillow/runtimes/node``."""
    raw = os.environ.get("PILLOW_NODE_DIR", "").strip()
    return Path(raw) if raw else Path.home() / ".pillow" / "runtimes" / "node"


def _node_exe(folder: Path) -> Optional[Path]:
    for rel in ("node.exe", "bin/node", "node"):
        p = folder / rel
        if p.is_file():
            return p
    return None


def node_version(node: str | Path) -> Optional[tuple[int, ...]]:
    try:
        p = subprocess.run([str(node), "--version"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.match(r"v?(\d+)\.(\d+)\.(\d+)", p.stdout.strip())
    return tuple(int(n) for n in m.groups()) if p.returncode == 0 and m else None


def _min_node(root: Optional[Path] = None) -> tuple[int, ...]:
    raw = str(upstream_info(root).get("node_min") or "20.10")
    return tuple(int(n) for n in re.findall(r"\d+", raw)) or (20, 10)


def find_node(root: Optional[Path] = None) -> Optional[Path]:
    """A Node new enough for Anatomy: ``PILLOW_NODE``, Pillow's own copy, then PATH."""
    candidates: list[Path] = []
    if os.environ.get("PILLOW_NODE", "").strip():
        candidates.append(Path(os.environ["PILLOW_NODE"].strip()))
    own = node_dir()
    if own.is_dir():
        exe = _node_exe(own)
        if exe is None:
            for sub in sorted(own.iterdir(), reverse=True):
                exe = _node_exe(sub) if sub.is_dir() else None
                if exe:
                    break
        if exe:
            candidates.append(exe)
    found = shutil.which("node")
    if found:
        candidates.append(Path(found))
    need = _min_node(root)
    for exe in candidates:
        version = node_version(exe)
        if version and version[:len(need)] >= need:
            return exe
    return None


def node_urls(name: str) -> list[str]:
    from pillow_assistant.core import mirrors

    path = f"v{NODE_VERSION}/{name}"
    original = [NODE_DIST + path]
    via = [] if mirrors.mode() == "off" else [m + path for m in _node_mirrors()]
    return via + original if mirrors.mode() == "cn" else original + via


def _node_mirrors() -> list[str]:
    raw = os.environ.get("PILLOW_NODE_MIRRORS")
    items = NODE_MIRRORS if raw is None else [p.strip() for p in raw.split(",")]
    return [m if m.endswith("/") else m + "/" for m in items if m.startswith("https://")]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _download(url: str, dest: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    tmp = dest.with_name(dest.name + ".part")
    with urllib.request.urlopen(req, timeout=60) as resp, open(tmp, "wb") as fh:  # noqa: S310
        shutil.copyfileobj(resp, fh, 1 << 20)
    os.replace(tmp, dest)


def _extract(archive: Path, dest: Path) -> None:
    root = dest.resolve()
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            for member in zf.namelist():
                if not str((dest / member).resolve()).startswith(str(root)):
                    raise AnatomyError(f"unsafe path in archive: {member}")
            zf.extractall(dest)
        return
    with tarfile.open(archive) as tf:
        for member in tf.getmembers():
            if not str((dest / member.name).resolve()).startswith(str(root)) or member.islnk():
                raise AnatomyError(f"unsafe path in archive: {member.name}")
        if hasattr(tarfile, "data_filter"):
            tf.extractall(dest, filter="data")
        else:
            tf.extractall(dest)  # noqa: S202 - members checked above


def install_node(download: Optional[Callable[[str, Path], None]] = None,
                 plat: Optional[str] = None) -> Path:
    """Download the pinned Node into ``node_dir()`` and return its executable."""
    plat = plat or platform_key()
    if plat not in NODE_ARCHIVES:
        raise AnatomyError(f"no Node build for {plat}; install Node.js {'.'.join(map(str, _min_node()))}+")
    name, want = NODE_ARCHIVES[plat]
    download = download or _download
    target = node_dir()
    target.mkdir(parents=True, exist_ok=True)
    archive = target / name
    errors = []
    for url in node_urls(name):
        try:
            download(url, archive)
        except Exception as exc:  # noqa: BLE001 - try the next source
            errors.append(f"{url}: {exc}")
            continue
        if _sha256(archive) != want:
            archive.unlink(missing_ok=True)
            errors.append(f"{url}: SHA-256 does not match nodejs.org")
            continue
        break
    else:
        raise AnatomyError(f"cannot download {name}: " + "; ".join(errors[-3:]))
    stem = re.sub(r"\.(zip|tar\.xz|tar\.gz)$", "", name)
    staging = target / (stem + ".new")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir()
    try:
        _extract(archive, staging)
        inner = staging / stem
        final = target / stem
        shutil.rmtree(final, ignore_errors=True)
        os.replace(inner if inner.is_dir() else staging, final)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        archive.unlink(missing_ok=True)
    exe = _node_exe(final)
    if exe is None:
        raise AnatomyError(f"no node executable in {final}")
    return exe


# -- Chrome -------------------------------------------------------------------
_CHROME_NAMES = ("chrome-win/chrome.exe", "chrome-win64/chrome.exe", "chromium-win64/chrome.exe",
                 "chrome-linux/chrome", "chrome-linux64/chrome",
                 "chrome-headless-shell-win64/chrome-headless-shell.exe",
                 "chrome-headless-shell-linux64/chrome-headless-shell",
                 "chrome-headless-shell-mac-arm64/chrome-headless-shell",
                 "chrome-headless-shell-mac-x64/chrome-headless-shell",
                 "chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
                 "chrome-mac/Chromium.app/Contents/MacOS/Chromium")


def _playwright_dirs() -> list[Path]:
    dirs = []
    if os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip() not in ("", "0"):
        dirs.append(Path(os.environ["PLAYWRIGHT_BROWSERS_PATH"]))
    home = Path.home()
    dirs += [home / "AppData/Local/ms-playwright", home / ".cache/ms-playwright", home / "Library/Caches/ms-playwright"]
    return dirs


def find_chrome() -> Optional[Path]:
    """``CHROME_PATH``, else a Playwright Chromium (full or headless shell). None lets upstream search."""
    raw = os.environ.get("CHROME_PATH", "").strip()
    if raw and Path(raw).is_file():
        return Path(raw)
    for base in _playwright_dirs():
        if not base.is_dir():
            continue
        # Full Chromium first, then the headless shell (what the Windows bundle installs).
        for prefix in ("chromium-", "chromium_headless_shell-"):
            for build in sorted((d for d in base.iterdir() if d.name.startswith(prefix)), reverse=True):
                for rel in _CHROME_NAMES:
                    if (build / rel).is_file():
                        return build / rel
    return None


# -- workspace ----------------------------------------------------------------
def prepare_workdir(workspace: Path, root: Optional[Path] = None) -> Path:
    """``<workspace>/anatomy`` with the vendored ``kit/`` and ``scripts/``; refreshed when the version changes."""
    src = skill_dir(root)
    work = Path(workspace) / WORKDIR
    (work / "figures").mkdir(parents=True, exist_ok=True)
    info = upstream_info(root)
    stamp = {"version": info.get("version", ""), "commit": info.get("commit", "")}
    marker = work / MARKER
    try:
        current = json.loads(marker.read_text("utf-8"))
    except (OSError, ValueError):
        current = {}
    if current != stamp or not all((work / d).is_dir() for d in COPIED):
        for name in COPIED:
            if (src / name).is_dir():
                shutil.rmtree(work / name, ignore_errors=True)
                shutil.copytree(src / name, work / name)
        marker.write_text(json.dumps(stamp) + "\n", "utf-8")
    return work

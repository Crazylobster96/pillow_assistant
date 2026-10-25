"""Desktop apps some text-to-cad Skills hand off to, located or installed on first use.

- OrcaSlicer (``gcode``): open source; the portable Windows zip / Linux AppImage
  from its GitHub releases is unpacked into the CAD apps folder.
- Bambu Studio (``bambu-labs``): open source; the Windows zip / Linux AppImage
  likewise.
- Bambu Connect (``bambu-labs``): Bambu Lab's closed app, not redistributable, so
  "install" opens its official download page for the user to finish.

The apps folder is ``PILLOW_CAD_APPS_DIR`` (the Windows bundle points it at
``cad-runtime/apps``), else ``~/.pillow/cad-apps``.  Only the standard library is
used so this works in any install.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import sys
import urllib.request
import webbrowser
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

USER_AGENT = "PillowAssistant-CAD"


@dataclass(frozen=True)
class AppSpec:
    key: str
    title: str
    repo: str = ""                         # GitHub repo with release assets
    commands: tuple[str, ...] = ()         # names on PATH
    windows_exe: str = ""                  # executable inside an unpacked Windows zip
    windows_paths: tuple[str, ...] = ()    # usual installer locations, under %ProgramFiles%
    asset_patterns: dict = None            # platform -> regex for the release asset
    download_page: str = ""
    env: str = ""                          # explicit override, e.g. PILLOW_ORCASLICER


APPS = {
    "orcaslicer": AppSpec(
        key="orcaslicer", title="OrcaSlicer", repo="OrcaSlicer/OrcaSlicer",
        commands=("orca-slicer", "orcaslicer", "OrcaSlicer"), windows_exe="orca-slicer.exe",
        windows_paths=(r"OrcaSlicer\orca-slicer.exe",),
        asset_patterns={"windows-x64": r"^OrcaSlicer_Windows_.*_x64_portable\.zip$",
                        "windows-arm64": r"^OrcaSlicer_Windows_.*_arm64_portable\.zip$",
                        "linux-x64": r"^OrcaSlicer_Linux_AppImage_Ubuntu2404_V[\d.]+\.AppImage$",
                        "linux-arm64": r"^OrcaSlicer_Linux_AppImage_Ubuntu2404_aarch64_.*\.AppImage$"},
        download_page="https://github.com/OrcaSlicer/OrcaSlicer/releases/latest",
        env="PILLOW_ORCASLICER",
    ),
    "bambu-studio": AppSpec(
        key="bambu-studio", title="Bambu Studio", repo="bambulab/BambuStudio",
        commands=("bambu-studio", "BambuStudio"), windows_exe="bambu-studio.exe",
        windows_paths=(r"Bambu Studio\bambu-studio.exe",),
        asset_patterns={"windows-x64": r"^Bambu_Studio_win_.*\.zip$",
                        "linux-x64": r"^BambuStudio_ubu24-.*\.AppImage$"},
        download_page="https://bambulab.com/en/download/studio",
        env="PILLOW_BAMBU_STUDIO",
    ),
    "bambu-connect": AppSpec(
        key="bambu-connect", title="Bambu Connect",
        commands=("bambu-connect", "BambuConnect"), windows_exe="Bambu Connect.exe",
        windows_paths=(r"Bambu Connect\Bambu Connect.exe",),
        download_page="https://bambulab.com/en/software/bambu-connect",
        env="PILLOW_BAMBU_CONNECT",
    ),
}


class CadAppError(RuntimeError):
    pass


def apps_dir() -> Path:
    raw = os.environ.get("PILLOW_CAD_APPS_DIR", "").strip()
    return Path(raw) if raw else Path.home() / ".pillow" / "cad-apps"


def platform_key() -> str:
    machine = platform.machine().lower()
    arch = "arm64" if machine in {"arm64", "aarch64"} else "x64"
    if sys.platform.startswith("win"):
        return f"windows-{arch}"
    if sys.platform == "darwin":
        return f"macos-{arch}"
    return f"linux-{arch}"


def _find_in(folder: Path, name: str) -> Optional[Path]:
    if not folder.is_dir():
        return None
    direct = folder / name
    if direct.is_file():
        return direct
    for p in folder.rglob(name):
        if p.is_file():
            return p
    return None


def locate(key: str) -> Optional[Path]:
    """Path of an installed app, or None."""
    spec = APPS[key]
    override = os.environ.get(spec.env, "").strip() if spec.env else ""
    if override and Path(override).exists():
        return Path(override)
    mine = apps_dir() / key
    if sys.platform.startswith("win"):
        found = _find_in(mine, spec.windows_exe) if spec.windows_exe else None
        if found:
            return found
        bases = [Path(b) for b in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")) if b]
        if os.environ.get("LOCALAPPDATA"):
            bases.append(Path(os.environ["LOCALAPPDATA"]) / "Programs")
        for base in bases:
            for rel in spec.windows_paths:
                if (base / rel).is_file():
                    return base / rel
    elif sys.platform == "darwin":
        for name in (spec.title, spec.title.replace(" ", "")):
            app = Path("/Applications") / f"{name}.app"
            if app.is_dir():
                return app
    else:
        images = sorted(mine.glob("*.AppImage")) if mine.is_dir() else []
        if images:
            return images[-1]
    for name in spec.commands:
        found = shutil.which(name)
        if found:
            return Path(found)
    return None


def command_for(app: Path) -> list[str]:
    """argv prefix that runs an app's command line (macOS bundles point inside the .app)."""
    if app.suffix == ".app":
        exes = sorted((app / "Contents" / "MacOS").glob("*"))
        return [str(exes[0])] if exes else ["open", "-a", str(app), "--args"]
    return [str(app)]


def profiles_dir(key: str, app: Optional[Path]) -> Optional[Path]:
    """OrcaSlicer's bundled presets folder for a portable install (for orca_presets.py --profiles)."""
    if key != "orcaslicer" or app is None:
        return None
    for base in (app.parent, *app.parents[:3]):
        candidate = base / "resources" / "profiles"
        if candidate.is_dir():
            return candidate
    return None


def _http_get(url: str, timeout: float = 30) -> bytes:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
    token = os.environ.get("PILLOW_UPDATE_TOKEN", "").strip()
    if token and url.startswith("https://api.github.com/"):
        headers["Authorization"] = f"Bearer {token}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as resp:  # noqa: S310
        return resp.read()


def _download(url: str, dest: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    tmp = dest.with_name(dest.name + ".part")
    with urllib.request.urlopen(req, timeout=60) as resp, open(tmp, "wb") as fh:  # noqa: S310
        shutil.copyfileobj(resp, fh, 1 << 20)
    os.replace(tmp, dest)


def release_asset(spec: AppSpec, plat: str, get_json: Callable[[str], dict]) -> tuple[str, str, str]:
    """(tag, asset name, download URL) of the newest non-prerelease build for ``plat``."""
    pattern = (spec.asset_patterns or {}).get(plat)
    if not pattern:
        raise CadAppError(f"no automatic {spec.title} install for {plat}")
    releases = get_json(f"https://api.github.com/repos/{spec.repo}/releases?per_page=15")
    for rel in releases if isinstance(releases, list) else []:
        if rel.get("draft") or rel.get("prerelease"):
            continue
        for asset in rel.get("assets") or []:
            if re.match(pattern, asset.get("name", "")):
                return rel.get("tag_name", ""), asset["name"], asset["browser_download_url"]
    raise CadAppError(f"no {spec.title} release asset matches {pattern}")


def _safe_extract(archive: Path, dest: Path) -> None:
    with zipfile.ZipFile(archive) as zf:
        root = dest.resolve()
        for member in zf.namelist():
            if not str((dest / member).resolve()).startswith(str(root)):
                raise CadAppError(f"unsafe path in archive: {member}")
        zf.extractall(dest)


def install(key: str, *, get_json: Optional[Callable[[str], dict]] = None,
            download: Callable[[str, Path], None] = _download,
            open_page: Callable[[str], bool] = webbrowser.open) -> dict:
    """Install an app into the apps folder; returns ``{"path", "version"}`` or ``{"opened"}``."""
    spec = APPS[key]
    if not spec.repo:
        open_page(spec.download_page)
        return {"opened": spec.download_page}
    plat = platform_key()
    if plat.startswith("macos"):
        open_page(spec.download_page)
        return {"opened": spec.download_page}
    get_json = get_json or (lambda url: json.loads(_http_get(url).decode("utf-8")))
    tag, name, url = release_asset(spec, plat, get_json)
    target = apps_dir() / key
    staging = target.with_name(f".{key}.new")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    try:
        file = staging / name
        download(url, file)
        if name.lower().endswith(".zip"):
            _safe_extract(file, staging)
            file.unlink()
        else:
            file.chmod(0o755)
        (staging / "INSTALLED.json").write_text(json.dumps({"app": key, "version": tag, "asset": name}), "utf-8")
        shutil.rmtree(target, ignore_errors=True)
        os.replace(staging, target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    path = locate(key)
    if path is None:
        raise CadAppError(f"{spec.title} {tag} was downloaded but its executable was not found in {target}")
    return {"path": str(path), "version": tag}


def installed_version(key: str) -> str:
    try:
        return json.loads((apps_dir() / key / "INSTALLED.json").read_text("utf-8")).get("version", "")
    except (OSError, ValueError):
        return ""

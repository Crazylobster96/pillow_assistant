"""Desktop apps some text-to-cad Skills hand off to, located or installed on first use.

- OrcaSlicer (``gcode``): open source; the portable Windows zip / Linux AppImage
  from its GitHub releases is unpacked into the CAD apps folder.
- Bambu Studio (``bambu-labs``): open source; the Windows zip / Linux AppImage
  likewise.
- FreeCAD (``cam`` milling): open source (LGPL); its CAM workbench runs headless
  through ``freecadcmd``.  The Windows .7z is unpacked with the system ``tar``
  (bsdtar reads 7z); the Linux AppImage is extracted so ``freecadcmd`` can run.
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
import subprocess
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
    linux_exe: str = ""                    # executable inside an extracted AppImage, if it is extracted
    macos_exe: str = ""                    # command-line executable inside the .app bundle


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
    "freecad": AppSpec(
        key="freecad", title="FreeCAD", repo="FreeCAD/FreeCAD",
        commands=("freecadcmd", "FreeCADCmd"), windows_exe="FreeCADCmd.exe",
        windows_paths=(r"FreeCAD 1.1\bin\FreeCADCmd.exe", r"FreeCAD 1.0\bin\FreeCADCmd.exe"),
        asset_patterns={"windows-x64": r"^FreeCAD_[\d.]+.*-Windows-x86_64.*\.7z$",
                        "linux-x64": r"^FreeCAD_[\d.]+.*-Linux-x86_64.*\.AppImage$",
                        "linux-arm64": r"^FreeCAD_[\d.]+.*-Linux-aarch64.*\.AppImage$"},
        download_page="https://www.freecad.org/downloads.php",
        env="PILLOW_FREECAD", linux_exe="freecadcmd", macos_exe="Contents/Resources/bin/freecadcmd",
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
            if spec.macos_exe and (app / spec.macos_exe).is_file():
                return app / spec.macos_exe
            if app.is_dir() and not spec.macos_exe:
                return app
    else:
        found = _find_in(mine / "squashfs-root", spec.linux_exe) if spec.linux_exe else None
        if found:
            return found
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


def _extract_7z(archive: Path, dest: Path) -> None:
    """Unpack a .7z with the system bsdtar (Windows 10+ ships it), else py7zr if installed."""
    tar = shutil.which("tar")
    if tar:
        p = subprocess.run([tar, "-xf", str(archive), "-C", str(dest)], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=1800)
        if p.returncode == 0:
            return
        err = p.stderr.strip()[-300:]
    else:
        err = "no tar command"
    try:
        import py7zr  # type: ignore[import-not-found]
    except ImportError:
        raise CadAppError(f"cannot unpack {archive.name}: {err}") from None
    with py7zr.SevenZipFile(archive) as zf:
        root = dest.resolve()
        for member in zf.getnames():
            if not str((dest / member).resolve()).startswith(str(root)):
                raise CadAppError(f"unsafe path in archive: {member}")
        zf.extractall(dest)


def _extract_appimage(image: Path, dest: Path) -> None:
    """Extract an AppImage (no FUSE needed) so the command-line tools inside can run directly."""
    p = subprocess.run([str(image), "--appimage-extract"], cwd=str(dest), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=1800)
    if p.returncode != 0 or not (dest / "squashfs-root").is_dir():
        raise CadAppError(f"cannot extract {image.name}: {p.stderr.strip()[-300:]}")
    image.unlink()


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
        elif name.lower().endswith(".7z"):
            _extract_7z(file, staging)
            file.unlink()
        else:
            file.chmod(0o755)
            if spec.linux_exe:
                _extract_appimage(file, staging)
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

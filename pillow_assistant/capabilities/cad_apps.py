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

Downloads go through the GitHub mirrors of ``core/mirrors.py`` when GitHub is
unreachable (mainland China).  A mirrored file is only kept when its SHA-256
matches the digest GitHub publishes for the asset.  When the GitHub API itself
cannot be reached, ``pins.json`` in the apps folder (written by the Windows
release build: ``python -m pillow_assistant.capabilities.cad_apps pin``) names
the asset, its URL and its digest.
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
import urllib.request
import webbrowser
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from pillow_assistant.core import mirrors

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
        asset_patterns={"windows-x64": r"^Bambu_Studio_win[-_].*\.zip$",
                        "linux-x64": r"^BambuStudio_ubu(ntu)?24[-.].*\.AppImage$"},
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


def release_asset(spec: AppSpec, plat: str, get_json: Callable[[str], dict]) -> dict:
    """``{tag, name, url, digest}`` of the newest non-prerelease build for ``plat``.

    ``digest`` is GitHub's ``sha256:<hex>`` for the asset, or "" for old releases.
    """
    pattern = (spec.asset_patterns or {}).get(plat)
    if not pattern:
        raise CadAppError(f"no automatic {spec.title} install for {plat}")
    releases = get_json(f"https://api.github.com/repos/{spec.repo}/releases?per_page=15")
    releases = releases if isinstance(releases, list) else []
    for rel in releases:
        if rel.get("draft") or rel.get("prerelease"):
            continue
        for asset in rel.get("assets") or []:
            if re.match(pattern, asset.get("name", "")):
                return {"tag": rel.get("tag_name", ""), "name": asset["name"],
                        "url": asset["browser_download_url"], "digest": asset.get("digest") or ""}
    newest = next((r for r in releases if not r.get("draft") and not r.get("prerelease")), {})
    names = ", ".join(a.get("name", "") for a in newest.get("assets") or [])
    raise CadAppError(f"no {spec.title} release asset matches {pattern}"
                      + (f" (newest {newest.get('tag_name', '')} has: {names})" if names else ""))


def pinned_asset(key: str, plat: str) -> Optional[dict]:
    """The asset recorded in ``pins.json`` for when the GitHub API is out of reach."""
    try:
        pins = json.loads((apps_dir() / "pins.json").read_text("utf-8"))
    except (OSError, ValueError):
        return None
    pin = (pins.get(key) or {}).get(plat) if isinstance(pins, dict) else None
    if isinstance(pin, dict) and all(isinstance(pin.get(k), str) for k in ("tag", "name", "url", "digest")) \
            and pin["url"].startswith("https://github.com/"):
        return pin
    return None


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def fetch_asset(asset: dict, dest: Path, download: Optional[Callable[[str, Path], None]] = None,
                prefer_mirrors: bool = False) -> None:
    """Download ``asset`` to ``dest``, directly or through a mirror, checking its digest."""
    download = download or _download
    digest = asset.get("digest") or ""
    want = digest.split(":", 1)[1].lower() if digest.lower().startswith("sha256:") else ""
    errors = []
    for url, mirrored in mirrors.github_urls(asset["url"], prefer_mirrors):
        if mirrored and not want:
            continue  # a mirror is a third party: only use it when the file can be checked
        try:
            download(url, dest)
        except Exception as exc:  # noqa: BLE001 - try the next source
            errors.append(f"{url}: {exc}")
            continue
        if want and _sha256(dest) != want:
            dest.unlink(missing_ok=True)
            errors.append(f"{url}: SHA-256 does not match GitHub's digest")
            continue
        return
    raise CadAppError(f"cannot download {asset['name']}: " + "; ".join(errors[-3:]))


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
            download: Optional[Callable[[str, Path], None]] = None,
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
    blocked = False
    try:
        asset = release_asset(spec, plat, get_json)
    except CadAppError:
        raise
    except Exception as exc:  # noqa: BLE001 - GitHub API unreachable or rate limited
        blocked = True
        asset = pinned_asset(key, plat)
        if asset is None:
            raise CadAppError(f"cannot list {spec.title} releases: {exc}") from exc
    tag, name = asset["tag"], asset["name"]
    target = apps_dir() / key
    staging = target.with_name(f".{key}.new")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    try:
        file = staging / name
        fetch_asset(asset, file, download, prefer_mirrors=blocked)
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


def pin(plat: str, keys=None, get_json: Optional[Callable[[str], dict]] = None) -> dict:
    """``{key: {plat: asset}}`` for every app with a build for ``plat``: the contents of pins.json."""
    get_json = get_json or (lambda url: json.loads(_http_get(url).decode("utf-8")))
    out = {}
    for key in keys or [k for k, a in APPS.items() if a.repo]:
        spec = APPS[key]
        if (spec.asset_patterns or {}).get(plat):
            try:
                out[key] = {plat: release_asset(spec, plat, get_json)}
            except CadAppError as exc:  # one app's release naming changed: still pin the others
                print(f"warning: {exc}", file=sys.stderr)
    return out


def main(argv=None) -> int:
    """``pin <platform> <pins.json>`` or ``install <app>...`` (used by the Windows release build)."""
    import argparse

    ap = argparse.ArgumentParser(prog="python -m pillow_assistant.capabilities.cad_apps")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pin")
    p.add_argument("platform")
    p.add_argument("output")
    i = sub.add_parser("install")
    i.add_argument("apps", nargs="+", choices=[k for k, a in APPS.items() if a.repo])
    args = ap.parse_args(argv)
    if args.cmd == "pin":
        pins = pin(args.platform)
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps(pins, indent=2), "utf-8")
        print(json.dumps(pins, indent=2))
        return 0
    for key in args.apps:
        print(key, install(key))
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Pillow Assistant launcher + self-updater for the offline Windows bundle.

This file is copied to the bundle root as ``launcher.pyw`` and started by
``PillowAssistant.exe`` with the bundled interpreter.  It deliberately uses
only the standard library so it keeps working whatever the app's own
dependencies look like.

Bundle layout::

    PillowAssistant/
      PillowAssistant.exe      tiny no-console stub that runs this launcher
      launcher.pyw             this file
      update.json              updater settings (repo / channel / branch ...)
      python/                  relocatable CPython with every dependency
      app/                     the application source + VERSION.json
      ms-playwright/           Chromium for the browser tool
      models/                  optional offline ASR models
      updates/                 runtime: downloaded and staged updates

Update flow (never touches files that are in use):

1. On start, a staged update (``updates/staged`` + ``updates/staged.json``)
   is applied *before* the app is imported: if ``requirements.txt`` changed,
   the new dependencies are installed with the bundled pip first; then
   ``app`` is swapped for the staged copy (the old one is kept as
   ``updates/previous`` for rollback).  Any failure leaves the current
   version running.
2. While the app runs, a background thread asks GitHub for the newest code
   version (latest commit of a branch, or latest release), downloads the
   source archive and stages it.  It is applied on the next launch.
3. Offline, every network step fails fast and silently; the app always
   starts with what is installed.
   Where GitHub downloads or PyPI are blocked (mainland China), the source
   archive comes through a GitHub mirror and pip falls back to a PyPI mirror
   (``mirrors`` in update.json: auto / cn / off).  A mirrored archive is only
   used when its files hash to the commit's git tree from the GitHub API.
4. A freshly applied version is on probation (``updates/probation.json``)
   until it has run for ``HEALTHY_SECONDS`` or exited cleanly.  If it raises
   during start-up, or two starts in a row never become healthy (hard
   crash), ``updates/previous`` is restored automatically.  A rolled-back
   commit, automatic or ``--rollback``, is remembered in ``state.json`` and
   never staged again; the next newer commit updates as usual.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import logging
import logging.handlers
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

ROOT = Path(__file__).resolve().parent

DEFAULT_CONFIG = {
    "enabled": True,
    "repo": "Crazylobster96/pillow_assistant",
    # "branch": follow the newest commit of ``branch``;
    # "release": follow the newest GitHub release (tag).
    "channel": "branch",
    "branch": "main",
    "check_interval_hours": 6,
    "startup_delay_seconds": 20,
    "timeout_seconds": 15,
    # Create desktop + Start menu shortcuts on first start (and again after
    # the bundle folder is moved). A shortcut the user deleted stays deleted.
    "desktop_shortcut": True,
    # Mainland China: "auto" tries GitHub / PyPI first and mirrors after them,
    # "cn" tries the mirrors first, "off" never uses them.
    "mirrors": "auto",
    "github_mirrors": ["https://ghfast.top/", "https://gh-proxy.com/", "https://ghproxy.net/"],
    "pypi_mirrors": ["https://pypi.tuna.tsinghua.edu.cn/simple", "https://mirrors.aliyun.com/pypi/simple/"],
}

HEALTHY_SECONDS = 60      # a new version that has run this long is kept
MAX_UNHEALTHY_STARTS = 2  # starts that never became healthy before auto-rollback
MAX_SKIPPED = 20

USER_AGENT = "PillowAssistant-Updater"
API = "https://api.github.com"

log = logging.getLogger("pillow.launcher")


# -- small helpers -----------------------------------------------------------
def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _rmtree(path: Path) -> None:
    if not path.exists():
        return

    def onerror(func, p, _exc):  # read-only files (e.g. from zip extraction)
        try:
            os.chmod(p, 0o700)
            func(p)
        except OSError:
            pass

    shutil.rmtree(path, onerror=onerror)


def requirements_hash(path: Path) -> str:
    """Hash of requirements.txt, insensitive to CRLF/LF checkouts."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").split("\n")]
    return hashlib.sha256("\n".join(lines).strip().encode("utf-8")).hexdigest()


class Paths:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.app = root / "app"
        self.python_dir = root / "python"
        self.updates = root / "updates"
        self.staged = self.updates / "staged"
        self.staged_marker = self.updates / "staged.json"
        self.previous = self.updates / "previous"
        self.state = self.updates / "state.json"
        self.probation = self.updates / "probation.json"
        self.lock = self.updates / ".lock"
        self.config = root / "update.json"
        self.version = self.app / "VERSION.json"

    @property
    def python_exe(self) -> Path:
        exe = self.python_dir / ("python.exe" if os.name == "nt" else "bin/python3")
        return exe if exe.exists() else Path(sys.executable)


def load_config(paths: Paths) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    cfg.update({k: v for k, v in _read_json(paths.config).items() if not k.startswith("_")})
    if os.environ.get("PILLOW_NO_UPDATE", "").strip() in ("1", "true", "yes"):
        cfg["enabled"] = False
    return cfg


def current_version(paths: Paths) -> dict:
    return _read_json(paths.version)


class UpdateLock:
    """Cross-process lock so two launchers never stage/apply at once."""

    STALE_SECONDS = 30 * 60

    def __init__(self, paths: Paths) -> None:
        self.path = paths.lock
        self.fd: Optional[int] = None

    def __enter__(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self.fd, str(os.getpid()).encode())
                return True
            except FileExistsError:
                try:
                    if time.time() - self.path.stat().st_mtime > self.STALE_SECONDS:
                        self.path.unlink()
                        continue
                except OSError:
                    pass
                return False
        return False

    def __exit__(self, *_exc) -> None:
        if self.fd is not None:
            os.close(self.fd)
            try:
                self.path.unlink()
            except OSError:
                pass
            self.fd = None


# -- GitHub ------------------------------------------------------------------
def _http_get(url: str, timeout: float, accept: str = "application/vnd.github+json") -> bytes:
    headers = {"User-Agent": USER_AGENT, "Accept": accept}
    token = os.environ.get("PILLOW_UPDATE_TOKEN", "").strip()
    if token and url.startswith(API):
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed https hosts
        return resp.read()


def _get_json(url: str, timeout: float) -> dict:
    return json.loads(_http_get(url, timeout).decode("utf-8"))


def fetch_remote_version(cfg: dict) -> Optional[dict]:
    """Return ``{"sha", "ref", "zip_url"}`` of the newest code version."""
    repo, timeout = cfg["repo"], float(cfg["timeout_seconds"])
    if cfg.get("channel") == "release":
        release = _get_json(f"{API}/repos/{repo}/releases/latest", timeout)
        ref = release["tag_name"]
    else:
        ref = cfg.get("branch") or "main"
    commit = _get_json(f"{API}/repos/{repo}/commits/{ref}", timeout)
    sha = commit["sha"]
    return {
        "sha": sha,
        "ref": ref,
        "channel": cfg.get("channel", "branch"),
        "committed_at": (commit.get("commit") or {}).get("committer", {}).get("date"),
        "tree": ((commit.get("commit") or {}).get("tree") or {}).get("sha", ""),
        "zip_url": f"{API}/repos/{repo}/zipball/{sha}",
        "archive_url": f"https://github.com/{repo}/archive/{sha}.zip",
    }


# -- mirrors -----------------------------------------------------------------
def _mirror_mode(cfg: dict) -> str:
    mode = str(cfg.get("mirrors") or "auto").lower()
    return mode if mode in ("auto", "cn", "off") else "auto"


def _mirror_list(cfg: dict, key: str) -> list:
    if _mirror_mode(cfg) == "off":
        return []
    return [str(m) for m in cfg.get(key) or [] if str(m).startswith("https://")]


def download_sources(cfg: dict, remote: dict) -> list:
    """``(url, mirrored)`` to fetch the source archive from, in order."""
    direct = [(remote["zip_url"], False)]
    if not remote.get("tree"):
        return direct  # nothing to check a mirrored copy against
    via = [((m if m.endswith("/") else m + "/") + remote["archive_url"], True)
           for m in _mirror_list(cfg, "github_mirrors")]
    return via + direct if _mirror_mode(cfg) == "cn" else direct + via


def _git_object(kind: str, data: bytes) -> bytes:
    return hashlib.sha1(f"{kind} {len(data)}\0".encode() + data).digest()


def _crlf_patterns(files: dict) -> list:
    """Globs that .gitattributes checks out with CRLF; GitHub's archives contain them that way."""
    raw = files.get(".gitattributes", (b"", 0))[0].decode("utf-8", "replace")
    out = []
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) > 1 and not parts[0].startswith("#") and "eol=crlf" in parts[1:]:
            out.append(parts[0])
    return out


def archive_tree_sha(archive: bytes) -> str:
    """The git tree id of a GitHub source archive, to compare with the commit's tree."""
    import fnmatch

    files = {}
    with zipfile.ZipFile(io.BytesIO(archive)) as zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        prefix = os.path.commonprefix([i.filename for i in infos]).rpartition("/")[0]
        prefix = prefix + "/" if prefix else ""
        for info in infos:
            mode = (info.external_attr >> 16) & 0o170777
            files[info.filename[len(prefix):]] = (zf.read(info), mode)
    crlf = _crlf_patterns(files)
    tree: dict = {}
    for path, (data, mode) in files.items():
        name = path.rsplit("/", 1)[-1]
        if any(fnmatch.fnmatch(name, pat) or fnmatch.fnmatch(path, pat) for pat in crlf):
            data = data.replace(b"\r\n", b"\n")
        if mode & 0o170000 == 0o120000:
            entry = ("120000", _git_object("blob", data))
        else:
            entry = ("100755" if mode & 0o111 else "100644", _git_object("blob", data))
        node = tree
        parts = path.split("/")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = entry

    def write(node: dict) -> bytes:
        rows = []
        for name, value in node.items():
            if isinstance(value, dict):
                rows.append((name + "/", b"40000 " + name.encode() + b"\0" + write(value)))
            else:
                rows.append((name, value[0].encode() + b" " + name.encode() + b"\0" + value[1]))
        return _git_object("tree", b"".join(r for _, r in sorted(rows)))

    return write(tree).hex()


def is_newer(cfg: dict, local_sha: str, remote_sha: str) -> bool:
    """True when ``remote_sha`` should replace ``local_sha``.

    Uses GitHub's compare API so a locally newer build (e.g. a release built
    from a commit not yet on the branch) is never "downgraded".
    """
    if not remote_sha or remote_sha == local_sha:
        return False
    if not local_sha:
        return True
    try:
        cmp = _get_json(f"{API}/repos/{cfg['repo']}/compare/{local_sha}...{remote_sha}",
                        float(cfg["timeout_seconds"]))
    except urllib.error.HTTPError as exc:
        return exc.code == 404  # local commit unknown to GitHub: trust the remote
    return cmp.get("status") not in ("behind", "identical")


# -- staging -----------------------------------------------------------------
def _extract_source(archive: bytes, dest: Path) -> Path:
    """Extract a GitHub zipball and return the repository root inside it."""
    with zipfile.ZipFile(io.BytesIO(archive)) as zf:
        for member in zf.namelist():
            target = (dest / member).resolve()
            if not str(target).startswith(str(dest.resolve())):
                raise ValueError(f"unsafe path in archive: {member}")
        zf.extractall(dest)
    roots = [p for p in dest.iterdir() if p.is_dir()]
    root = roots[0] if len(roots) == 1 else dest
    if not (root / "pillow_assistant" / "app.py").is_file():
        raise ValueError("archive does not look like pillow_assistant source")
    return root


def stage_update(paths: Paths, cfg: dict,
                 download: Callable[[str, float], bytes] = None) -> Optional[dict]:
    """Check GitHub and stage a newer version. Returns its metadata or None."""
    download = download or (lambda url, t: _http_get(url, t, accept="*/*"))
    remote = fetch_remote_version(cfg)
    local_sha = current_version(paths).get("sha", "")
    staged = _read_json(paths.staged_marker)
    if remote is None or not is_newer(cfg, local_sha, remote["sha"]):
        log.info("up to date (%s)", local_sha[:10] or "unknown")
        return None
    if remote["sha"] in skipped_shas(paths):
        log.info("skipping %s: it was rolled back; waiting for a newer commit", remote["sha"][:10])
        return None
    if staged.get("sha") == remote["sha"] and paths.staged.is_dir():
        log.info("update %s already staged", remote["sha"][:10])
        return staged

    log.info("downloading update %s (%s)", remote["sha"][:10], remote["ref"])
    archive, errors = None, []
    for url, mirrored in download_sources(cfg, remote):
        try:
            data = download(url, max(60.0, float(cfg["timeout_seconds"]) * 8))
            if mirrored and archive_tree_sha(data) != remote["tree"]:
                raise ValueError("files do not match the commit on GitHub")
            archive = data
            break
        except Exception as exc:  # noqa: BLE001 - try the next source
            log.info("download from %s failed: %s", url, exc)
            errors.append(exc)
    if archive is None:
        raise errors[-1]
    work = paths.updates / f"download-{uuid.uuid4().hex[:8]}"
    work.mkdir(parents=True)
    try:
        src = _extract_source(archive, work)
        meta = {k: v for k, v in remote.items() if k != "zip_url"}
        meta.update({
            "staged_at": _now(),
            "requirements_sha256": requirements_hash(src / "requirements.txt"),
        })
        _write_json(src / "VERSION.json", meta)
        # Replace any older staged copy, then publish the marker last.
        try:
            paths.staged_marker.unlink()
        except FileNotFoundError:
            pass
        _rmtree(paths.staged)
        os.replace(src, paths.staged)
        _write_json(paths.staged_marker, meta)
        log.info("update %s staged; it will be applied on next start", remote["sha"][:10])
        return meta
    finally:
        _rmtree(work)


# -- applying ----------------------------------------------------------------
def pip_indexes(cfg: dict) -> list:
    """Package indexes to try in order; None is pip's own default (PyPI)."""
    mirrors = _mirror_list(cfg, "pypi_mirrors")
    return mirrors + [None] if _mirror_mode(cfg) == "cn" else [None] + mirrors


def _install_requirements(paths: Paths, requirements: Path) -> bool:
    base = [str(paths.python_exe), "-E", "-s", "-m", "pip", "install",
            "--disable-pip-version-check", "--no-warn-script-location",
            "-r", str(requirements)]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    for index in pip_indexes(load_config(paths)):
        cmd = base + (["-i", index] if index else [])
        log.info("installing changed dependencies: %s", " ".join(cmd))
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600,
                                  creationflags=flags)
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.warning("dependency install failed: %s", exc)
            continue
        if proc.returncode == 0:
            return True
        log.warning("dependency install failed (%s):\n%s", proc.returncode,
                    (proc.stdout + proc.stderr)[-4000:])
    return False


def apply_staged(paths: Paths,
                 install: Callable[[Paths, Path], bool] = _install_requirements) -> bool:
    """Swap in a staged update. Returns True when the app was updated."""
    meta = _read_json(paths.staged_marker)
    if not meta or not paths.staged.is_dir():
        return False
    if meta.get("sha") and meta.get("sha") == current_version(paths).get("sha"):
        paths.staged_marker.unlink(missing_ok=True)
        _rmtree(paths.staged)
        return False
    if not (paths.staged / "pillow_assistant" / "app.py").is_file():
        log.warning("staged update is incomplete; discarding it")
        paths.staged_marker.unlink(missing_ok=True)
        _rmtree(paths.staged)
        return False

    new_req = meta.get("requirements_sha256") or requirements_hash(paths.staged / "requirements.txt")
    old_req = current_version(paths).get("requirements_sha256") \
        or requirements_hash(paths.app / "requirements.txt")
    if new_req != old_req:
        if not install(paths, paths.staged / "requirements.txt"):
            log.warning("keeping current version; will retry the update next start")
            return False

    _rmtree(paths.previous)
    try:
        if paths.app.exists():
            os.replace(paths.app, paths.previous)
        os.replace(paths.staged, paths.app)
    except OSError as exc:
        log.warning("could not swap in update: %s", exc)
        if not paths.app.exists() and paths.previous.exists():
            os.replace(paths.previous, paths.app)
        return False

    # Keep runtime data that older versions wrote inside the app folder.
    old_data, new_data = paths.previous / "data", paths.app / "data"
    if old_data.is_dir() and not new_data.exists():
        try:
            shutil.move(str(old_data), str(new_data))
        except OSError:
            log.warning("could not carry over %s", old_data, exc_info=True)

    # Refresh the launcher itself; the new copy takes effect next start.
    new_launcher = paths.app / "packaging" / "windows" / "launcher.py"
    if new_launcher.is_file():
        try:
            shutil.copyfile(new_launcher, paths.root / "launcher.pyw")
        except OSError:
            log.warning("could not refresh launcher", exc_info=True)

    paths.staged_marker.unlink(missing_ok=True)
    previous_sha = _read_json(paths.previous / "VERSION.json").get("sha", "")
    _write_json(paths.probation, {"sha": meta.get("sha", ""), "previous_sha": previous_sha,
                                  "applied_at": _now(), "starts": 0})
    log.info("updated to %s", str(meta.get("sha", ""))[:10])
    return True


def skipped_shas(paths: Paths) -> list:
    return list(_read_json(paths.state).get("skipped_shas") or [])


def rollback(paths: Paths, reason: str = "manual") -> bool:
    """Restore ``updates/previous`` and never auto-install the rolled-back commit again."""
    if not (paths.previous / "pillow_assistant").is_dir():
        return False
    bad_sha = current_version(paths).get("sha", "")
    broken = paths.updates / f"rolled-back-{uuid.uuid4().hex[:8]}"
    os.replace(paths.app, broken)
    os.replace(paths.previous, paths.app)
    # Runtime data that older versions keep inside app/ moves back with it.
    if (broken / "data").is_dir() and not (paths.app / "data").exists():
        try:
            shutil.move(str(broken / "data"), str(paths.app / "data"))
        except OSError:
            log.warning("could not carry back %s", broken / "data", exc_info=True)
    _rmtree(broken)
    paths.probation.unlink(missing_ok=True)
    # A staged copy of the same bad commit must not come straight back.
    if bad_sha and _read_json(paths.staged_marker).get("sha") == bad_sha:
        paths.staged_marker.unlink(missing_ok=True)
        _rmtree(paths.staged)
    state = _read_json(paths.state)
    if bad_sha:
        skipped = [s for s in (state.get("skipped_shas") or []) if s != bad_sha] + [bad_sha]
        state["skipped_shas"] = skipped[-MAX_SKIPPED:]
    state["last_rollback"] = {"sha": bad_sha, "to": current_version(paths).get("sha", ""),
                              "reason": reason, "at": _now()}
    try:
        _write_json(paths.state, state)
    except OSError:
        log.warning("could not record rollback", exc_info=True)
    log.warning("rolled back %s -> %s (%s)", bad_sha[:10],
                str(current_version(paths).get("sha", ""))[:10], reason)
    return True


def clear_skipped(paths: Paths) -> None:
    state = _read_json(paths.state)
    state.pop("skipped_shas", None)
    _write_json(paths.state, state)


# -- probation: keep a new version only once it has proven it starts ---------
def begin_probation_start(paths: Paths) -> bool:
    """Count this start against a freshly applied version.

    Returns True while the current version is still on probation.  Rolls back
    (and returns False) when earlier starts never became healthy.
    """
    probe = _read_json(paths.probation)
    if not probe:
        return False
    if probe.get("sha") != current_version(paths).get("sha"):
        paths.probation.unlink(missing_ok=True)  # stale marker (manual swap, rollback, ...)
        return False
    if int(probe.get("starts", 0)) >= MAX_UNHEALTHY_STARTS:
        rollback(paths, reason=f"{probe['starts']} starts never became healthy")
        return False
    probe["starts"] = int(probe.get("starts", 0)) + 1
    _write_json(paths.probation, probe)
    return True


def mark_healthy(paths: Paths) -> None:
    probe = _read_json(paths.probation)
    if probe and probe.get("sha") == current_version(paths).get("sha"):
        paths.probation.unlink(missing_ok=True)
        log.info("version %s is healthy", str(probe.get("sha", ""))[:10])


def run_app(paths: Paths, app_main: Callable[[], Optional[int]], on_probation: bool,
            relaunch: Optional[Callable[[], None]] = None) -> Optional[int]:
    """Run the app; a new version that crashes while on probation is rolled back."""
    timer = None
    if on_probation:
        timer = threading.Timer(HEALTHY_SECONDS, mark_healthy, args=(paths,))
        timer.daemon = True
        timer.start()
    try:
        code = app_main()
    except BaseException as exc:
        if on_probation and not isinstance(exc, KeyboardInterrupt) and paths.probation.exists():
            log.exception("new version failed while on probation")
            try:
                rolled = rollback(paths, reason=f"start-up error: {exc!r}"[:300])
            except OSError:
                log.exception("rollback failed; it will be retried on the next start")
                rolled = False
            if rolled and relaunch is not None:
                relaunch()
        raise
    finally:
        if timer is not None:
            timer.cancel()
    if on_probation and code in (0, None):
        mark_healthy(paths)
    return code


def _relaunch_previous() -> None:
    """Start the (now rolled-back) app again in a fresh process."""
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        subprocess.Popen([sys.executable, str(ROOT / "launcher.pyw"), *sys.argv[1:]],
                         creationflags=flags, close_fds=True)
    except OSError:
        log.warning("could not relaunch after rollback", exc_info=True)


def cleanup(paths: Paths) -> None:
    if not paths.updates.is_dir():
        return
    for p in paths.updates.glob("download-*"):
        _rmtree(p)


# -- background checker ------------------------------------------------------
def check_once(paths: Paths, cfg: dict, force: bool = False) -> Optional[dict]:
    state = _read_json(paths.state)
    interval = float(cfg.get("check_interval_hours", 6)) * 3600
    if not force and time.time() - float(state.get("last_check", 0)) < interval:
        return None
    with UpdateLock(paths) as locked:
        if not locked:
            return None
        try:
            result = stage_update(paths, cfg)
            state.update(last_check=time.time(), last_error=None)
            return result
        except Exception as exc:  # noqa: BLE001 - offline / API errors are expected
            log.info("update check failed: %s", exc)
            state.update(last_error=str(exc))
            return None
        finally:
            state["last_attempt"] = time.time()
            try:
                _write_json(paths.state, state)
            except OSError:
                pass


def start_background_checker(paths: Paths, cfg: dict) -> Optional[threading.Thread]:
    if not cfg.get("enabled", True):
        return None

    def loop() -> None:
        time.sleep(float(cfg.get("startup_delay_seconds", 20)))
        while True:
            check_once(paths, cfg)
            time.sleep(30 * 60)

    thread = threading.Thread(target=loop, name="pillow-updater", daemon=True)
    thread.start()
    return thread


# -- shortcuts ---------------------------------------------------------------
SHORTCUT_NAME = "Pillow Assistant.lnk"

# Paths come in through environment variables so non-ASCII folders (e.g. a
# Chinese user name) never need quoting inside the script.
_SHORTCUT_PS = r"""
$ErrorActionPreference = 'Stop'
$ws = New-Object -ComObject WScript.Shell
foreach ($folder in @([Environment]::GetFolderPath('Desktop'),
                      [Environment]::GetFolderPath('Programs'))) {
    if (-not $folder) { continue }
    $link = Join-Path $folder $env:PILLOW_LNK_NAME
    $s = $ws.CreateShortcut($link)
    $s.TargetPath = $env:PILLOW_LNK_TARGET
    $s.WorkingDirectory = $env:PILLOW_LNK_DIR
    $s.IconLocation = $env:PILLOW_LNK_ICON
    $s.Description = 'Pillow Assistant'
    $s.Save()
    Write-Output $link
}
"""


def _run_shortcut_script(target: Path, workdir: Path, icon: str) -> list:
    env = dict(os.environ, PILLOW_LNK_NAME=SHORTCUT_NAME, PILLOW_LNK_TARGET=str(target),
               PILLOW_LNK_DIR=str(workdir), PILLOW_LNK_ICON=icon)
    proc = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-Command", _SHORTCUT_PS],
        env=env, capture_output=True, text=True, timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if proc.returncode != 0:
        raise OSError(proc.stderr.strip() or f"powershell exited {proc.returncode}")
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def create_shortcuts(paths: Paths, force: bool = False,
                     run: Callable[[Path, Path, str], list] = _run_shortcut_script) -> list:
    """Create desktop + Start menu shortcuts to PillowAssistant.exe once.

    Recorded in ``updates/state.json``; repeated only when forced or when the
    bundle now lives in a different folder than when the shortcuts were made.
    """
    target = paths.root / "PillowAssistant.exe"
    if not target.is_file():
        return []
    state = _read_json(paths.state)
    if not force and state.get("shortcuts_for") == str(paths.root):
        return []
    ico = paths.root / "PillowAssistant.ico"
    icon = f"{ico},0" if ico.is_file() else f"{target},0"
    try:
        links = run(target, paths.root, icon)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("could not create shortcuts: %s", exc)
        return []
    state = _read_json(paths.state)  # re-read: the updater may have written it
    state["shortcuts_for"] = str(paths.root)
    _write_json(paths.state, state)
    log.info("created shortcuts: %s", ", ".join(links))
    return links


# -- runtime environment -----------------------------------------------------
def setup_logging(log_dir: Path, console: bool) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        log_dir / "launcher.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    if console:
        log.addHandler(logging.StreamHandler())
    log.setLevel(logging.INFO)


def setup_environment(paths: Paths) -> None:
    root = paths.root
    env = os.environ
    browsers = root / "ms-playwright"
    if browsers.is_dir():
        env.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(browsers))
    models = root / "models"
    if models.is_dir():
        env.setdefault("PILLOW_MODELS_DIR", str(models))
        env.setdefault("MODELSCOPE_CACHE", str(models / "modelscope"))
        env.setdefault("HF_HOME", str(models / "huggingface"))
    # text-to-cad: uv keeps cadgen and its managed Python inside the bundle, so a
    # prewarmed bundle (build.ps1) models offline and the folder stays portable.
    cad = root / "cad-runtime"
    env.setdefault("UV_CACHE_DIR", str(cad / "cache"))
    env.setdefault("UV_PYTHON_INSTALL_DIR", str(cad / "python"))
    env.setdefault("UV_TOOL_DIR", str(cad / "tools"))
    env.setdefault("PILLOW_CAD_APPS_DIR", str(cad / "apps"))  # OrcaSlicer etc., installed on first use
    # Mainland China mirrors for downloads the app makes itself (core/mirrors.py).
    cfg = load_config(paths)
    env.setdefault("PILLOW_MIRRORS", _mirror_mode(cfg))
    env.setdefault("PILLOW_GITHUB_MIRRORS", ",".join(_mirror_list(cfg, "github_mirrors")))
    env.setdefault("PILLOW_PYPI_MIRRORS", ",".join(_mirror_list(cfg, "pypi_mirrors")))
    # Offline-friendly defaults: never block start-up on remote lookups.
    env.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    env.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    env.setdefault("PYTHONNOUSERSITE", "1")
    extra = [str(paths.python_dir), str(paths.python_dir / "Scripts")]
    env["PATH"] = os.pathsep.join(extra + [env.get("PATH", "")])


def redirect_stdio(log_dir: Path) -> None:
    """pythonw has no console: keep prints/tracebacks in a log file."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    stream = open(log_dir / "app-output.log", "a", encoding="utf-8", buffering=1)  # noqa: SIM115
    stream.write(f"\n===== {_now()} =====\n")
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream


def _other_instance_running() -> bool:
    """Hold a named mutex for this process; report if one already existed."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateMutexW(None, False, "Local\\PillowAssistantBundle")
        globals()["_MUTEX"] = handle  # keep alive for the process lifetime
        return kernel32.GetLastError() == 183  # ERROR_ALREADY_EXISTS
    except Exception:  # noqa: BLE001
        return False


SELF_TEST_MODULES = [
    "PySide6.QtWidgets", "pydantic", "litellm", "keyring", "numpy",
    "pandas", "matplotlib", "sounddevice", "funasr", "torch", "torchaudio",
    "faster_whisper", "fitz", "openpyxl", "docx", "pptx", "mammoth",
    "imageio_ffmpeg", "playwright.async_api", "mcp",
    "pillow_assistant.app",
]


def self_test(paths: Paths) -> int:
    """Import every bundled dependency and launch the browser (CI smoke test)."""
    import importlib
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    failed = []
    for name in SELF_TEST_MODULES:
        try:
            importlib.import_module(name)
            print(f"ok    {name}")
        except Exception as exc:  # noqa: BLE001
            failed.append(name)
            print(f"FAIL  {name}: {exc!r}")
    try:
        import imageio_ffmpeg
        print("ok    ffmpeg", imageio_ffmpeg.get_ffmpeg_exe())
    except Exception as exc:  # noqa: BLE001
        failed.append("ffmpeg")
        print(f"FAIL  ffmpeg: {exc!r}")
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.set_content("<p id=x>pillow</p>")
            assert page.inner_text("#x") == "pillow"
            browser.close()
        print("ok    chromium")
    except Exception as exc:  # noqa: BLE001
        failed.append("chromium")
        print(f"FAIL  chromium: {exc!r}")
    print("version:", json.dumps(current_version(paths)))
    return 1 if failed else 0


def stamp_version(paths: Paths, sha: str, ref: str) -> None:
    _write_json(paths.version, {
        "sha": sha,
        "ref": ref,
        "channel": "build",
        "built_at": _now(),
        "requirements_sha256": requirements_hash(paths.app / "requirements.txt"),
    })


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(prog="launcher")
    parser.add_argument("--no-update", action="store_true", help="skip all update steps")
    parser.add_argument("--update-now", action="store_true",
                        help="check GitHub, download and apply an update, then exit")
    parser.add_argument("--rollback", action="store_true",
                        help="restore the previous version and stop auto-updating to the current one")
    parser.add_argument("--clear-skipped", action="store_true",
                        help="forget rolled-back versions so they may be installed again")
    parser.add_argument("--version", action="store_true", help="print the installed version")
    parser.add_argument("--create-shortcuts", action="store_true",
                        help="(re)create the desktop and Start menu shortcuts, then exit")
    parser.add_argument("--self-test", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--stamp-version", nargs=2, metavar=("SHA", "REF"), help=argparse.SUPPRESS)
    parser.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    args, app_args = parser.parse_known_args(argv)

    paths = Paths(args.root.resolve())
    if args.stamp_version:
        stamp_version(paths, *args.stamp_version)
        return 0
    if args.version:
        print(json.dumps(current_version(paths), ensure_ascii=False, indent=2))
        return 0

    log_dir = Path.home() / ".pillow" / "logs"
    interactive = (args.update_now or args.rollback or args.clear_skipped or args.self_test
                   or args.create_shortcuts)
    setup_logging(log_dir, console=interactive and sys.stdout is not None)
    setup_environment(paths)
    cfg = load_config(paths)
    if args.no_update:
        cfg["enabled"] = False

    if args.create_shortcuts:
        links = create_shortcuts(paths, force=True)
        print("\n".join(links) if links else "no shortcuts created (see launcher.log)")
        return 0 if links else 1

    if args.rollback:
        with UpdateLock(paths) as locked:
            ok = locked and rollback(paths)
        print(json.dumps(current_version(paths), ensure_ascii=False, indent=2))
        print("rolled back; the bad version will not be installed again"
              if ok else "no previous version to roll back to")
        return 0 if ok else 1

    if args.clear_skipped:
        clear_skipped(paths)
        print("rolled-back versions forgotten")
        return 0

    if args.update_now:
        cfg["enabled"] = True
        meta = check_once(paths, cfg, force=True)
        state = _read_json(paths.state)
        if meta is None and state.get("last_error"):
            print("update check failed:", state["last_error"])
            return 1
        with UpdateLock(paths) as locked:
            updated = locked and apply_staged(paths)
        print(json.dumps(current_version(paths), ensure_ascii=False, indent=2))
        print("updated" if updated else "already up to date")
        return 0

    sys.path.insert(0, str(paths.app))
    if args.self_test:
        return self_test(paths)

    redirect_stdio(log_dir)
    on_probation = False
    if not _other_instance_running():
        with UpdateLock(paths) as locked:
            if locked:
                if cfg.get("enabled", True):
                    cleanup(paths)
                    try:
                        apply_staged(paths)
                    except Exception:  # noqa: BLE001 - never block start-up
                        log.exception("applying update failed")
                try:
                    on_probation = begin_probation_start(paths)
                except Exception:  # noqa: BLE001
                    log.exception("probation check failed")
    start_background_checker(paths, cfg)
    if os.name == "nt" and cfg.get("desktop_shortcut", True):
        threading.Thread(target=create_shortcuts, args=(paths,), name="pillow-shortcuts",
                         daemon=True).start()

    sys.argv = [sys.argv[0]] + app_args

    def app_main():
        from pillow_assistant.app import main
        return main()

    return run_app(paths, app_main, on_probation, relaunch=_relaunch_previous)


if __name__ == "__main__":
    raise SystemExit(main())

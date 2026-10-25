"""Download mirrors for users in mainland China, where GitHub and PyPI are slow or blocked.

``PILLOW_MIRRORS`` picks the order: ``auto`` (default) tries the original site
first and the mirrors after it, ``cn`` tries the mirrors first, ``off`` never
uses them.  ``PILLOW_GITHUB_MIRRORS`` and ``PILLOW_PYPI_MIRRORS`` (comma
separated) replace the default lists.  The Windows launcher sets all three from
``update.json``.

GitHub mirrors are public proxies that are put in front of the original URL
(``https://ghfast.top/https://github.com/...``).  They are third parties, so
whatever comes through one must be checked against a hash from GitHub itself
(callers do this; see ``cad_apps.install``).  Only the standard library is used.
"""

from __future__ import annotations

import os
from typing import Iterator, Optional

GITHUB_MIRRORS = ("https://ghfast.top/", "https://gh-proxy.com/", "https://ghproxy.net/")
PYPI_MIRRORS = ("https://pypi.tuna.tsinghua.edu.cn/simple", "https://mirrors.aliyun.com/pypi/simple/")
PYTHON_BUILDS = "https://github.com/astral-sh/python-build-standalone/releases/download"
_PROXIED = ("https://github.com/", "https://raw.githubusercontent.com/", "https://objects.githubusercontent.com/",
            "https://codeload.github.com/")


def mode() -> str:
    value = os.environ.get("PILLOW_MIRRORS", "").strip().lower()
    return value if value in ("auto", "cn", "off") else "auto"


def _list(env: str, default: tuple[str, ...]) -> list[str]:
    raw = os.environ.get(env)
    if raw is None:
        return list(default)
    return [p.strip() for p in raw.split(",") if p.strip().startswith("https://")]


def github_mirrors() -> list[str]:
    return [] if mode() == "off" else [m if m.endswith("/") else m + "/" for m in
                                       _list("PILLOW_GITHUB_MIRRORS", GITHUB_MIRRORS)]


def pypi_mirrors() -> list[str]:
    return [] if mode() == "off" else _list("PILLOW_PYPI_MIRRORS", PYPI_MIRRORS)


def github_urls(url: str, prefer_mirrors: bool = False) -> list[tuple[str, bool]]:
    """``(url, through_mirror)`` in the order to try them for a GitHub download URL.

    ``prefer_mirrors``: GitHub already failed to answer, so try the mirrors first.
    """
    if not url.startswith(_PROXIED):
        return [(url, False)]
    via = [(m + url, True) for m in github_mirrors()]
    return via + [(url, False)] if via and (mode() == "cn" or prefer_mirrors) else [(url, False)] + via


def pypi_indexes() -> list[Optional[str]]:
    """Package indexes to try in order; ``None`` is the configured default (PyPI)."""
    mirrors = pypi_mirrors()
    return mirrors + [None] if mode() == "cn" else [None] + mirrors


def uv_environments(env: dict) -> Iterator[dict]:
    """``env`` as given, then once per mirror with uv's package index and Python downloads moved there."""
    order = pypi_indexes()
    gh = github_mirrors()
    for i, index in enumerate(order):
        if index is None:
            yield env
            continue
        e = dict(env)
        e.setdefault("UV_DEFAULT_INDEX", index)
        if gh:
            # uv checks every managed Python download against its own hash list.
            e.setdefault("UV_PYTHON_INSTALL_MIRROR", gh[min(i, len(gh) - 1)] + PYTHON_BUILDS)
        yield e

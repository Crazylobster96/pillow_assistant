"""R3 tests (non-GUI): UndoManager + file_write undo (overwrite restore / new delete).

Run: ``python tests/test_r3.py``.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pillow_assistant.core.tools.base import ToolContext
from pillow_assistant.core.tools.builtin import build_default_registry
from pillow_assistant.core.undo import UndoManager

PASS, FAIL = 0, 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}")


def test_undo_manager():
    print("UndoManager")
    um = UndoManager(ttl=5.0)
    state = {"v": 0}
    token = um.register("set v=1", lambda: state.__setitem__("v", 1))
    check("pending has token", token in um.pending())
    check("undo runs fn", um.undo(token) is True and state["v"] == 1)
    check("second undo is no-op", um.undo(token) is False)


def test_undo_expiry():
    print("UndoManager expiry")
    um = UndoManager(ttl=0.05)
    token = um.register("x", lambda: None)
    time.sleep(0.1)
    check("expired -> gone", token not in um.pending())
    check("undo after expiry fails", um.undo(token) is False)


def test_file_write_undo_overwrite():
    print("file_write undo: overwrite restores old content")
    reg = build_default_registry()
    um = UndoManager()
    with tempfile.TemporaryDirectory() as d:
        ws = Path(d)
        (ws / "a.txt").write_text("OLD", "utf-8")
        ctx = ToolContext(workspace=ws, undo_manager=um)
        r = asyncio.run(reg.dispatch("file_write", {"path": "a.txt", "content": "NEW"}, ctx))
        check("overwrote", (ws / "a.txt").read_text("utf-8") == "NEW")
        check("undo token issued", r.undo_token is not None and "覆盖" in r.undo_label)
        check("undo restores OLD", um.undo(r.undo_token) and (ws / "a.txt").read_text("utf-8") == "OLD")


def test_file_write_undo_new():
    print("file_write undo: new file is removed")
    reg = build_default_registry()
    um = UndoManager()
    with tempfile.TemporaryDirectory() as d:
        ws = Path(d)
        ctx = ToolContext(workspace=ws, undo_manager=um)
        r = asyncio.run(reg.dispatch("file_write", {"path": "new.txt", "content": "X"}, ctx))
        check("created", (ws / "new.txt").exists() and "新建" in r.undo_label)
        check("undo deletes", um.undo(r.undo_token) and not (ws / "new.txt").exists())


def test_no_undo_without_manager():
    print("no undo manager -> no token (back-compat)")
    reg = build_default_registry()
    with tempfile.TemporaryDirectory() as d:
        ctx = ToolContext(workspace=Path(d))
        r = asyncio.run(reg.dispatch("file_write", {"path": "a.txt", "content": "x"}, ctx))
        check("ok, no undo token", r.ok and r.undo_token is None)


if __name__ == "__main__":
    for t in (test_undo_manager, test_undo_expiry, test_file_write_undo_overwrite,
              test_file_write_undo_new, test_no_undo_without_manager):
        t()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)

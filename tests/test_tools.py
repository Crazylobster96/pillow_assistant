"""T0 tool-system tests (non-GUI): registry, run_python tool, file tools.

Run: ``python tests/test_tools.py``. The python tool runs a real subprocess.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pillow_assistant.core.tools.base import ToolContext
from pillow_assistant.core.tools.builtin import build_default_registry

PASS, FAIL = 0, 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}")


def test_registry():
    print("registry")
    reg = build_default_registry()
    names = set(reg.names())
    check("has all tools", {"run_python", "file_read", "file_write", "file_list", "http_request", "run_cli"} <= names)
    schemas = reg.schemas()
    check("schemas are openai functions", all(s["type"] == "function" and "name" in s["function"] for s in schemas))


def test_http_tool():
    print("http_request: SSRF / scheme / allowlist guards")
    reg = build_default_registry()
    with tempfile.TemporaryDirectory() as d:
        ctx = ToolContext(workspace=Path(d))
        check("bad scheme rejected", not asyncio.run(reg.dispatch("http_request", {"url": "ftp://x/y"}, ctx)).ok)
        check("loopback blocked", not asyncio.run(reg.dispatch("http_request", {"url": "http://127.0.0.1/"}, ctx)).ok)
        check("localhost blocked", not asyncio.run(reg.dispatch("http_request", {"url": "http://localhost:8080/"}, ctx)).ok)
        ctx2 = ToolContext(workspace=Path(d), http_allowlist=["example.com"])
        r = asyncio.run(reg.dispatch("http_request", {"url": "https://evil.test/"}, ctx2))
        check("allowlist blocks others", not r.ok and "白名单" in r.text)


def test_cli_tool():
    print("run_cli: denylist + safe run + disable")
    reg = build_default_registry()
    with tempfile.TemporaryDirectory() as d:
        ctx = ToolContext(workspace=Path(d))
        bad = asyncio.run(reg.dispatch("run_cli", {"command": "rm -rf /"}, ctx))
        check("dangerous blocked", not bad.ok and "拦截" in bad.text)
        good = asyncio.run(reg.dispatch("run_cli", {"command": "echo hello123"}, ctx))
        check("safe command runs", good.ok and "hello123" in good.text)
        off = ToolContext(workspace=Path(d), allow_cli=False)
        check("disabled gate", not asyncio.run(reg.dispatch("run_cli", {"command": "echo x"}, off)).ok)


def test_python_tool():
    print("run_python via registry")
    reg = build_default_registry()
    with tempfile.TemporaryDirectory() as d:
        ctx = ToolContext(workspace=Path(d))
        r = asyncio.run(reg.dispatch("run_python", {"code": "print(2+2)"}, ctx))
        check("ok", r.ok)
        check("stdout 4", "4" in r.text)


def test_file_tools():
    print("file_write / file_read / file_list")
    reg = build_default_registry()
    with tempfile.TemporaryDirectory() as d:
        ctx = ToolContext(workspace=Path(d))
        w = asyncio.run(reg.dispatch("file_write", {"path": "report.md", "content": "# 标题\n内容"}, ctx))
        check("write ok", w.ok and "report.md" in w.artifacts)
        check("file on disk", (Path(d) / "report.md").read_text("utf-8").startswith("# 标题"))
        r = asyncio.run(reg.dispatch("file_read", {"path": "report.md"}, ctx))
        check("read back", r.ok and "内容" in r.text)
        ls = asyncio.run(reg.dispatch("file_list", {"path": "."}, ctx))
        check("list shows file", ls.ok and "report.md" in ls.text)


def test_file_safety():
    print("file path safety")
    reg = build_default_registry()
    with tempfile.TemporaryDirectory() as d:
        ctx = ToolContext(workspace=Path(d) / "ws")
        (Path(d) / "ws").mkdir()
        outside = str(Path(d) / "secret.txt")
        Path(outside).write_text("top secret", "utf-8")
        # write outside workspace -> denied
        w = asyncio.run(reg.dispatch("file_write", {"path": outside, "content": "x"}, ctx))
        check("write outside denied", not w.ok)
        # read outside workspace and not referenced -> denied
        r = asyncio.run(reg.dispatch("file_read", {"path": outside}, ctx))
        check("read outside denied", not r.ok)
        # but a referenced outside file is readable
        ctx2 = ToolContext(workspace=Path(d) / "ws", references=[outside])
        r2 = asyncio.run(reg.dispatch("file_read", {"path": outside}, ctx2))
        check("referenced file readable", r2.ok and "top secret" in r2.text)


if __name__ == "__main__":
    for t in (test_registry, test_python_tool, test_file_tools, test_file_safety,
              test_http_tool, test_cli_tool):
        t()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)

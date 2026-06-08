"""T3 tests (non-GUI): model routing, audit log, browser tool guards/degradation.

Run: ``python tests/test_t3.py``. Browser is tested for guards + graceful
degradation (Playwright not required).
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pillow_assistant.core.model_router import select_model
from pillow_assistant.core.observability import AuditLog
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


def test_model_router():
    print("select_model")
    cfgs = [
        {"display_name": "GPT", "model_type": "llm"},
        {"display_name": "Claude-V", "model_type": "vlm"},
    ]
    check("vision -> vlm", select_model(cfgs, "GPT", want_vision=True) == "Claude-V")
    check("no vision -> chosen", select_model(cfgs, "GPT", want_vision=False) == "GPT")
    check("vision but chosen is vlm -> keep", select_model(cfgs, "Claude-V", want_vision=True) == "Claude-V")
    check("no vlm -> default", select_model([{"display_name": "GPT", "model_type": "llm"}], "GPT", True) == "GPT")
    check("bad default -> first", select_model(cfgs, "Nope", False) == "GPT")
    check("empty configs", select_model([], "X", False) == "X")


def test_audit_log():
    print("AuditLog")
    with tempfile.TemporaryDirectory() as d:
        a = AuditLog(Path(d) / "sub" / "audit.jsonl")
        a.run_start("做个图表")
        a.tool_call("run_python", {"code": "print(1)"}, True, 12, 5)
        a.run_end(42)
        lines = (Path(d) / "sub" / "audit.jsonl").read_text("utf-8").strip().splitlines()
        recs = [json.loads(x) for x in lines]
        check("three records", len(recs) == 3)
        check("kinds", [r["kind"] for r in recs] == ["run_start", "tool", "run_end"])
        check("tool record", recs[1]["name"] == "run_python" and recs[1]["ok"] is True and recs[1]["ms"] == 12)


def test_browser_tool():
    print("browser_read guards / degradation")
    reg = build_default_registry()
    check("registered", "browser_read" in reg.names())
    with tempfile.TemporaryDirectory() as d:
        ctx = ToolContext(workspace=Path(d))
        check("bad scheme", not asyncio.run(reg.dispatch("browser_read", {"url": "ftp://x"}, ctx)).ok)
        check("loopback blocked", not asyncio.run(reg.dispatch("browser_read", {"url": "http://127.0.0.1/"}, ctx)).ok)
        # public URL: either playwright missing (degraded message) or a real attempt;
        # both return a ToolResult without raising.
        r = asyncio.run(reg.dispatch("browser_read", {"url": "https://example.com/"}, ctx))
        check("returns a result", isinstance(r.text, str))


if __name__ == "__main__":
    for t in (test_model_router, test_audit_log, test_browser_tool):
        t()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)

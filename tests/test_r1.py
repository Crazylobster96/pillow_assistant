"""R1 unit/integration tests (non-GUI). Run: ``python tests/test_r1.py``.

The sandbox tests actually execute subprocesses, so they run real code. The
Agent-loop test uses a fake model that asks for one tool call then answers.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pillow_assistant.contracts import EventType
from pillow_assistant.core import llm
from pillow_assistant.core.agent.loop import ToolLoopAgent
from pillow_assistant.core.project_manager import ProjectManager, derive_name
from pillow_assistant.core.session import Session
from pillow_assistant.core.tools.sandbox import Sandbox
from storage.projects import ProjectStore

PASS, FAIL = 0, 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}")


def test_sandbox_basic():
    print("sandbox: run + stdout")
    with tempfile.TemporaryDirectory() as d:
        sb = Sandbox(d)
        r = sb.run_python("print(2 + 2)")
        check("returncode 0", r.returncode == 0)
        check("stdout captured", r.stdout.strip() == "4")


def test_sandbox_artifacts():
    print("sandbox: artifact in workspace")
    with tempfile.TemporaryDirectory() as d:
        sb = Sandbox(d)
        r = sb.run_python("open('out.txt','w').write('hello')")
        check("artifact listed", "out.txt" in r.artifacts)
        check("file really written", (Path(d) / "out.txt").read_text() == "hello")
        check("runner cleaned up", not (Path(d) / ".pillow_run.py").exists())


def test_sandbox_timeout():
    print("sandbox: timeout kill")
    with tempfile.TemporaryDirectory() as d:
        sb = Sandbox(d)
        r = sb.run_python("import time; time.sleep(5)", timeout=1)
        check("timed_out flagged", r.timed_out is True)


def test_sandbox_network_softblock():
    print("sandbox: network soft-block")
    with tempfile.TemporaryDirectory() as d:
        sb = Sandbox(d)
        r = sb.run_python("import socket; socket.socket()")
        check("socket blocked", r.returncode != 0 and "disabled" in r.stderr)


def test_projects():
    print("projects: create / get / list / session-bound resolve")
    with tempfile.TemporaryDirectory() as d:
        store = ProjectStore(d)
        p = store.create("我的任务")
        check("workspace exists", p.workspace.is_dir())
        check("get by id", store.get(p.id).name == "我的任务")
        check("list has one", len(store.list()) == 1)

        session = Session()
        pm = ProjectManager(store, session)
        first = pm.resolve("把 CSV 画成折线图")
        check("session bound", session.project_id == first.id)
        check("named from prompt", first.name == derive_name("把 CSV 画成折线图"))
        again = pm.resolve("继续，加上标题")
        check("same project continued", again.id == first.id)
        check("no duplicate project", len(store.list()) == 2)  # the manual one + this one


def test_derive_name():
    print("project name heuristic")
    check("truncates long", derive_name("a" * 50).endswith("…"))
    check("first line only", derive_name("标题\n第二行") == "标题")
    check("empty fallback", derive_name("   ") == "未命名任务")


def test_agent_loop_with_tool():
    print("agent loop: one tool call then final answer")

    # Fake model: first turn requests run_python, second turn answers.
    calls = {"n": 0}

    async def fake_complete(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return llm.ToolTurn(content=None, tool_calls=[
                llm.ToolCall(id="c1", name="run_python", arguments='{"code": "print(6*7)"}')
            ])
        return llm.ToolTurn(content="答案是 42。", tool_calls=[])

    original = llm.complete_with_tools
    llm.complete_with_tools = fake_complete
    try:
        with tempfile.TemporaryDirectory() as d:
            from pillow_assistant.core.tools.base import ToolContext
            from pillow_assistant.core.tools.builtin import build_default_registry
            registry = build_default_registry()
            ctx = ToolContext(workspace=Path(d))
            agent = ToolLoopAgent(cfg={"provider": "OpenAI", "model": "x"}, api_key=None,
                                  registry=registry, ctx=ctx)
            events = []

            async def emit(ev):
                events.append(ev)

            async def run():
                return await agent.run(prompt="6 乘 7 等于多少？", emit=emit, request_id="r1")

            final = asyncio.run(run())
            types = [e.type for e in events]
            tokens = "".join(e.text for e in events if e.type == EventType.TOKEN)
            check("two model turns", calls["n"] == 2)
            check("tool actually ran (42 in output)", "42" in tokens)
            check("final answer emitted", "答案是 42" in tokens)
            check("ends with DONE", types[-1] == EventType.DONE)
            check("returns final text", "42" in final)
    finally:
        llm.complete_with_tools = original


if __name__ == "__main__":
    for t in (test_sandbox_basic, test_sandbox_artifacts, test_sandbox_timeout,
              test_sandbox_network_softblock, test_projects, test_derive_name,
              test_agent_loop_with_tool):
        t()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)

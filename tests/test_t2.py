"""T2 tests (non-GUI): Skill library + apply_skill tool, MCP config + McpTool.

Run: ``python tests/test_t2.py``. MCP uses a fake client (no real server / SDK).
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pillow_assistant.core.skills import SkillStore, parse_skill_md
from pillow_assistant.core.tools.base import ToolContext
from pillow_assistant.core.tools.builtin.skill_tool import SkillTool
from pillow_assistant.core.tools.mcp import McpServerConfig, McpTool, load_mcp_tools, read_mcp_configs

PASS, FAIL = 0, 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}")


def test_skill_parse():
    print("skill SKILL.md parsing")
    md = "---\nname: weekly\ndescription: 周报技能\ntools: run_python, file_write\n---\n请整理周报。\n第二行。"
    s = parse_skill_md(md, "fallback")
    check("name", s.name == "weekly")
    check("description", s.description == "周报技能")
    check("tools", s.tools == ["run_python", "file_write"])
    check("instructions body", s.instructions.startswith("请整理周报"))
    check("no-frontmatter fallback", parse_skill_md("just text", "fb").name == "fb")


def test_skill_store_and_tool():
    print("SkillStore + apply_skill tool")
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        (base / "weekly").mkdir()
        (base / "weekly" / "SKILL.md").write_text("---\nname: weekly\ndescription: 周报\n---\n步骤一二三", "utf-8")
        (base / "summarize.md").write_text("---\ndescription: 摘要\n---\n做摘要", "utf-8")
        skills = SkillStore(base).load()
        check("loaded two", len(skills) == 2)
        names = {s.name for s in skills}
        check("names", "weekly" in names and "summarize" in names)

        tool = SkillTool(skills)
        check("desc lists skills", "weekly" in tool.description and "summarize" in tool.description)
        ctx = ToolContext(workspace=base)
        r = asyncio.run(tool({"name": "weekly"}, ctx))
        check("apply returns instructions", r.ok and "步骤一二三" in r.text)
        bad = asyncio.run(tool({"name": "ghost"}, ctx))
        check("unknown skill", not bad.ok)


def test_mcp_config():
    print("read_mcp_configs")
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "mcp.json"
        f.write_text(json.dumps({"servers": [
            {"name": "fs", "command": "npx", "args": ["-y", "server-fs", "/tmp"]},
            {"bad": "no name/command"},
        ]}), "utf-8")
        cfgs = read_mcp_configs(f)
        check("one valid server", len(cfgs) == 1 and cfgs[0].name == "fs")
        check("args parsed", cfgs[0].args == ["-y", "server-fs", "/tmp"])
        check("missing file -> empty", read_mcp_configs(Path(d) / "nope.json") == [])


def test_mcp_tool_and_load():
    print("McpTool + load_mcp_tools (fake client)")

    class FakeClient:
        def __init__(self, cfg=None):
            self.cfg = cfg

        async def list_tools(self):
            return [{"name": "echo", "description": "回显", "inputSchema": {"type": "object", "properties": {}}}]

        async def call(self, tool_name, args):
            return f"called {tool_name} with {args}"

    tool = McpTool(FakeClient(), "srv", "echo", "回显工具", {"type": "object", "properties": {}})
    check("name format", tool.name == "mcp:srv:echo")
    r = asyncio.run(tool({"x": 1}, ToolContext(workspace=Path("."))))
    check("dispatch ok", r.ok and "called echo" in r.text)

    # load_mcp_tools wraps each discovered tool; patch McpClient with the fake
    import pillow_assistant.core.tools.mcp as mcpmod
    orig = mcpmod.McpClient
    mcpmod.McpClient = FakeClient
    try:
        tools = asyncio.run(load_mcp_tools([McpServerConfig(name="srv", command="x")]))
        check("loaded one wrapper", len(tools) == 1 and tools[0].name == "mcp:srv:echo")
    finally:
        mcpmod.McpClient = orig


if __name__ == "__main__":
    for t in (test_skill_parse, test_skill_store_and_tool, test_mcp_config, test_mcp_tool_and_load):
        t()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)

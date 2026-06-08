"""MCP client (T2): mount external MCP servers' tools into the registry.

Reads server configs from ``~/.pillow/mcp_servers.json`` (a list, or {"servers":[...]})::

    [{"name": "fs", "command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path"]}]

Each external tool is wrapped as a local Tool named ``mcp:<server>:<tool>``.
Requires the optional ``mcp`` SDK; without it, ``load_mcp_tools`` returns [].
Connections are made per call (simple lifecycle) — fine for occasional use.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from pillow_assistant.core.tools.base import Permission, ToolContext, ToolResult


@dataclass
class McpServerConfig:
    name: str
    command: str
    args: list = field(default_factory=list)
    env: dict = field(default_factory=dict)


def read_mcp_configs(path: str | Path) -> list[McpServerConfig]:
    p = Path(path)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text("utf-8"))
    except (ValueError, OSError):
        return []
    servers = data.get("servers") if isinstance(data, dict) else data
    out: list[McpServerConfig] = []
    for s in (servers or []):
        if isinstance(s, dict) and s.get("name") and s.get("command"):
            out.append(McpServerConfig(
                name=s["name"], command=s["command"],
                args=list(s.get("args") or []), env=dict(s.get("env") or {}),
            ))
    return out


def _content_to_text(result) -> str:
    parts = []
    for block in (getattr(result, "content", None) or []):
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    from pillow_assistant.core.i18n import t
    return "\n".join(parts) or t("tool.mcp.no_text")


class McpClient:
    def __init__(self, cfg: McpServerConfig) -> None:
        self.cfg = cfg

    def _imports(self):
        from mcp import ClientSession, StdioServerParameters  # optional dependency
        from mcp.client.stdio import stdio_client
        return ClientSession, StdioServerParameters, stdio_client

    async def list_tools(self) -> list[dict]:
        ClientSession, StdioServerParameters, stdio_client = self._imports()
        params = StdioServerParameters(command=self.cfg.command, args=self.cfg.args, env=self.cfg.env or None)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                resp = await session.list_tools()
                return [
                    {"name": t.name, "description": getattr(t, "description", "") or "",
                     "inputSchema": getattr(t, "inputSchema", None)}
                    for t in resp.tools
                ]

    async def call(self, tool_name: str, args: dict) -> str:
        ClientSession, StdioServerParameters, stdio_client = self._imports()
        params = StdioServerParameters(command=self.cfg.command, args=self.cfg.args, env=self.cfg.env or None)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, args)
                return _content_to_text(result)


class McpTool:
    permission = Permission.NETWORK

    def __init__(self, client: McpClient, server: str, tool_name: str, description: str, schema) -> None:
        from pillow_assistant.core.i18n import t
        self.name = f"mcp:{server}:{tool_name}"
        self.description = description or t("tool.mcp.desc", tool=tool_name, server=server)
        self.parameters = schema or {"type": "object", "properties": {}}
        self._client = client
        self._tool = tool_name

    async def __call__(self, args: dict, ctx: ToolContext) -> ToolResult:
        try:
            text = await self._client.call(self._tool, args)
            return ToolResult(ok=True, text=text)
        except Exception as exc:  # noqa: BLE001
            from pillow_assistant.core.i18n import t
            return ToolResult(ok=False, text=t("tool.mcp.failed", err=exc))


async def load_mcp_tools(configs: list[McpServerConfig]) -> list[McpTool]:
    tools: list[McpTool] = []
    for cfg in configs:
        client = McpClient(cfg)
        try:
            specs = await client.list_tools()
        except Exception:
            continue  # SDK missing / server unavailable -> skip this server
        for spec in specs:
            tools.append(McpTool(client, cfg.name, spec["name"], spec.get("description", ""), spec.get("inputSchema")))
    return tools

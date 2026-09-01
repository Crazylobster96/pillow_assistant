"""http_request tool (T1): GET/POST with SSRF protection + optional allowlist."""

from __future__ import annotations

from pillow_assistant.capabilities.tool_manifest import manifest_tool

import asyncio
import ipaddress
import socket
import urllib.error
import urllib.parse
import urllib.request

from pillow_assistant.core.i18n import t
from pillow_assistant.core.tools.base import Permission, ToolContext, ToolResult

MAX_BODY = 64 * 1024
TIMEOUT = 20


def _host_is_private(host: str) -> bool:
    """True if the host resolves to a private/loopback/link-local/reserved IP
    (blocks SSRF to internal services) or cannot be resolved."""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return True
    for info in infos:
        ip = info[4][0]
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved or addr.is_multicast:
            return True
    return False


@manifest_tool
class HttpRequestTool:
    name = "http_request"
    permission = Permission.NETWORK

    async def __call__(self, args: dict, ctx: ToolContext) -> ToolResult:
        url = (args.get("url") or "").strip()
        method = (args.get("method") or "GET").upper()
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return ToolResult(ok=False, text=t("tool.http.scheme"))
        host = parsed.hostname or ""
        allow = getattr(ctx, "http_allowlist", None)
        if allow and not any(host == a or host.endswith("." + a) for a in allow):
            return ToolResult(ok=False, text=t("tool.http.not_allowed", host=host))
        if _host_is_private(host):
            return ToolResult(ok=False, text=t("tool.http.private", host=host))

        data = None
        if method == "POST" and args.get("body") is not None:
            data = str(args.get("body")).encode("utf-8")
        request = urllib.request.Request(url, data=data, method=method, headers=args.get("headers") or {})

        def do():
            try:
                with urllib.request.urlopen(request, timeout=TIMEOUT) as resp:
                    return resp.status, resp.read(MAX_BODY + 1)
            except urllib.error.HTTPError as exc:
                return exc.code, exc.read(MAX_BODY + 1)
            except Exception as exc:  # noqa: BLE001
                return None, str(exc).encode("utf-8")

        loop = asyncio.get_event_loop()
        code, body = await loop.run_in_executor(None, do)
        if code is None:
            return ToolResult(ok=False, text=t("tool.http.failed", err=body.decode("utf-8", "replace")))
        text = body[:MAX_BODY].decode("utf-8", "replace")
        trunc = t("tool.truncated") if len(body) > MAX_BODY else ""
        return ToolResult(ok=(200 <= code < 400), text=f"HTTP {code}\n{text}{trunc}")

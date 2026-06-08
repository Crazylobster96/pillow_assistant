"""browser_read tool (T3): open a page in a headless browser (Playwright) and
return its visible text — for JS-rendered pages that http_request can't read."""

from __future__ import annotations

import urllib.parse

from pillow_assistant.core.i18n import t
from pillow_assistant.core.tools.base import Permission, ToolContext, ToolResult
from pillow_assistant.core.tools.builtin.http_tool import _host_is_private

MAX_TEXT = 60 * 1024
TIMEOUT_MS = 20000


class BrowserReadTool:
    name = "browser_read"
    permission = Permission.NETWORK
    description = t("tool.browser.desc")
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": t("tool.browser.url")},
            "selector": {"type": "string", "description": t("tool.browser.selector")},
        },
        "required": ["url"],
    }

    async def __call__(self, args: dict, ctx: ToolContext) -> ToolResult:
        url = (args.get("url") or "").strip()
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return ToolResult(ok=False, text=t("tool.http.scheme"))
        if _host_is_private(parsed.hostname or ""):
            return ToolResult(ok=False, text=t("tool.http.private", host=parsed.hostname))
        try:
            from playwright.async_api import async_playwright  # optional dependency
        except ImportError:
            return ToolResult(ok=False, text=t("tool.browser.need_pkg"))

        selector = args.get("selector") or "body"
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch()
                try:
                    page = await browser.new_page()
                    await page.goto(url, timeout=TIMEOUT_MS, wait_until="domcontentloaded")
                    text = await page.inner_text(selector)
                finally:
                    await browser.close()
        except Exception as exc:  # noqa: BLE001
            return ToolResult(ok=False, text=t("tool.browser.failed", err=exc))
        if len(text) > MAX_TEXT:
            text = text[:MAX_TEXT] + t("tool.truncated")
        return ToolResult(ok=True, text=text)

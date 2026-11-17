"""MCP (Model Context Protocol) servers become Zolt tools.

Configure in data/mcp.json, e.g.
{"servers": {"filesystem": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "C:/Users/me/Documents"]}}}
Each server's tools appear as  mcp_<server>_<tool>.  Tools not marked read-only ask her first.

Each server lives in its own long-running task (MCP's stdio client must be opened
and closed in the same task).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re

from .memory import DATA_DIR

log = logging.getLogger("zolt.mcp")
CONFIG_FILE = DATA_DIR / "mcp.json"
_stop = asyncio.Event()
_tasks: list[asyncio.Task] = []


def _field(obj, *names, default=None):
    for n in names:  # mcp 2.x uses snake_case, 1.x camelCase
        value = getattr(obj, n, None)
        if value is not None:
            return value
    return default


async def _serve(server_name: str, cfg: dict, ready: asyncio.Future) -> None:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    from .tools import DECLINED, ToolContext, register_tools

    try:
        params = StdioServerParameters(command=cfg["command"], args=cfg.get("args", []), env=cfg.get("env"))
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            listed = await session.list_tools()
            new, names = [], []
            for tool in listed.tools:
                name = re.sub(r"[^a-zA-Z0-9_-]", "_", f"mcp_{server_name}_{tool.name}")[:64]
                read_only = bool(_field(_field(tool, "annotations"), "read_only_hint", "readOnlyHint", default=False))

                def make(tool_name=tool.name, read_only=read_only, label=f"{server_name}/{tool.name}"):
                    async def handler(inp: dict, ctx: ToolContext) -> str:
                        if not read_only and not await ctx.confirm(
                                f"🔌 MCP tool চালাব?\n{label}\n{json.dumps(inp, ensure_ascii=False)[:500]}"):
                            return DECLINED
                        result = await session.call_tool(tool_name, inp)
                        texts = [getattr(c, "text", "") or f"[{getattr(c, 'type', 'content')}]" for c in result.content]
                        failed = _field(result, "is_error", "isError", default=False)
                        return ("ERROR: " if failed else "") + "\n".join(texts)[:20000]
                    return handler

                schema = dict(_field(tool, "input_schema", "inputSchema", default=None) or {"type": "object"})
                schema.setdefault("type", "object")
                schema.setdefault("properties", {})
                schema.setdefault("required", [])
                new.append(({"name": name, "description": (tool.description or tool.name)[:1000],
                             "input_schema": schema}, make()))
                names.append(name)
            register_tools(new, strict=False)
            log.info("MCP server %s: %d tools", server_name, len(new))
            ready.set_result(names)
            await _stop.wait()
    except Exception as e:
        log.warning("MCP server %s failed: %s", server_name, e)
        if not ready.done():
            ready.set_result([])


async def start() -> list[str]:
    """Connect every configured server and register its tools. Returns loaded tool names."""
    try:
        servers = json.loads(CONFIG_FILE.read_text(encoding="utf-8")).get("servers", {})
    except (OSError, ValueError):
        return []
    _stop.clear()
    loaded: list[str] = []
    for server_name, cfg in servers.items():
        ready: asyncio.Future = asyncio.get_running_loop().create_future()
        _tasks.append(asyncio.create_task(_serve(server_name, cfg, ready)))
        try:
            loaded += await asyncio.wait_for(ready, 60)
        except asyncio.TimeoutError:
            log.warning("MCP server %s did not start within 60s", server_name)
    return loaded


async def stop() -> None:
    _stop.set()
    if _tasks:
        await asyncio.wait(_tasks, timeout=10)
        _tasks.clear()

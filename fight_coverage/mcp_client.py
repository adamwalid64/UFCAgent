"""Inspect the real MCP tools/list contract before calling a configured tool."""
from __future__ import annotations

import asyncio
import json
import os
from contextlib import AsyncExitStack
from datetime import timedelta


class MCPClient:
    def __init__(self, config, timeout=30):
        self.config, self.timeout = config, timeout

    async def _run(self, video_id=None):
        import anyio
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        from mcp.client.streamable_http import streamable_http_client
        from jsonschema import validate

        cfg = self.config
        with anyio.fail_after(self.timeout):
            async with AsyncExitStack() as stack:
                if cfg.transport == "stdio":
                    if not cfg.command:
                        raise ValueError("MCP stdio command is not configured")
                    # Never inherit API keys unnecessarily; SDK supplies its usual safe environment.
                    env = {key: os.environ[source] for key, source in cfg.env.items()}
                    # Server stderr is untrusted and may echo credentials/transcripts.
                    errors = stack.enter_context(open(os.devnull, "w"))
                    streams = await stack.enter_async_context(stdio_client(
                        StdioServerParameters(command=cfg.command, args=cfg.args, env=env), errlog=errors))
                else:
                    import httpx
                    url = os.environ[cfg.url_env]
                    if not url.startswith(("https://", "http://localhost:", "http://127.0.0.1:")):
                        raise ValueError("Remote MCP requires HTTPS")
                    headers = {"Authorization": "Bearer " + os.environ[cfg.token_env]} if cfg.token_env else {}
                    client = await stack.enter_async_context(httpx.AsyncClient(headers=headers, timeout=self.timeout))
                    streams = await stack.enter_async_context(streamable_http_client(url, http_client=client))
                session = await stack.enter_async_context(ClientSession(
                    streams[0], streams[1], read_timeout_seconds=timedelta(seconds=self.timeout)))
                await session.initialize()
                inventory, cursor, seen = [], None, set()
                for _ in range(20):
                    listing = await session.list_tools(cursor=cursor)
                    inventory.extend(t.model_dump(mode="json") for t in listing.tools)
                    cursor = listing.nextCursor
                    if not cursor:
                        break
                    if cursor in seen:
                        raise ValueError("Repeated MCP tools/list cursor")
                    seen.add(cursor)
                else:
                    raise ValueError("MCP tool inventory exceeded page limit")
                if video_id is None:
                    return inventory
                matches = [tool for tool in inventory if tool["name"] == cfg.tool]
                if len(matches) != 1:
                    raise ValueError("Configured transcript tool not present in tools/list; run inspect-mcp")
                arguments = {**cfg.arguments, cfg.video_argument:
                             "https://www.youtube.com/watch?v=" + video_id if cfg.video_as_url else video_id}
                validate(arguments, matches[0]["inputSchema"])
                result = await session.call_tool(cfg.tool, arguments)
                if result.isError:
                    raise ValueError("MCP transcript tool returned an error")
                payload = result.structuredContent
                if payload is None:
                    texts = [block.text for block in result.content if block.type == "text"]
                    if not texts:
                        raise ValueError("MCP transcript response has no text")
                    payload = "\n".join(texts)
                    try:
                        payload = json.loads(payload)
                    except ValueError:
                        pass
                for key in cfg.result_path:
                    payload = payload[key]
                return payload

    def inspect(self):
        return asyncio.run(self._run())

    def fetch(self, video_id):
        return asyncio.run(self._run(video_id))

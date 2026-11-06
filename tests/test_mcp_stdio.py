"""stdio transport 联调测试 —— 对应 MCP 客户端（如 WorkBuddy）直接拉起子进程的模式。

用法:
    uv run python tests/test_mcp_stdio.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

PROJECT_ROOT = Path(__file__).resolve().parent.parent


async def run() -> int:
    params = StdioServerParameters(
        command=str(PROJECT_ROOT / ".venv" / "bin" / "wecom-mcp-server"),
        args=[],
        env={**os.environ, "WECOM_MCP_TRANSPORT": "stdio", "PYTHONUNBUFFERED": "1"},
        cwd=str(PROJECT_ROOT),
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            print(f"[1] initialize  -> server={init.serverInfo.name} "
                  f"version={init.serverInfo.version} proto={init.protocolVersion}")

            tools = await session.list_tools()
            print(f"[2] tools/list  -> {len(tools.tools)} 个工具 (stdio 模式不注册 HTTP-only 工具)")

            for name, args in (
                ("search_approval_records", {"with_stats": True}),
                ("list_approval_templates", {}),
            ):
                res = await session.call_tool(name, args)
                text = "\n".join(getattr(c, "text", "") for c in (res.content or []))
                print(f"[3] call {name:28s} -> isError={getattr(res, 'isError', False)}  {text[:160]}")

    return 0


def main() -> int:
    try:
        return asyncio.run(run())
    except Exception as exc:  # noqa: BLE001
        print(f"stdio 联调失败: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

"""MCP 协议级联调测试。

连接已在运行的 wecom-mcp-server (由 supervisord 托管), 走完整 MCP 协议链路:
    initialize -> tools/list -> tools/call

与 tests/test_approval.py 的区别: 后者是进程内直接调用领域层, 本脚本验证
「客户端 ↔ MCP 协议 ↔ 服务进程 ↔ 企业微信 API」的端到端通路。

用法:
    uv run python tests/test_mcp_live.py
    uv run python tests/test_mcp_live.py --url http://127.0.0.1:8101/mcp
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

# (工具名, 参数, 期望状态)
#   "ok"      -> 期望业务层返回 ok=True
#   "need_auth" -> 期望业务层返回 ok=False 且提示缺少企业微信凭据/授权
#   "any"     -> 不做断言, 仅确认协议层有响应
CASES: list[tuple[str, dict[str, Any], str]] = [
    # ---- 纯本地/配置类, 无需任何凭据即可通过 ----
    ("check_config", {}, "ok"),
    ("health_check", {}, "ok"),
    ("list_message_years", {}, "ok"),
    ("get_message_stats", {}, "ok"),
    ("search_history_messages", {"keyword": "__probe_no_match__", "limit": 5}, "ok"),
    # ---- 审批: 本地库能力 (无凭据也能跑通) ----
    ("analyze_approval_records", {"limit": 10}, "ok"),
    ("search_approval_records", {"keyword": "__probe_no_match__"}, "ok"),
    # ---- 审批: 需要真实企业微信凭据 + 后台授权 ----
    ("list_approval_templates", {}, "need_auth"),
    ("export_approval_records", {"start_date": "2026-10-01", "end_date": "2026-10-31"}, "need_auth"),
]


def _extract(result: Any) -> str:
    """把 CallToolResult 的 content 拼成纯文本。"""
    parts: list[str] = []
    for item in getattr(result, "content", []) or []:
        text = getattr(item, "text", None)
        if text:
            parts.append(text)
    return "\n".join(parts)


def _parse(text: str) -> dict[str, Any] | None:
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None


def _summarize(payload: dict[str, Any]) -> str:
    """提取一行可读摘要。"""
    if payload.get("ok"):
        return str(payload.get("note") or "")
    error = str(payload.get("error") or "未知错误")
    detail = payload.get("detail")
    if isinstance(detail, dict):
        hint = detail.get("hint") or detail.get("message") or ""
        missing = detail.get("missing")
        if missing:
            return f"{error} (缺失: {', '.join(map(str, missing))}) {hint}"
        return f"{error} {hint}".strip()
    return f"{error} {detail}".strip()


async def run(url: str) -> int:
    failures: list[str] = []
    print(f"连接 MCP 服务: {url}\n" + "=" * 72)

    async with streamablehttp_client(url) as (read, write, _get_sid):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            print(f"[1] initialize           -> server={init.serverInfo.name} "
                  f"version={init.serverInfo.version} proto={init.protocolVersion}")

            tools = await session.list_tools()
            names = sorted(t.name for t in tools.tools)
            print(f"[2] tools/list           -> {len(names)} 个工具")
            for n in names:
                print(f"      - {n}")

            print("-" * 72)
            print("[3] tools/call 逐项验证")
            for name, args, expect in CASES:
                if name not in names:
                    print(f"  [SKIP] {name:32s} 工具未注册")
                    failures.append(f"{name}: 工具未注册")
                    continue
                try:
                    res = await session.call_tool(name, args)
                except Exception as exc:  # noqa: BLE001
                    print(f"  [FAIL] {name:32s} 协议异常: {exc}")
                    failures.append(f"{name}: 协议异常 {exc}")
                    continue

                text = _extract(res)
                payload = _parse(text)
                app_ok = bool(payload and payload.get("ok"))
                is_error = bool(getattr(res, "isError", False))

                if payload is None:
                    status = "BAD" if expect != "any" else "OK"
                    print(f"  [{status:4s}] {name:32s} 返回非 JSON: {text[:120]!r}")
                    if expect != "any":
                        failures.append(f"{name}: 返回非 JSON")
                    continue

                if expect == "ok":
                    status = "OK" if app_ok else "FAIL"
                elif expect == "need_auth":
                    # 该场景期望服务「明确拒绝并给出可诊断信息」, 而非静默成功或协议异常:
                    #   ok=True  -> 凭据/授权齐备且调用成功
                    #   ok=False -> 带非空 error 的结构化失败 (缺配置/网络/权限/业务码)
                    if app_ok:
                        status = "OK"
                    else:
                        has_error = bool(str(payload.get("error") or "").strip())
                        status = "OK" if has_error else "FAIL"
                        if not has_error:
                            failures.append(f"{name}: 失败但缺少 error 字段")
                else:
                    status = "OK"

                if status == "FAIL":
                    failures.append(f"{name}: 期望 {expect}, 实际 ok={app_ok}")

                summary = _summarize(payload)
                print(f"  [{status:4s}] {name:32s} ok={app_ok} isError={is_error}  {summary[:90]}")

                if not app_ok:
                    print(f"         └─ 错误详情: {json.dumps(payload, ensure_ascii=False)[:300]}")

    print("=" * 72)
    if failures:
        print(f"结果: {len(failures)} 项未达标")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("结果: 全部通过")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="MCP 协议级联调测试")
    p.add_argument("--url", default="http://127.0.0.1:8101/mcp", help="MCP 服务端点")
    args = p.parse_args()
    try:
        return asyncio.run(run(args.url))
    except Exception as exc:  # noqa: BLE001
        print(f"联调失败: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

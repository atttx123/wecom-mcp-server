"""MCP server entry point.

支持三种 transport: stdio (默认) | streamable-http | sse。
HTTP/SSE 模式自动启动 webhook 接收消息 + 注册历史查询/健康检查工具。

启动:
  uv run wecom-mcp-server
  uv run wecom-mcp-server --transport streamable-http --host 0.0.0.0 --port 9000
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from mcp.server.fastmcp import FastMCP

from .config import Settings, get_settings
from .wecom_client import WeComClient, WeComError
from .webhook import WebhookReceiver
from .storage import get_message_db

_client: WeComClient | None = None
_webhook: WebhookReceiver | None = None


def _settings() -> Settings:
    return get_settings()


def _get_client() -> WeComClient:
    global _client
    if _client is None:
        _client = WeComClient(_settings())
    return _client


def _get_webhook() -> WebhookReceiver:
    global _webhook
    if _webhook is None:
        _webhook = WebhookReceiver(_settings())
    return _webhook


def _ok(data: Any, *, note: str = "") -> str:
    payload = {"ok": True, "data": data}
    if note:
        payload["note"] = note
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


def _err(message: str, *, detail: Any = None) -> str:
    payload = {"ok": False, "error": message}
    if detail is not None:
        payload["detail"] = detail
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


def _config_status() -> str:
    s = _settings()
    missing = [k for k, v in [
        ("WECOM_CORP_ID", s.corp_id), ("WECOM_CORP_SECRET", s.corp_secret),
        ("WECOM_AGENT_ID", s.agent_id),
    ] if not v]
    return _err("WeCom 未配置", detail={
        "missing": missing,
        "hint": "请在项目根目录创建 .env (参考 .env.example) 或设置对应环境变量后重启服务。",
    })


mcp = FastMCP("wecom-mcp-server", host=_settings().mcp_host, port=_settings().mcp_port)


# ====================================================================== #
# 发送消息
# ====================================================================== #
@mcp.tool()
async def send_text_message(
    to_user: str = "", to_party: str = "", to_tag: str = "",
    content: str = "", agent_id: int = 0,
) -> str:
    """向企业微信成员发送文本消息。

    Args:
        to_user: 接收人 userid，多人用 "|" 分隔
        to_party: 接收部门 id，多个用 "|" 分隔
        to_tag: 接收标签 id，多个用 "|" 分隔
        content: 文本内容
        agent_id: 应用 AgentId，留空使用默认配置
    """
    if not _settings().is_configured:
        return _config_status()
    if not any([to_user, to_party, to_tag]):
        return _err("to_user / to_party / to_tag 至少指定一个")
    try:
        data = await _get_client().send_text(
            content, to_user=to_user or None, to_party=to_party or None,
            to_tag=to_tag or None, agent_id=agent_id or None,
        )
        return _ok(data, note="文本消息发送结果")
    except WeComError as e:
        return _err(str(e), detail=e.raw)
    except Exception as e:  # noqa: BLE001
        return _err(f"请求失败: {e}")


@mcp.tool()
async def send_markdown_message(
    to_user: str = "", to_party: str = "", to_tag: str = "",
    content: str = "", agent_id: int = 0,
) -> str:
    """发送 Markdown 消息 (仅企业微信客户端支持)。

    content 示例: # 标题 / **加粗** / [链接](url) / > 引用
    """
    if not _settings().is_configured:
        return _config_status()
    if not any([to_user, to_party, to_tag]):
        return _err("to_user / to_party / to_tag 至少指定一个")
    try:
        data = await _get_client().send_markdown(
            content, to_user=to_user or None, to_party=to_party or None,
            to_tag=to_tag or None, agent_id=agent_id or None,
        )
        return _ok(data, note="Markdown 消息发送结果")
    except WeComError as e:
        return _err(str(e), detail=e.raw)
    except Exception as e:  # noqa: BLE001
        return _err(f"请求失败: {e}")


@mcp.tool()
async def send_message(
    to_user: str = "", to_party: str = "", to_tag: str = "",
    msg_type: str = "text", content_json: str = "", agent_id: int = 0,
) -> str:
    """发送任意类型应用消息 (通用接口)。

    Args:
        msg_type: text/markdown/textcard/news/image/mpnews/template_card 等
        content_json: 对应 msg_type 的内容体 JSON。
            text: {"content":"文本"}  markdown: {"content":"# 标题"}
            textcard: {"title":"标题","description":"描述","url":"https://..."}
        agent_id: 应用 AgentId，留空使用默认配置
    """
    if not _settings().is_configured:
        return _config_status()
    if not any([to_user, to_party, to_tag]):
        return _err("to_user / to_party / to_tag 至少指定一个")
    try:
        body = json.loads(content_json) if content_json else {}
    except json.JSONDecodeError as e:
        return _err(f"content_json 不是合法 JSON: {e}")
    try:
        data = await _get_client().send_message(
            to_user=to_user or None, to_party=to_party or None, to_tag=to_tag or None,
            msg_type=msg_type, content=body, agent_id=agent_id or None,
        )
        return _ok(data, note=f"{msg_type} 消息发送结果")
    except WeComError as e:
        return _err(str(e), detail=e.raw)
    except Exception as e:  # noqa: BLE001
        return _err(f"请求失败: {e}")


@mcp.tool()
async def send_file_message(
    file_path: str, to_user: str = "", to_party: str = "", to_tag: str = "",
    agent_id: int = 0,
) -> str:
    """发送文件给企业微信成员 (自动上传文件并发送)。

    限制: 文件不超过 20MB。media_id 仅 3 天内有效。
    """
    if not _settings().is_configured:
        return _config_status()
    if not any([to_user, to_party, to_tag]):
        return _err("to_user / to_party / to_tag 至少指定一个")
    if not os.path.isfile(file_path):
        return _err(f"文件不存在: {file_path}")
    try:
        upload = await _get_client().upload_media(file_path, media_type="file")
        media_id = upload.get("media_id", "")
        if not media_id:
            return _err("上传成功但未获取到 media_id", detail=upload)
        data = await _get_client().send_message(
            to_user=to_user or None, to_party=to_party or None, to_tag=to_tag or None,
            msg_type="file", content={"media_id": media_id}, agent_id=agent_id or None,
        )
        size = os.path.getsize(file_path)
        return _ok(data, note=f"文件发送成功: {os.path.basename(file_path)} ({size} bytes)")
    except WeComError as e:
        return _err(str(e), detail=e.raw)
    except Exception as e:  # noqa: BLE001
        return _err(f"请求失败: {e}")


@mcp.tool()
async def mass_send_message(content: str, msg_type: str = "text", agent_id: int = 0) -> str:
    """群发消息给应用可见范围内的全部成员 (touser=@all)。

    注意: @all 仅覆盖应用可见范围，不是企业全部成员。
    如需发给全企业，请把应用可见范围设为根部门。
    """
    if not _settings().is_configured:
        return _config_status()
    try:
        if msg_type == "markdown":
            data = await _get_client().send_message(
                to_user="@all", msg_type="markdown",
                content={"content": content}, agent_id=agent_id or None,
            )
        else:
            data = await _get_client().mass_send_text(content, agent_id=agent_id or None)
        return _ok(data, note="群发 (@all) 结果")
    except WeComError as e:
        return _err(str(e), detail=e.raw)
    except Exception as e:  # noqa: BLE001
        return _err(f"请求失败: {e}")


# ====================================================================== #
# 通讯录 (简化版: 辅助发送消息)
# ====================================================================== #
@mcp.tool()
async def lookup_user_id(name: str) -> str:
    """通过成员姓名查找 UserID (用于发送消息时指定接收人)。

    支持精确匹配；若无人匹配则模糊匹配。多人同名会全部返回。
    """
    if not _settings().is_configured:
        return _config_status()
    try:
        data = await _get_client().user_simplelist_all()
        users = data.get("userlist", [])
        matches = [{"userid": u["userid"], "name": u["name"]} for u in users if u.get("name") == name]
        if not matches:
            matches = [{"userid": u["userid"], "name": u["name"]} for u in users if name in u.get("name", "")]
        return _ok({"matches": matches, "total_candidates": len(users)},
                   note=f"找到 {len(matches)} 个匹配")
    except WeComError as e:
        return _err(str(e), detail=e.raw)
    except Exception as e:  # noqa: BLE001
        return _err(f"请求失败: {e}")


@mcp.tool()
async def list_users_by_department(department_id: int = 0, fetch_child: bool = True) -> str:
    """列出部门成员 (仅返回姓名 + UserID)。

    Args:
        department_id: 部门 ID，0=根部门 (全部成员)
        fetch_child: 是否递归子部门
    """
    if not _settings().is_configured:
        return _config_status()
    try:
        data = await _get_client().user_list(department_id, fetch_child=1 if fetch_child else 0)
        users = [{"userid": u["userid"], "name": u["name"]} for u in data.get("userlist", [])]
        return _ok({"users": users, "count": len(users), "department_id": department_id},
                   note=f"部门 {department_id} 成员列表")
    except WeComError as e:
        return _err(str(e), detail=e.raw)
    except Exception as e:  # noqa: BLE001
        return _err(f"请求失败: {e}")


# ====================================================================== #
# 历史查询 + 健康检查 (仅 HTTP/SSE 模式, 在 main() 中动态注册)
# ====================================================================== #
async def health_check() -> str:
    """健康检查: webhook 运行状态、回调配置、数据库状态。"""
    wh = _get_webhook()
    s = _settings()
    db = get_message_db()
    db_ok, db_years = True, []
    try:
        db_years = [y["year"] for y in db.list_years()]
    except Exception:  # noqa: BLE001
        db_ok = False
    return _ok({
        "status": "ok" if wh.running else "degraded",
        "webhook_running": wh.running,
        "webhook_listen": wh.describe(),
        "webhook_port": wh.port,
        "callback_configured": bool(s.callback_token and s.callback_encoding_aes_key),
        "db_ok": db_ok,
        "db_years": db_years,
    }, note="webhook=运行中/未运行, db=数据库可读性")


async def search_history_messages(
    start_time: int = 0, end_time: int = 0, from_user: str = "",
    msg_type: str = "", keyword: str = "", limit: int = 50, offset: int = 0,
) -> str:
    """查询历史消息 (本地 SQLite，支持跨年)。

    Args:
        start_time: 起始时间 (unix 时间戳, 0=不限)
        end_time: 结束时间 (unix 时间戳, 0=当前时间)
        from_user: 按发送人 UserID 过滤
        msg_type: 按类型过滤 (text/image/voice/video/file/location/link/event)
        keyword: 文本内容关键词模糊匹配
        limit: 返回条数上限 (默认 50)
        offset: 分页偏移
    """
    db = get_message_db()
    rows = db.search_messages(
        start_time=start_time, end_time=end_time, from_user=from_user,
        msg_type=msg_type, keyword=keyword, limit=limit, offset=offset,
    )
    return _ok({"messages": rows, "count": len(rows)}, note="历史消息查询结果 (按时间倒序)")


async def get_message_detail(msg_id: str, year: int = 0) -> str:
    """获取单条历史消息详情 (含文件信息)。

    Args:
        msg_id: 消息 ID
        year: 消息年份；0=自动查找
    """
    db = get_message_db()
    if year == 0:
        for y in db.list_years():
            row = db.get_message_by_id(y["year"], msg_id)
            if row:
                return _ok(row, note="消息详情")
        return _err(f"未找到 msg_id={msg_id} 的消息")
    row = db.get_message_by_id(year, msg_id)
    return _ok(row, note="消息详情") if row else _err(f"未找到 msg_id={msg_id} (year={year})")


async def get_message_file(msg_id: str, year: int = 0) -> str:
    """获取消息关联的文件本地路径。

    Args:
        msg_id: 消息 ID
        year: 消息年份；0=自动查找
    """
    db = get_message_db()
    row = None
    if year == 0:
        for y in db.list_years():
            row = db.get_message_by_id(y["year"], msg_id)
            if row:
                break
    else:
        row = db.get_message_by_id(year, msg_id)
    if not row:
        return _err(f"未找到 msg_id={msg_id} 的消息")
    if not row.get("file_path"):
        return _err("该消息无关联文件", detail={"msg_type": row.get("msg_type"), "file_status": row.get("file_status")})
    return _ok({
        "msg_id": msg_id,
        "file_name": row.get("file_name"),
        "file_path": str(db.abs_path(row["file_path"])),
        "file_size": row.get("file_size"),
        "file_md5": row.get("file_md5"),
        "file_status": row.get("file_status"),
        "file_ext": row.get("file_ext"),
    }, note="文件本地路径")


async def list_message_years() -> str:
    """列出有历史消息数据的年份及各年消息数。"""
    db = get_message_db()
    years = db.list_years()
    return _ok({"years": years, "total_years": len(years)}, note="有数据的年份列表")


async def get_message_stats(start_time: int = 0, end_time: int = 0) -> str:
    """统计历史消息 (按类型/发送人/月份)。

    Args:
        start_time: 起始时间 (unix 时间戳, 0=不限)
        end_time: 结束时间 (unix 时间戳, 0=当前时间)
    """
    db = get_message_db()
    return _ok(db.get_stats(start_time=start_time, end_time=end_time), note="消息统计")


def _register_http_tools() -> None:
    """注册仅 HTTP/SSE 模式可用的工具。"""
    for tool in (health_check, search_history_messages, get_message_detail,
                 get_message_file, list_message_years, get_message_stats):
        mcp.add_tool(tool)


def _autostart_webhook() -> None:
    """HTTP/SSE 模式自动开启 webhook 接收消息。"""
    s = _settings()
    if not s.callback_token or not s.callback_encoding_aes_key:
        print("[wecom-mcp-server] 未配置回调 Token/AESKey，webhook 不会启动", file=sys.stderr)
        return
    try:
        wh = _get_webhook()
        if not wh.running:
            wh.start()
        print(f"[wecom-mcp-server] webhook 已启动: {wh.describe()}", file=sys.stderr)
    except Exception as e:  # noqa: BLE001
        print(f"[wecom-mcp-server] webhook 启动失败: {e}", file=sys.stderr)


@mcp.tool()
async def check_config() -> str:
    """检查当前 WeCom 配置是否就绪 (不发起网络请求)。"""
    s = _settings()
    return _ok({
        "configured": s.is_configured,
        "corp_id": s.corp_id or "(未设置)",
        "agent_id": s.agent_id or "(未设置)",
        "corp_secret": "***" if s.corp_secret else "(未设置)",
        "contacts_secret_configured": s.has_contacts_secret,
        "callback_configured": bool(s.callback_token and s.callback_encoding_aes_key),
        "api_base": s.api_base,
        "webhook_port": s.webhook_port,
        "mcp_transport": s.mcp_transport,
        "data_dir": s.data_dir,
    }, note="配置自检结果")


def main() -> None:
    """MCP 服务入口。优先级: 命令行 > 环境变量 > 默认 (stdio)。"""
    p = argparse.ArgumentParser(prog="wecom-mcp-server", description="WeCom MCP Server")
    p.add_argument("--transport", choices=["stdio", "streamable-http", "sse"], default=None)
    p.add_argument("--host", default=None)
    p.add_argument("--port", type=int, default=None)
    args = p.parse_args()

    if args.transport is not None:
        os.environ["WECOM_MCP_TRANSPORT"] = args.transport
    if args.host is not None:
        os.environ["WECOM_MCP_HOST"] = args.host
    if args.port is not None:
        os.environ["WECOM_MCP_PORT"] = str(args.port)

    get_settings.cache_clear()
    s = get_settings()
    transport = s.mcp_transport.lower().strip()

    if transport == "stdio":
        mcp.run(transport="stdio")
    elif transport in ("streamable-http", "sse"):
        _register_http_tools()
        mcp.settings.host = s.mcp_host
        mcp.settings.port = s.mcp_port
        endpoint = "/mcp" if transport == "streamable-http" else "/sse"
        print(f"[wecom-mcp-server] {transport} on http://{s.mcp_host}:{s.mcp_port}{endpoint}", file=sys.stderr)
        _autostart_webhook()
        mcp.run(transport=transport)
    else:
        raise ValueError(f"未知 transport: {transport!r}")


if __name__ == "__main__":
    main()

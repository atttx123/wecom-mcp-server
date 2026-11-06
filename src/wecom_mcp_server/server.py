"""MCP server entry point.

支持三种 transport: stdio (默认) | streamable-http | sse。
HTTP/SSE 模式自动启动 webhook 接收消息 + 注册历史查询/健康检查工具。

启动:
  uv run wecom-mcp-server
  uv run wecom-mcp-server --transport streamable-http --host 0.0.0.0 --port 8101
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Any

from mcp.server.fastmcp import FastMCP

from .config import Settings, get_settings
from .wecom_client import WeComClient, WeComError
from .webhook import WebhookReceiver
from .storage import get_message_db
from .approval import (
    ApprovalCollector,
    ContactResolver,
    RECORD_TYPE_TEXT,
    build_record,
    fmt_ts,
    now_ts,
    parse_status_filter,
    parse_time_input,
)
from .approval_storage import get_approval_db
from .approval_export import analyze_records, export_records

_client: WeComClient | None = None
_webhook: WebhookReceiver | None = None
_approval_collector: ApprovalCollector | None = None
_contacts: ContactResolver | None = None


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


def _get_approval_collector() -> ApprovalCollector:
    global _approval_collector
    if _approval_collector is None:
        _approval_collector = ApprovalCollector(
            _get_client(), concurrency=_settings().approval_concurrency
        )
    return _approval_collector


def _get_contacts() -> ContactResolver:
    global _contacts
    if _contacts is None:
        _contacts = ContactResolver(_get_client())
    return _contacts


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
# 审批 (工作台 - 审批)
# ====================================================================== #
_APPROVAL_SETUP_HINT = (
    "请在企业微信管理后台完成配置：应用管理 → 审批 → API → 可调用应用，加入本自建应用；"
    "并在 审批 → API → 审批数据权限 中授权该应用读取审批数据；"
    "同时确认应用可见范围覆盖目标申请人，否则只能读到可见范围内成员的审批单。"
)

#: 301055 的 errmsg 会指出具体症结, 按关键字给出对症指引
_APPROVAL_301055_CASES: tuple[tuple[str, str], ...] = (
    (
        "not in app visible range",
        "症结是【可见范围】不匹配。企微遵循权限最小原则, 审批场景涉及三个独立的可见范围, "
        "必须同时覆盖目标申请人: ① 自建应用的可见范围 (应用管理 → 自建应用 → 编辑 → 可见范围); "
        "② 审批应用的可见范围 (应用管理 → 审批 → 应用设置 → 可见范围); "
        "③ 审批模板的可见范围 (应用管理 → 审批 → 选择模板 → 规则设置 → 可见范围)。",
    ),
    (
        "no approval auth",
        "症结是应用本身未获得审批接口权限。请到 应用管理 → 审批 → API → 可调用应用 "
        "把本自建应用加入列表, 并在 审批 → API → 审批数据权限 中授权该应用读取审批单据。",
    ),
    (
        "template",
        "症结可能是审批模板的可见范围未覆盖申请人。请到 应用管理 → 审批 → 选择对应模板 → "
        "规则设置 → 可见范围, 确认目标申请人已包含。",
    ),
)


def _approval_301055_hint(errmsg: str) -> str:
    """按企业微信返回的 errmsg 关键字给出精确的排查指引。"""
    text = (errmsg or "").lower()
    for keyword, guidance in _APPROVAL_301055_CASES:
        if keyword in text:
            return f"无审批应用数据拉取权限。{guidance}"
    return "无审批应用数据拉取权限。" + _APPROVAL_SETUP_HINT


#: 从企业微信 errmsg 中提取出口 IP (形如 "from ip: 1.2.3.4")
_IP_IN_ERRMSG = re.compile(r"from ip:\s*([0-9.]+)")


def _ip_whitelist_hint(errmsg: str) -> str:
    """60020: 调用方 IP 不在应用的「企业可信 IP」白名单内。"""
    match = _IP_IN_ERRMSG.search(errmsg or "")
    ip = match.group(1) if match else "<当前出口 IP>"
    return (
        f"调用方 IP 不在企业可信 IP 白名单内, 本次请求的出口 IP 是 {ip}。"
        f"请到 管理后台 → 应用管理 → 自建应用 → 本应用 → 企业可信IP, "
        f"把 {ip} 加入白名单并保存; 之后需重新发起请求。"
        f"若该 IP 由动态网络或代理产生 (地址会变), 建议改用固定出口 IP 的服务器部署。"
    )


def _approval_error(exc: WeComError) -> str:
    if exc.errcode == 301055:
        hint = _approval_301055_hint(exc.errmsg)
    elif exc.errcode == 60020:
        hint = _ip_whitelist_hint(exc.errmsg)
    else:
        hints = {
            301025: "请求参数错误（starttime/endtime 跨度不得超过 31 天，size 不超过 100）。",
            301026: "企业微信内部接口失败，建议稍后重试。",
            40001: "access_token 无效或已过期。",
            42001: "access_token 已过期，请重试。",
            48002: "api 接口无权限。" + _APPROVAL_SETUP_HINT,
            301002: "无效的 corpsecret。",
        }
        hint = hints.get(exc.errcode, "")
    return _err(
        f"企业微信审批接口调用失败: {exc.errmsg}",
        detail={"errcode": exc.errcode, "hint": hint},
    )


def _split_multi(value: str) -> list[str]:
    return [v.strip() for v in str(value or "").replace("，", ",").split(",") if v.strip()]


def _resolve_range(start_date: str, end_date: str, default_days: int = 30) -> tuple[int, int]:
    """把 start/end 解析为时间戳区间；缺省 end=当前，start=end-default_days。"""
    end_ts = parse_time_input(end_date, end_of_day=True) if end_date else None
    start_ts = parse_time_input(start_date) if start_date else None
    if end_ts is None:
        end_ts = now_ts()
    if start_ts is None:
        start_ts = end_ts - default_days * 86400
    if start_ts > end_ts:
        start_ts, end_ts = end_ts, start_ts
    return start_ts, end_ts


def _record_brief(record: dict[str, Any]) -> dict[str, Any]:
    """精简记录，用于检索结果展示。"""
    return {
        "sp_no": record.get("sp_no", ""),
        "template_name": record.get("template_name", ""),
        "sp_status_text": record.get("sp_status_text", ""),
        "apply_time_text": record.get("apply_time_text", ""),
        "applier_name": record.get("applier_name", ""),
        "applier_userid": record.get("applier_userid", ""),
        "money_total": record.get("money_total", 0),
        "form_text": record.get("form_text", "")[:500],
    }


@mcp.tool()
async def export_approval_records(
    start_date: str = "",
    end_date: str = "",
    template_id: str = "",
    template_name: str = "",
    applicant: str = "",
    department_id: str = "",
    sp_status: str = "",
    record_type: int = 0,
    keyword: str = "",
    output_format: str = "xlsx",
    output_path: str = "",
    max_records: int = 0,
    fetch_detail: bool = True,
) -> str:
    """导出企业微信「工作台 - 审批」的提交记录，并附带统计分析。

    数据来源为审批应用数据权限范围内的审批单（按提交时间筛选）。

    Args:
        start_date: 提交时间起点，支持 YYYY-MM-DD / "YYYY-MM-DD HH:MM:SS" / unix 时间戳
        end_date: 提交时间终点，默认当前时间
        template_id: 审批模板 ID，多个用 "," 分隔
        template_name: 模板名称关键字（本地匹配）
        applicant: 申请人 userid 或姓名
        department_id: 提单人所在部门 ID
        sp_status: 审批状态，支持 1/2/3/4/6/7/10 或中文名（"审批中,已通过"），多个用 "," 分隔
        record_type: 审批单类型 1请假 2补卡 3出差 4外出 5加班 6调班 7会议室预定 8退款 9红包报销
        keyword: 表单内容关键字（本地过滤）
        output_format: 导出格式 csv / xlsx / json，默认 xlsx
        output_path: 导出文件路径，留空则写入 {data_dir}/exports/
        max_records: 最多导出条数，0 表示不限（远端拉取上限为该值的 5 倍）
        fetch_detail: 是否拉取审批详情。False 时只导出单号列表，速度快但无表单内容
    """
    if not _settings().is_configured:
        return _config_status()

    db = get_approval_db(_settings())
    start_ts, end_ts = _resolve_range(start_date, end_date)
    notes: list[str] = []
    filters: list[dict[str, str]] = []

    # ---- 模板筛选 ----
    template_ids = _split_multi(template_id)
    local_template_name = ""
    if template_name:
        resolved = db.template_ids_by_name(template_name)
        if resolved:
            template_ids.extend(t for t in resolved if t not in template_ids)
            notes.append(
                f"模板名「{template_name}」在本地匹配到 {len(resolved)} 个模板 ID，"
                f"已按 ID 精确拉取；同名的其他模板若不在本地库中则不会被拉取。"
            )
        else:
            local_template_name = template_name
            notes.append(
                f"本地未找到名称包含「{template_name}」的模板 ID，将拉取时间范围内全部审批单后在本地过滤，"
                f"数据量较大时请配合 max_records 使用。"
            )
    for tid in template_ids:
        filters.append({"key": "template_id", "value": tid})

    # ---- 申请人筛选 ----
    applicant_ids: list[str] = []
    if applicant:
        applicant_ids = await _get_contacts().userids_by_name(applicant)
        if not applicant_ids:
            applicant_ids = [applicant]
            notes.append(
                f"未能在通讯录中匹配到「{applicant}」，已按 userid 直接过滤，"
                f"若通讯录不可见范围受限，建议改用模板+关键字筛选。"
            )
        for uid in applicant_ids:
            filters.append({"key": "creator", "value": uid})

    # ---- 部门 / 状态 / 类型 ----
    for dept in _split_multi(department_id):
        filters.append({"key": "department", "value": dept})
    for status in parse_status_filter(sp_status):
        filters.append({"key": "sp_status", "value": str(status)})
    if record_type:
        filters.append({"key": "record_type", "value": str(int(record_type))})
        notes.append(f"单据类型筛选：{RECORD_TYPE_TEXT.get(int(record_type), record_type)}。")

    collector = _get_approval_collector()
    list_limit = int(max_records) * 5 if max_records else 0
    try:
        sp_nos, list_meta = await collector.list_sp_no(
            start_time=start_ts,
            end_time=end_ts,
            filters=filters or None,
            max_records=list_limit,
        )
    except WeComError as e:
        return _approval_error(e)
    except Exception as e:  # noqa: BLE001
        return _err(f"拉取审批单号失败: {e}")

    if not sp_nos:
        return _ok({
            "range": {"start": fmt_ts(start_ts), "end": fmt_ts(end_ts)},
            "api_filters": filters,
            "sp_no_count": 0,
            "records_fetched": 0,
            "records_matched": 0,
            "records_saved": 0,
            "export": None,
            "notes": notes + ["该时间范围内没有匹配的审批单。"],
        }, note="审批记录导出结果（无数据）")

    records: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    if fetch_detail:
        chunk_size = 200
        for index in range(0, len(sp_nos), chunk_size):
            chunk_records, chunk_errors = await collector.fetch_details(sp_nos[index:index + chunk_size])
            records.extend(chunk_records)
            errors.extend(chunk_errors)
            if max_records and len(records) >= max_records * 3:
                break
    else:
        records = [{"sp_no": sp_no, "template_name": "", "sp_status": 0,
                    "sp_status_text": "", "apply_time": 0, "apply_time_text": "",
                    "form_text": "", "raw": {}} for sp_no in sp_nos]
        notes.append("未拉取审批详情，导出内容仅含审批单号。")

    if errors:
        # 详情拉取失败 = 数据缺失，必须显式告知，避免用户误以为导出是完整的
        by_code: dict[str, int] = {}
        for item in errors:
            by_code[item.get("errcode", "?")] = by_code.get(item.get("errcode", "?"), 0) + 1
        detail = "、".join(
            f"{code}×{count}" for code, count in sorted(by_code.items(), key=lambda kv: -kv[1])
        )
        sample = errors[0]
        notes.append(
            f"有 {len(errors)} 条审批单详情拉取失败并已跳过（导出结果不完整），"
            f"错误码分布：{detail}；示例 {sample.get('sp_no', '')}: {sample.get('errmsg', '')}"
        )

    # ---- 本地二次过滤 ----
    matched = records
    if local_template_name:
        matched = [r for r in matched if local_template_name in (r.get("template_name") or "")]
    if applicant:
        matched = [
            r for r in matched
            if r.get("applier_userid") in applicant_ids
            or applicant in (r.get("applier_name") or "")
        ]
    if department_id:
        # 企业微信服务端按 department 筛选时**包含子部门**的审批单，
        # 因此本地必须按部门子树判断；通讯录不可用时放弃本地过滤，
        # 改为完全信任服务端的筛选结果，避免误删。
        allowed: set[str] = set()
        subtree_known = True
        for dept in _split_multi(department_id):
            subtree = await _get_contacts().department_subtree(dept)
            if subtree is None:
                subtree_known = False
                break
            allowed |= subtree
        if subtree_known and allowed:
            matched = [r for r in matched if (r.get("applier_partyid") or "") in allowed]
        else:
            notes.append(
                "通讯录部门层级不可用，已完全采用企业微信服务端的部门筛选结果（该结果包含子部门）。"
            )
    if keyword:
        kw = keyword.lower()
        matched = [
            r for r in matched
            if kw in (r.get("form_text") or "").lower()
            or kw in (r.get("template_name") or "").lower()
            or kw in (r.get("applier_name") or "").lower()
            or kw in (r.get("sp_no") or "").lower()
        ]
    if max_records:
        matched = matched[:int(max_records)]

    # ---- 补全姓名 + 落库 ----
    saved = 0
    if fetch_detail and matched:
        try:
            await _get_contacts().fill_names(matched)
        except Exception:  # noqa: BLE001
            notes.append("通讯录姓名解析失败，申请人/部门名称可能为空。")
        saved = db.upsert_approvals(matched)
        for template_id, template_name in {
            (r.get("template_id", ""), r.get("template_name", "")) for r in matched
        }:
            if template_id:
                db.upsert_template(template_id, template_name)

    analysis = analyze_records(matched)
    try:
        export_info = export_records(
            matched,
            output_format=output_format,
            output_path=output_path,
            output_dir=str(_settings().data_dir) + "/exports",
            analysis=analysis,
        )
    except ValueError as e:
        return _err(str(e))
    except OSError as e:
        return _err(f"导出文件写入失败: {e}")

    return _ok({
        "range": {"start": fmt_ts(start_ts), "end": fmt_ts(end_ts)},
        "api_filters": filters,
        "local_filters": {
            "template_name": local_template_name,
            "applicant": applicant,
            "department_id": department_id,
            "keyword": keyword,
        },
        "sp_no_count": len(sp_nos),
        "list_meta": list_meta,
        "records_fetched": len(records),
        "records_matched": len(matched),
        "records_saved": saved,
        "export": export_info,
        "analysis": analysis,
        "notes": notes,
    }, note="审批记录导出结果")


@mcp.tool()
async def analyze_approval_records(
    start_date: str = "",
    end_date: str = "",
    template_id: str = "",
    template_name: str = "",
    applicant: str = "",
    sp_status: str = "",
    keyword: str = "",
    limit: int = 0,
) -> str:
    """分析本地已导出的审批记录（按模板 / 状态 / 申请人 / 时长 / 金额分组）。

    仅统计本地库中已通过 export_approval_records 拉取到的数据。

    Args:
        start_date: 提交时间起点 (YYYY-MM-DD 或时间戳)，留空表示不限
        end_date: 提交时间终点，留空表示不限
        template_id: 模板 ID，多个用 "," 分隔
        template_name: 模板名称关键字
        applicant: 申请人 userid 或姓名
        sp_status: 审批状态，多个用 "," 分隔
        keyword: 表单内容关键字
        limit: 最多参与统计的记录数，0 表示全部
    """
    db = get_approval_db(_settings())
    start_ts, end_ts = _resolve_range(start_date, end_date) if (start_date or end_date) else (0, 0)
    template_ids = _split_multi(template_id)
    if template_name:
        template_ids.extend(t for t in db.template_ids_by_name(template_name) if t not in template_ids)
    statuses = parse_status_filter(sp_status)
    records = db.query(
        start_time=start_ts,
        end_time=end_ts,
        template_ids=template_ids or None,
        template_name=template_name if template_ids else "",
        applicant=applicant,
        sp_status=statuses or None,
        keyword=keyword,
        limit=int(limit or 0),
    )
    analysis = analyze_records(records)
    local_lo, local_hi = db.time_range()
    return _ok({
        "scope": {
            "start": fmt_ts(start_ts) if start_ts else "不限",
            "end": fmt_ts(end_ts) if end_ts else "不限",
            "template_ids": template_ids,
            "template_name": template_name,
            "applicant": applicant,
            "sp_status": statuses,
            "keyword": keyword,
        },
        "local_total": db.count(),
        "local_range": {
            "start": fmt_ts(local_lo),
            "end": fmt_ts(local_hi),
        },
        "matched": len(records),
        "analysis": analysis,
    }, note="审批记录分析结果")


@mcp.tool()
async def get_approval_detail(sp_no: str, refresh: bool = False) -> str:
    """获取单条审批单的完整详情（表单内容、审批流程、备注）。

    Args:
        sp_no: 审批单号，如 "202609060001"
        refresh: 为 True 时强制回源企业微信拉取最新状态
    """
    if not sp_no:
        return _err("sp_no 不能为空")
    db = get_approval_db(_settings())
    if not refresh:
        record = db.get_approval(sp_no)
        if record:
            record["source"] = "local"
            return _ok(record, note="审批单详情（本地缓存）")
    if not _settings().is_configured:
        return _config_status()
    try:
        resp = await _get_client().get_approval_detail(sp_no)
    except WeComError as e:
        return _approval_error(e)
    except Exception as e:  # noqa: BLE001
        return _err(f"请求失败: {e}")
    info = resp.get("info") or {}
    if not info:
        return _err(f"未找到审批单 {sp_no}", detail=resp)
    record = build_record(info)
    try:
        await _get_contacts().fill_names([record])
    except Exception:  # noqa: BLE001
        pass
    db.upsert_approval(record)
    if record.get("template_id"):
        db.upsert_template(record["template_id"], record.get("template_name", ""))
    record["source"] = "remote"
    return _ok(record, note="审批单详情")


@mcp.tool()
async def list_approval_templates(
    template_id: str = "", with_controls: bool = False, refresh: bool = False
) -> str:
    """列出审批模板（模板名、控件结构），用于确定导出筛选条件。

    Args:
        template_id: 指定模板 ID 则只返回该模板；留空返回全部已知模板
        with_controls: 是否附带模板控件结构（需回源企业微信获取）
        refresh: 为 True 时忽略缓存，重新拉取模板结构
    """
    db = get_approval_db(_settings())
    if template_id:
        meta = None if refresh else db.template_meta(template_id)
        if meta is None:
            if not _settings().is_configured:
                return _config_status()
            try:
                resp = await _get_client().get_approval_template(template_id)
            except WeComError as e:
                return _approval_error(e)
            except Exception as e:  # noqa: BLE001
                return _err(f"请求失败: {e}")
            names = resp.get("template_names") or []
            template_name = ""
            if names:
                first = names[0]
                template_name = first.get("text", "") if isinstance(first, dict) else str(first)
            controls = ((resp.get("template_content") or {}).get("controls")) or []
            db.upsert_template(template_id, template_name, controls)
            return _ok({
                "template_id": template_id,
                "template_name": template_name,
                "controls": controls,
                "control_count": len(controls),
                "source": "remote",
            }, note="审批模板详情")
        return _ok({
            "template_id": template_id,
            "template_name": meta.get("template_name", ""),
            "controls": meta.get("controls", []),
            "control_count": len(meta.get("controls", [])),
            "source": "local",
        }, note="审批模板详情（本地缓存）")

    templates = db.list_templates()
    if with_controls:
        enriched = []
        for item in templates:
            tid = item.get("template_id", "")
            meta = None if refresh else db.template_meta(tid)
            if meta is None and _settings().is_configured:
                try:
                    resp = await _get_client().get_approval_template(tid)
                    names = resp.get("template_names") or []
                    template_name = ""
                    if names:
                        first = names[0]
                        template_name = first.get("text", "") if isinstance(first, dict) else str(first)
                    controls = ((resp.get("template_content") or {}).get("controls")) or []
                    db.upsert_template(tid, template_name, controls)
                    meta = {"template_name": template_name, "controls": controls}
                except Exception:  # noqa: BLE001
                    meta = {"template_name": item.get("template_name", ""), "controls": []}
            enriched.append({
                "template_id": tid,
                "template_name": (meta or {}).get("template_name") or item.get("template_name", ""),
                "count": item.get("count", 0),
                "last_apply_time": fmt_ts(item.get("last_apply_time")),
                "controls": (meta or {}).get("controls", []),
            })
        return _ok({"templates": enriched, "count": len(enriched)}, note="审批模板列表（含控件结构）")

    return _ok({"templates": templates, "count": len(templates)}, note="审批模板列表")


@mcp.tool()
async def search_approval_records(
    keyword: str = "",
    template_name: str = "",
    applicant: str = "",
    sp_status: str = "",
    start_date: str = "",
    end_date: str = "",
    limit: int = 30,
    offset: int = 0,
) -> str:
    """在本地已导出的审批记录中检索表单内容。

    Args:
        keyword: 表单内容 / 模板名 / 申请人 / 单号关键字
        template_name: 模板名称关键字
        applicant: 申请人 userid 或姓名
        sp_status: 审批状态，多个用 "," 分隔
        start_date: 提交时间起点
        end_date: 提交时间终点
        limit: 返回条数上限
        offset: 分页偏移
    """
    db = get_approval_db(_settings())
    start_ts, end_ts = _resolve_range(start_date, end_date) if (start_date or end_date) else (0, 0)
    statuses = parse_status_filter(sp_status)
    records = db.query(
        start_time=start_ts,
        end_time=end_ts,
        template_name=template_name,
        applicant=applicant,
        sp_status=statuses or None,
        keyword=keyword,
        limit=int(limit),
        offset=int(offset),
    )
    return _ok({
        "records": [_record_brief(r) for r in records],
        "count": len(records),
        "local_total": db.count(),
    }, note="本地审批记录检索结果")


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

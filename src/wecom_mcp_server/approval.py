"""企业微信「工作台 - 审批」领域层。

负责把企业微信 OA 审批开放接口的原始报文，规范化为可导出、可统计的结构化记录。

官方接口:
  POST /cgi-bin/oa/getapprovalinfo     批量获取审批单号
  POST /cgi-bin/oa/getapprovaldetail   获取审批申请详情
  POST /cgi-bin/oa/gettemplatedetail   获取审批模板详情

接口约束 (必须在采集层处理):
  - 单次最多拉取 100 条审批单号, 600 次/分钟
  - starttime/endtime 时间跨度不得超过 31 天
  - filters 中不同类型条件之间为「与」, 同类型条件之间为「或」
  - 自建应用需在「管理后台 - 应用管理 - 审批 - API - 可调用应用」中授权
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from .wecom_client import WeComClient, WeComError

try:  # pragma: no cover - 平台差异
    from zoneinfo import ZoneInfo

    _TZ: Any = ZoneInfo("Asia/Shanghai")
except Exception:  # pragma: no cover - 无 tzdata 时退化为固定偏移
    _TZ = timezone(timedelta(hours=8))

#: 单次拉取审批单号的最大条数 (企业微信硬限制)
PAGE_SIZE = 100
#: getapprovalinfo 允许的最大时间跨度 (秒)
MAX_WINDOW = 31 * 86400
#: 拉取审批详情的默认并发数。
#: 企业微信对「同一企业 + 同一接口」做全局并发限制 (errcode 45033)，
#: 且该限制由常驻服务与本进程共享，故不宜取大；4 可兼顾速度与稳定。
DEFAULT_CONCURRENCY = 4

#: 触发「接口并发调用超过限制」的错误码，需退避后重试
CONCURRENT_LIMIT_ERRCODE = 45033

#: 45033 退避重试次数与基础延迟(秒)
CONCURRENT_LIMIT_RETRIES = 3
CONCURRENT_LIMIT_BACKOFF = 0.5

# ---------------------------------------------------------------------- #
# 枚举
# ---------------------------------------------------------------------- #
SP_STATUS_TEXT: dict[int, str] = {
    1: "审批中",
    2: "已通过",
    3: "已驳回",
    4: "已撤销",
    6: "通过后撤销",
    7: "已删除",
    10: "已支付",
    12: "加签中",
}

#: 允许用户以中文/英文名指定审批状态
SP_STATUS_ALIASES: dict[str, int] = {
    "审批中": 1, "在途": 1, "待审批": 1, "pending": 1, "in_progress": 1,
    "已通过": 2, "通过": 2, "approved": 2, "pass": 2,
    "已驳回": 3, "驳回": 3, "rejected": 3, "reject": 3,
    "已撤销": 4, "撤销": 4, "已撤回": 4, "撤回": 4, "cancelled": 4, "canceled": 4,
    "通过后撤销": 6, "已删除": 7, "已支付": 10, "加签中": 12,
}

APPROVER_ATTR_TEXT: dict[int, str] = {1: "或签", 2: "会签", 3: "依次审批"}

RECORD_TYPE_TEXT: dict[int, str] = {
    1: "请假", 2: "补卡", 3: "出差", 4: "外出", 5: "加班",
    6: "调班", 7: "会议室预定", 8: "退款", 9: "红包报销",
}

ATTENDANCE_TYPE_TEXT: dict[int, str] = {
    1: "请假", 2: "补卡", 3: "出差", 4: "外出", 5: "加班",
}


# ---------------------------------------------------------------------- #
# 时间工具
# ---------------------------------------------------------------------- #
def now_ts() -> int:
    return int(datetime.now(tz=_TZ).timestamp())


def fmt_ts(ts: Any, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """把 unix 时间戳格式化为北京时间字符串。"""
    try:
        ts = int(ts)
    except (TypeError, ValueError):
        return ""
    if ts <= 0:
        return ""
    return datetime.fromtimestamp(ts, tz=_TZ).strftime(fmt)


def fmt_date(ts: Any) -> str:
    return fmt_ts(ts, "%Y-%m-%d")


def parse_time_input(value: Any, *, end_of_day: bool = False) -> int | None:
    """把用户输入的时间解析为 unix 时间戳。

    支持:
      - 整数/数字字符串: unix 时间戳
      - YYYY-MM-DD
      - YYYY-MM-DD HH:MM / YYYY-MM-DD HH:MM:SS
      - YYYY/MM/DD
    """
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"\d{9,13}", text):
        ts = int(text)
        return ts // 1000 if len(text) == 13 else ts
    text = text.replace("/", "-").replace("T", " ")
    for pattern, has_time in (
        ("%Y-%m-%d %H:%M:%S", True),
        ("%Y-%m-%d %H:%M", True),
        ("%Y-%m-%d", False),
    ):
        try:
            dt = datetime.strptime(text, pattern)
        except ValueError:
            continue
        if not has_time and end_of_day:
            dt = dt.replace(hour=23, minute=59, second=59)
        return int(dt.replace(tzinfo=_TZ).timestamp())
    return None


def split_windows(start_ts: int, end_ts: int, window: int = MAX_WINDOW) -> list[tuple[int, int]]:
    """把时间范围切成不超过 window 秒的窗口。"""
    if end_ts <= start_ts:
        return [(start_ts, end_ts)]
    windows: list[tuple[int, int]] = []
    cursor = start_ts
    while cursor < end_ts:
        nxt = min(cursor + window, end_ts)
        windows.append((cursor, nxt))
        cursor = nxt
    return windows


# ---------------------------------------------------------------------- #
# 表单控件解析
# ---------------------------------------------------------------------- #
def pick_text(items: Any, lang: str = "zh_CN") -> str:
    """从 [{text, lang}, ...] 中取指定语言文本。"""
    if not items:
        return ""
    if isinstance(items, str):
        return items
    for item in items:
        if isinstance(item, dict) and item.get("lang") == lang:
            return str(item.get("text", ""))
    for item in items:
        if isinstance(item, dict) and item.get("text"):
            return str(item["text"])
    return ""


def _selector_text(selector: dict[str, Any]) -> str:
    texts: list[str] = []
    for option in selector.get("options") or []:
        if not isinstance(option, dict):
            continue
        for val in option.get("value") or []:
            text = val.get("text") if isinstance(val, dict) else None
            if text:
                texts.append(str(text))
    return "、".join(texts)


def _duration_text(seconds: Any) -> str:
    try:
        sec = int(seconds)
    except (TypeError, ValueError):
        return ""
    if sec <= 0:
        return ""
    hours, rest = divmod(sec, 3600)
    minutes = rest // 60
    if hours and minutes:
        return f"{hours}小时{minutes}分钟"
    if hours:
        return f"{hours}小时"
    return f"{minutes}分钟"


def parse_control_value(control: str, value: Any) -> str:
    """把单个控件的值转为可读文本。"""
    if value is None:
        return ""
    if not isinstance(value, dict):
        return str(value)
    kind = (control or "").lower()

    if kind in ("text", "textarea"):
        return str(value.get("text", "") or "")
    if kind == "number":
        return str(value.get("new_number", "") or "")
    if kind == "money":
        return str(value.get("new_money", "") or "")
    if kind == "date":
        date = value.get("date") or {}
        ts = date.get("s_timestamp")
        fmt = "%Y-%m-%d %H:%M" if date.get("type") == "hour" else "%Y-%m-%d"
        return fmt_ts(ts, fmt)
    if kind == "daterange":
        rng = value.get("date_range") or {}
        parts: list[str] = []
        if rng.get("new_begin"):
            parts.append(f"{fmt_ts(rng['new_begin'])} ~ {fmt_ts(rng.get('new_end'))}")
        text = _duration_text(rng.get("new_duration"))
        if text:
            parts.append(text)
        return " ".join(parts)
    if kind == "selector":
        return _selector_text(value.get("selector") or {})
    if kind == "contact":
        parts = []
        for member in value.get("members") or []:
            if isinstance(member, dict):
                parts.append(str(member.get("name") or member.get("userid") or ""))
        for dept in value.get("departments") or []:
            if isinstance(dept, dict):
                parts.append(str(dept.get("name") or dept.get("openapi_id") or ""))
        return "、".join(p for p in parts if p)
    if kind == "file":
        files = value.get("files") or []
        if not files:
            return ""
        ids = [str(f.get("file_id", "")) for f in files if isinstance(f, dict)]
        short = ", ".join(i[:16] for i in ids if i)
        return f"{len(files)} 个附件" + (f"（{short}）" if short else "")
    if kind == "table":
        rows: list[str] = []
        for child in value.get("children") or []:
            if not isinstance(child, dict):
                continue
            # children 中每一项代表明细中的一行, 其 list 为该行的各个控件;
            # 兼容 children 直接给出单个控件的旧结构。
            if isinstance(child.get("list"), list):
                items = child["list"]
            else:
                items = [child]
            cells = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                title = pick_text(item.get("title"))
                cell = parse_control_value(item.get("control", ""), item.get("value"))
                cells.append(f"{title}: {cell}" if title else cell)
            rows.append(" | ".join(c for c in cells if c))
        return " ;; ".join(rows)
    if kind == "location":
        loc = value.get("location") or {}
        return str(loc.get("address") or "")
    if kind == "vacation":
        vacation = value.get("vacation") or {}
        parts = []
        text = _selector_text(vacation.get("selector") or {})
        if text:
            parts.append(text)
        attendance = vacation.get("attendance") or {}
        rng = attendance.get("date_range") or {}
        if rng.get("new_begin"):
            parts.append(f"{fmt_ts(rng['new_begin'])} ~ {fmt_ts(rng.get('new_end'))}")
        type_text = ATTENDANCE_TYPE_TEXT.get(attendance.get("type", 0), "")
        if type_text:
            parts.append(type_text)
        return " ".join(parts)
    if kind == "attendance":
        attendance = value.get("attendance") or {}
        rng = attendance.get("date_range") or {}
        parts = []
        if rng.get("new_begin"):
            parts.append(f"{fmt_ts(rng['new_begin'])} ~ {fmt_ts(rng.get('new_end'))}")
        text = _duration_text(rng.get("new_duration"))
        if text:
            parts.append(text)
        type_text = ATTENDANCE_TYPE_TEXT.get(attendance.get("type", 0), "")
        if type_text:
            parts.append(type_text)
        return " ".join(parts)
    if kind == "punchcorrection":
        pc = value.get("punch_correction") or {}
        parts = [str(pc.get("state", "") or "")]
        if pc.get("time"):
            parts.append(fmt_ts(pc.get("time")))
        return " ".join(p for p in parts if p)
    if kind == "relatedapproval":
        return str((value.get("related_approval") or {}).get("sp_no", "") or "")
    if kind == "formula":
        return str(value.get("formula", "") or "")
    if kind == "tips":
        return ""
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def parse_form_contents(contents: Any) -> list[dict[str, Any]]:
    """把 apply_data.contents 规范化为字段列表。"""
    fields: list[dict[str, Any]] = []
    for item in contents or []:
        if not isinstance(item, dict):
            continue
        control = str(item.get("control", "") or "")
        title = pick_text(item.get("title")) or control
        value = parse_control_value(control, item.get("value"))
        fields.append({
            "id": str(item.get("id", "") or ""),
            "control": control,
            "title": title,
            "value": value,
            "raw": item.get("value"),
        })
    return fields


def _money_entries(
    control: str, value: Any, path: str = "", nested: bool = False
) -> list[tuple[str, float, bool]]:
    """递归收集金额控件 (含明细控件内的子金额)。"""
    entries: list[tuple[str, float, bool]] = []
    kind = (control or "").lower()
    if kind == "money":
        if isinstance(value, dict):
            try:
                entries.append((path or "金额", float(str(value.get("new_money"))), nested))
            except (TypeError, ValueError):
                pass
        return entries
    if kind == "table" and isinstance(value, dict):
        for child in value.get("children") or []:
            if not isinstance(child, dict):
                continue
            items = child.get("list") if isinstance(child.get("list"), list) else [child]
            for item in items:
                if not isinstance(item, dict):
                    continue
                sub_title = pick_text(item.get("title"))
                sub_path = f"{path} / {sub_title}" if path and sub_title else (sub_title or path)
                entries.extend(
                    _money_entries(item.get("control", ""), item.get("value"), sub_path, True)
                )
    return entries


def extract_money(fields: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """提取金额控件 (control=Money) 的金额，含明细控件内的子金额。"""
    money: list[dict[str, Any]] = []
    for field in fields:
        title = field.get("title", "") or "金额"
        for name, amount, nested in _money_entries(
            str(field.get("control", "")), field.get("raw"), title
        ):
            money.append({"title": name, "amount": amount, "nested": nested})
    return money


def form_to_text(fields: Iterable[dict[str, Any]]) -> str:
    """表单字段拼接为一段可检索文本。"""
    parts = []
    for field in fields:
        value = field.get("value")
        if value in (None, ""):
            continue
        parts.append(f"{field.get('title', '')}: {value}")
    return " | ".join(parts)


# ---------------------------------------------------------------------- #
# 审批流程解析
# ---------------------------------------------------------------------- #
def parse_flow(sp_record: Any) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    for index, node in enumerate(sp_record or []):
        if not isinstance(node, dict):
            continue
        details = []
        for detail in node.get("details") or []:
            if not isinstance(detail, dict):
                continue
            details.append({
                "approver": str((detail.get("approver") or {}).get("userid", "") or ""),
                "sp_status": detail.get("sp_status"),
                "speech": str(detail.get("speech", "") or ""),
                "sptime": detail.get("sptime", 0),
                "media_id": detail.get("media_id") or [],
            })
        nodes.append({
            "index": index,
            "approverattr": node.get("approverattr"),
            "approverattr_text": APPROVER_ATTR_TEXT.get(node.get("approverattr", 0), ""),
            "sp_status": node.get("sp_status"),
            "details": details,
        })
    return nodes


def flow_to_text(nodes: Iterable[dict[str, Any]]) -> str:
    """审批流程拼接为可读文本。"""
    parts: list[str] = []
    for node in nodes:
        mode = node.get("approverattr_text") or ""
        for detail in node.get("details") or []:
            status = SP_STATUS_TEXT.get(detail.get("sp_status", 0), "")
            chunk = (
                f"节点{node.get('index', 0) + 1}"
                f"{('(' + mode + ')') if mode else ''}"
                f" {detail.get('approver', '')} {status}"
            )
            if detail.get("sptime"):
                chunk += f" @{fmt_ts(detail.get('sptime'))}"
            if detail.get("speech"):
                chunk += f" 「{detail.get('speech')}」"
            parts.append(chunk)
    return " ; ".join(parts)


def finish_time_of(nodes: Iterable[dict[str, Any]]) -> int:
    """审批完成时间 = 最后一个有动作的审批节点时间。"""
    times = [
        int(detail.get("sptime") or 0)
        for node in nodes
        for detail in node.get("details") or []
        if detail.get("sptime")
    ]
    return max(times) if times else 0


# ---------------------------------------------------------------------- #
# 记录规范化
# ---------------------------------------------------------------------- #
def build_record(info: dict[str, Any], *, applier_name: str = "", department_name: str = "") -> dict[str, Any]:
    """把 getapprovaldetail 返回的 info 规范化为一条可落库记录。"""
    apply_data = info.get("apply_data") or {}
    fields = parse_form_contents(apply_data.get("contents"))
    money = extract_money(fields)
    top_money = [m for m in money if not m.get("nested")]
    money_total = (
        sum(m["amount"] for m in top_money) if top_money else sum(m["amount"] for m in money)
    )
    nodes = parse_flow(info.get("sp_record"))
    apply_time = int(info.get("apply_time") or 0)
    finish_time = finish_time_of(nodes)
    duration = finish_time - apply_time if (apply_time and finish_time) else 0

    applyer = info.get("applyer") or {}
    applier_userid = str(applyer.get("userid", "") or "")
    applier_partyid = str(applyer.get("partyid", "") or "")

    comments = []
    for item in info.get("comments") or []:
        if not isinstance(item, dict):
            continue
        comments.append({
            "userid": str((item.get("commentUserInfo") or {}).get("userid", "") or ""),
            "time": item.get("commenttime", 0),
            "content": str(item.get("commentcontent", "") or ""),
            "commentid": str(item.get("commentid", "") or ""),
            "media_id": item.get("media_id") or [],
        })

    notifyer = []
    for item in info.get("notifyer") or []:
        if isinstance(item, dict):
            notifyer.append(str(item.get("userid", "") or ""))

    file_count = 0
    for field in fields:
        if str(field.get("control", "")).lower() == "file":
            raw = field.get("raw") or {}
            file_count += len(raw.get("files") or []) if isinstance(raw, dict) else 0

    sp_status = int(info.get("sp_status") or 0)
    return {
        "sp_no": str(info.get("sp_no", "") or ""),
        "template_id": str(info.get("template_id", "") or ""),
        "template_name": str(info.get("sp_name", "") or ""),
        "sp_status": sp_status,
        "sp_status_text": SP_STATUS_TEXT.get(sp_status, f"状态{sp_status}"),
        "apply_time": apply_time,
        "apply_time_text": fmt_ts(apply_time),
        "apply_date": fmt_date(apply_time),
        "finish_time": finish_time,
        "finish_time_text": fmt_ts(finish_time),
        "duration_seconds": duration,
        "duration_hours": round(duration / 3600, 2) if duration else 0,
        "applier_userid": applier_userid,
        "applier_name": applier_name,
        "applier_partyid": applier_partyid,
        "applier_department": department_name,
        "form_fields": fields,
        "form_text": form_to_text(fields),
        "money": money,
        "money_total": round(money_total, 2),
        "flow": nodes,
        "flow_text": flow_to_text(nodes),
        "node_count": len(nodes),
        "file_count": file_count,
        "comments": comments,
        "comments_text": " ; ".join(
            f"{c['userid']} @{fmt_ts(c['time'])}: {c['content']}" for c in comments if c.get("content")
        ),
        "notifyer": notifyer,
        "notifyer_text": ", ".join(n for n in notifyer if n),
        "raw": info,
    }


# ---------------------------------------------------------------------- #
# 采集服务
# ---------------------------------------------------------------------- #
class ApprovalCollector:
    """负责把远端审批数据拉取下来（分页 + 并发 + 窗口切分）。"""

    def __init__(self, client: WeComClient, *, concurrency: int = DEFAULT_CONCURRENCY) -> None:
        self.client = client
        self.concurrency = max(1, int(concurrency))

    async def list_sp_no(
        self,
        *,
        start_time: int,
        end_time: int,
        filters: list[dict[str, str]] | None = None,
        max_records: int = 0,
    ) -> tuple[list[str], dict[str, Any]]:
        """按时间范围拉取审批单号，自动切分 31 天窗口并处理游标分页。"""
        sp_nos: list[str] = []
        seen: set[str] = set()
        windows = split_windows(start_time, end_time)
        api_calls = 0
        truncated = False

        for win_start, win_end in windows:
            cursor: int | str = ""
            while True:
                resp = await self.client.get_approval_no_list(
                    start_time=win_start,
                    end_time=win_end,
                    cursor=cursor,
                    size=PAGE_SIZE,
                    filters=filters,
                )
                api_calls += 1
                batch = [str(x) for x in (resp.get("sp_no_list") or [])]
                fresh = [x for x in batch if x and x not in seen]
                for item in fresh:
                    seen.add(item)
                    sp_nos.append(item)
                # 官方文档: 响应中无 new_next_cursor 字段即表示已拉取完毕
                next_cursor = resp.get("new_next_cursor")
                if next_cursor in (None, "", 0):
                    next_cursor = resp.get("next_cursor")
                if not batch:
                    break
                if max_records and len(sp_nos) >= max_records:
                    truncated = True
                    break
                if not next_cursor or str(next_cursor) == str(cursor):
                    break
                cursor = int(next_cursor)
            if truncated:
                break

        if max_records and len(sp_nos) > max_records:
            sp_nos = sp_nos[:max_records]
            truncated = True
        return sp_nos, {
            "windows": len(windows),
            "api_calls": api_calls,
            "total": len(sp_nos),
            "truncated": truncated,
        }

    async def fetch_details(
        self, sp_nos: list[str], *, on_error: Any = None
    ) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        """并发拉取审批详情。返回 (成功记录, 失败列表)。"""
        if not sp_nos:
            return [], []
        semaphore = asyncio.Semaphore(self.concurrency)

        async def one(sp_no: str) -> dict[str, Any] | None:
            async with semaphore:
                attempt = 0
                while True:
                    try:
                        resp = await self.client.get_approval_detail(sp_no)
                        break
                    except WeComError as e:
                        # 45033 = 接口并发超限: 同一企业的同一接口共享并发额度，
                        # 常驻服务与本进程叠加时也会触发。退避后重试即可恢复。
                        if (e.errcode == CONCURRENT_LIMIT_ERRCODE
                                and attempt < CONCURRENT_LIMIT_RETRIES):
                            attempt += 1
                            await asyncio.sleep(CONCURRENT_LIMIT_BACKOFF * (2 ** (attempt - 1)))
                            continue
                        return {"sp_no": sp_no, "errcode": e.errcode, "errmsg": e.errmsg}
                    except Exception as e:  # noqa: BLE001
                        return {
                            "sp_no": sp_no,
                            "errcode": -1,
                            "errmsg": f"{type(e).__name__}: {e}" if str(e) else type(e).__name__,
                        }
                info = resp.get("info") or {}
                return build_record(info)

        results = await asyncio.gather(*(one(sp_no) for sp_no in sp_nos))
        records: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        for item in results:
            if item is None:
                continue
            if "errcode" in item:
                errors.append({"sp_no": item["sp_no"], "errcode": str(item["errcode"]),
                               "errmsg": item.get("errmsg", "")})
                if on_error:
                    on_error(item)
            else:
                records.append(item)
        return records, errors


# ---------------------------------------------------------------------- #
# 联系人名称解析 (可选, 受应用可见范围限制)
# ---------------------------------------------------------------------- #
class ContactResolver:
    """按 userid 解析姓名 / 按姓名解析 userid，结果进程内缓存。

    通讯录接口受应用可见范围限制。失败采用**按目标**熔断（记住失败的
    userid / partyid），而非全局熔断——否则某个已离职成员查不到，
    会连带导致其余所有成员也解析不出来。
    """

    def __init__(self, client: WeComClient) -> None:
        self.client = client
        self._users: dict[str, str] = {}
        self._departments: dict[str, str] = {}
        self._loaded = False
        self._failed_users: set[str] = set()
        self._failed_departments: set[str] = set()
        self._dept_children: dict[str, list[str]] = {}
        self._dept_loaded = False

    async def ensure_loaded(self) -> None:
        if self._loaded:
            return
        try:
            data = await self.client.user_simplelist_all()
        except Exception:  # noqa: BLE001
            self._loaded = True
            return
        for user in data.get("userlist") or []:
            if isinstance(user, dict) and user.get("userid"):
                self._users[str(user["userid"])] = str(user.get("name", "") or "")
        self._loaded = True

    async def ensure_departments_loaded(self) -> dict[str, list[str]]:
        """加载部门树 (子部门 ID -> 子部门列表)。失败返回空字典。"""
        if self._dept_loaded:
            return self._dept_children
        self._dept_loaded = True
        try:
            data = await self.client.department_list()
        except Exception:  # noqa: BLE001
            return self._dept_children
        for dept in data.get("department") or []:
            if not isinstance(dept, dict):
                continue
            dept_id = str(dept.get("id", "") or "")
            parent = str(dept.get("parentid", "") or "")
            if not dept_id:
                continue
            self._dept_children.setdefault(parent, []).append(dept_id)
        return self._dept_children

    async def department_subtree(self, dept_id: str) -> set[str] | None:
        """返回该部门及其全部子部门的 ID 集合。

        企业微信服务端按 `department` 筛选时**包含子部门**的审批单，
        因此本地二次筛选必须按子树判断，否则父部门的结果会被误删。

        通讯录不可用时返回 None，表示「无法判断」——调用方应放弃本地过滤，
        改为信任服务端的筛选结果。
        """
        if not dept_id:
            return None
        children = await self.ensure_departments_loaded()
        if not children:
            return None
        result = {dept_id}
        queue = [dept_id]
        while queue:
            current = queue.pop()
            for child in children.get(current, []):
                if child not in result:
                    result.add(child)
                    queue.append(child)
        return result

    async def user_name(self, userid: str) -> str:
        if not userid:
            return ""
        if userid in self._users:
            return self._users[userid]
        if userid in self._failed_users:
            return ""
        await self.ensure_loaded()
        if userid in self._users:
            return self._users[userid]
        try:
            data = await self.client.user_get(userid)
        except Exception:  # noqa: BLE001
            self._failed_users.add(userid)
            return ""
        name = str((data or {}).get("name", "") or "")
        if name:
            self._users[userid] = name
        return name

    async def department_name(self, partyid: str) -> str:
        if not partyid:
            return ""
        if partyid in self._departments:
            return self._departments[partyid]
        if partyid in self._failed_departments:
            return ""
        try:
            data = await self.client.department_get(int(partyid))
        except Exception:  # noqa: BLE001
            self._failed_departments.add(partyid)
            return ""
        # 企业微信把部门信息嵌套在 `department` 对象内
        detail = (data or {}).get("department")
        if not isinstance(detail, dict):
            detail = data or {}
        name = str(detail.get("name", "") or "")
        self._departments[partyid] = name
        return name

    async def userids_by_name(self, name: str) -> list[str]:
        """按姓名（精确或包含）反查 userid 列表。"""
        if not name:
            return []
        await self.ensure_loaded()
        exact = [uid for uid, n in self._users.items() if n == name]
        if exact:
            return exact
        return [uid for uid, n in self._users.items() if name in n]

    async def fill_names(self, records: list[dict[str, Any]]) -> None:
        """为记录补全申请人与部门名称。"""
        for record in records:
            if not record.get("applier_name"):
                record["applier_name"] = await self.user_name(record.get("applier_userid", ""))
            if not record.get("applier_department"):
                record["applier_department"] = await self.department_name(
                    record.get("applier_partyid", "")
                )


def parse_status_filter(value: Any) -> list[int]:
    """把用户输入的状态转成状态码列表 (支持多选, 逗号分隔)。"""
    if value in (None, "", 0):
        return []
    if isinstance(value, int):
        return [value]
    text = str(value).strip()
    if not text:
        return []
    if re.fullmatch(r"\d+", text):
        return [int(text)]
    result: list[int] = []
    for part in re.split(r"[,，\s|]+", text):
        if not part:
            continue
        if part.isdigit():
            result.append(int(part))
            continue
        code = SP_STATUS_ALIASES.get(part)
        if code is None:
            for key, alias in SP_STATUS_ALIASES.items():
                if key.lower() == part.lower():
                    code = alias
                    break
        if code is not None:
            result.append(code)
    return result

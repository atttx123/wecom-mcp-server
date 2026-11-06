"""审批记录导出与统计分析。

支持三种导出格式:
  csv  UTF-8 BOM，Excel/WPS 可直接打开
  xlsx 多表 (明细 + 统计)，零第三方依赖
  json 结构化全量，便于二次处理

同时提供基于导出结果的分析能力: 分组统计、审批时长、金额汇总与异常提示。
"""

from __future__ import annotations

import csv
import json
import statistics
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

from .approval import SP_STATUS_TEXT
from .xlsx_writer import write_xlsx

#: (记录字段, 表头, 列宽)
COLUMNS: list[tuple[str, str, int]] = [
    ("sp_no", "审批单号", 22),
    ("template_name", "模板名称", 20),
    ("template_id", "模板ID", 36),
    ("sp_status_text", "审批状态", 10),
    ("apply_time_text", "提交时间", 20),
    ("finish_time_text", "完成时间", 20),
    ("duration_hours", "审批时长(小时)", 14),
    ("applier_name", "申请人", 12),
    ("applier_userid", "申请人UserID", 16),
    ("applier_department", "申请人部门", 16),
    ("money_total", "金额合计", 12),
    ("node_count", "审批节点数", 12),
    ("file_count", "附件数", 10),
    ("form_text", "表单内容", 60),
    ("flow_text", "审批流程", 50),
    ("comments_text", "备注", 40),
    ("notifyer_text", "抄送人", 20),
]

def _row(record: dict[str, Any]) -> list[Any]:
    return [record.get(key, "") for key, _title, _width in COLUMNS]


def _default_path(output_dir: Path, fmt: str, stamp: str) -> Path:
    return output_dir / f"approvals_{stamp}.{fmt}"


def export_records(
    records: Sequence[dict[str, Any]],
    *,
    output_format: str = "xlsx",
    output_path: str = "",
    output_dir: str = "data/exports",
    analysis: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """把审批记录导出为文件，返回导出信息。"""
    fmt = (output_format or "xlsx").lower().lstrip(".")
    if fmt not in ("csv", "xlsx", "json"):
        raise ValueError(f"不支持的导出格式: {output_format!r} (可选 csv/xlsx/json)")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    known_suffixes = {".csv", ".xlsx", ".json"}
    if output_path:
        target = Path(output_path).expanduser()
        if target.is_dir():
            target = _default_path(target, fmt, stamp)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            # 用 with_name 追加后缀, 避免 with_suffix 误吞文件名中的点 (如 "2026.09报销")
            if target.suffix.lower() not in known_suffixes:
                target = target.with_name(target.name + f".{fmt}")
    else:
        target = _default_path(Path(output_dir), fmt, stamp)
        target.parent.mkdir(parents=True, exist_ok=True)

    if fmt == "csv":
        _write_csv(records, target)
    elif fmt == "json":
        _write_json(records, target, analysis)
    else:
        _write_xlsx(records, target, analysis)

    return {
        "path": str(target.resolve()),
        "format": fmt,
        "rows": len(records),
        "size_bytes": target.stat().st_size if target.exists() else 0,
    }


def _write_csv(records: Sequence[dict[str, Any]], target: Path) -> None:
    with target.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow([title for _key, title, _width in COLUMNS])
        for record in records:
            writer.writerow(_row(record))


def _write_json(
    records: Sequence[dict[str, Any]], target: Path, analysis: dict[str, Any] | None
) -> None:
    payload = {
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "count": len(records),
        "records": list(records),
    }
    if analysis:
        payload["analysis"] = analysis
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_xlsx(
    records: Sequence[dict[str, Any]], target: Path, analysis: dict[str, Any] | None
) -> None:
    sheets: list[dict[str, Any]] = [{
        "name": "审批明细",
        "headers": [title for _key, title, _width in COLUMNS],
        "rows": [_row(r) for r in records],
        "widths": [width for _key, _title, width in COLUMNS],
    }]

    if analysis:
        by_template = analysis.get("by_template") or []
        sheets.append({
            "name": "按模板统计",
            "headers": ["模板名称", "单据数", "已通过", "已驳回", "审批中", "通过率", "平均时长(小时)", "金额合计"],
            "rows": [
                [
                    item.get("template_name", ""),
                    item.get("count", 0),
                    item.get("approved", 0),
                    item.get("rejected", 0),
                    item.get("pending", 0),
                    f"{item.get('pass_rate', 0) * 100:.1f}%" if item.get("pass_rate") is not None else "",
                    item.get("avg_duration_hours", 0),
                    item.get("money_total", 0),
                ]
                for item in by_template
            ],
            "widths": [22, 10, 10, 10, 10, 10, 14, 14],
        })

        by_status = analysis.get("by_status") or []
        sheets.append({
            "name": "按状态统计",
            "headers": ["审批状态", "单据数", "占比"],
            "rows": [
                [item.get("status_text", ""), item.get("count", 0),
                 f"{item.get('ratio', 0) * 100:.1f}%"]
                for item in by_status
            ],
            "widths": [14, 10, 10],
        })

        by_applicant = analysis.get("by_applicant") or []
        sheets.append({
            "name": "按申请人统计",
            "headers": ["申请人", "UserID", "部门", "单据数", "金额合计"],
            "rows": [
                [item.get("name", ""), item.get("userid", ""), item.get("department", ""),
                 item.get("count", 0), item.get("money_total", 0)]
                for item in by_applicant
            ],
            "widths": [14, 18, 18, 10, 14],
        })

        money_by_field = analysis.get("money_by_field") or []
        if money_by_field:
            sheets.append({
                "name": "金额汇总",
                "headers": ["模板名称", "金额字段", "出现次数", "合计"],
                "rows": [
                    [item.get("template_name", ""), item.get("title", ""),
                     item.get("count", 0), item.get("total", 0)]
                    for item in money_by_field
                ],
                "widths": [22, 18, 10, 14],
            })

    write_xlsx(target, sheets)


# ---------------------------------------------------------------------- #
# 统计分析
# ---------------------------------------------------------------------- #
def _pct(numerator: float, denominator: float) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def analyze_records(
    records: Sequence[dict[str, Any]], *, pending_days: int = 3
) -> dict[str, Any]:
    """对审批记录做分组统计与派生指标计算。"""
    total = len(records)
    if total == 0:
        return {
            "overview": {"total": 0},
            "by_status": [],
            "by_template": [],
            "by_applicant": [],
            "by_day": [],
            "by_month": [],
            "duration": {},
            "money_by_field": [],
            "findings": [],
            "markdown": "_无符合条件的审批记录。_",
        }

    # ---- 状态分布 ----
    status_counter: dict[int, int] = {}
    for record in records:
        status = int(record.get("sp_status", 0) or 0)
        status_counter[status] = status_counter.get(status, 0) + 1
    by_status = [
        {
            "status": status,
            "status_text": _status_text(status, records),
            "count": count,
            "ratio": _pct(count, total),
        }
        for status, count in sorted(status_counter.items(), key=lambda kv: -kv[1])
    ]

    # ---- 按模板 ----
    template_groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for record in records:
        key = (record.get("template_name", "") or "(未命名模板)",
               record.get("template_id", "") or "")
        template_groups.setdefault(key, []).append(record)

    by_template: list[dict[str, Any]] = []
    for (template_name, template_id), group in template_groups.items():
        statuses = [int(r.get("sp_status", 0) or 0) for r in group]
        approved = sum(1 for s in statuses if s == 2)
        rejected = sum(1 for s in statuses if s == 3)
        pending = sum(1 for s in statuses if s == 1)
        durations = [int(r.get("duration_seconds", 0) or 0) for r in group]
        durations = [d for d in durations if d > 0]
        by_template.append({
            "template_name": template_name,
            "template_id": template_id,
            "count": len(group),
            "approved": approved,
            "rejected": rejected,
            "pending": pending,
            "pass_rate": _pct(approved, approved + rejected),
            "avg_duration_hours": round(sum(durations) / len(durations) / 3600, 2) if durations else 0,
            "money_total": round(sum(float(r.get("money_total", 0) or 0) for r in group), 2),
        })
    by_template.sort(key=lambda x: -x["count"])

    # ---- 按申请人 ----
    applicant_groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for record in records:
        key = (
            record.get("applier_name", "") or "(未知)",
            record.get("applier_userid", "") or "",
            record.get("applier_department", "") or "",
        )
        applicant_groups.setdefault(key, []).append(record)
    by_applicant = [
        {
            "name": key[0],
            "userid": key[1],
            "department": key[2],
            "count": len(group),
            "money_total": round(sum(float(r.get("money_total", 0) or 0) for r in group), 2),
        }
        for key, group in applicant_groups.items()
    ]
    by_applicant.sort(key=lambda x: -x["count"])

    # ---- 按天 / 按月 ----
    day_counter: dict[str, int] = {}
    month_counter: dict[str, int] = {}
    for record in records:
        day = record.get("apply_date", "") or ""
        if day:
            day_counter[day] = day_counter.get(day, 0) + 1
            month = day[:7]
            month_counter[month] = month_counter.get(month, 0) + 1
    by_day = [{"date": d, "count": c} for d, c in sorted(day_counter.items())]
    by_month = [{"month": m, "count": c} for m, c in sorted(month_counter.items())]

    # ---- 审批时长 ----
    durations = sorted(
        int(r.get("duration_seconds", 0) or 0) for r in records
        if int(r.get("duration_seconds", 0) or 0) > 0
    )
    duration = {
        "samples": len(durations),
        "avg_hours": round(statistics.fmean(durations) / 3600, 2) if durations else 0,
        "median_hours": round(statistics.median(durations) / 3600, 2) if durations else 0,
        "max_hours": round(durations[-1] / 3600, 2) if durations else 0,
        "min_hours": round(durations[0] / 3600, 2) if durations else 0,
    }

    # ---- 金额 ----
    money_groups: dict[tuple[str, str], dict[str, Any]] = {}
    for record in records:
        template_name = record.get("template_name", "") or "(未命名模板)"
        for item in record.get("money") or []:
            key = (template_name, item.get("title", "") or "(未命名金额字段)")
            entry = money_groups.setdefault(key, {
                "count": 0, "total": 0.0, "nested": bool(item.get("nested")),
            })
            entry["count"] += 1
            entry["total"] += float(item.get("amount", 0) or 0)
    money_by_field = [
        {
            "template_name": key[0],
            "title": key[1],
            "count": value["count"],
            "total": round(value["total"], 2),
            "nested": value["nested"],
        }
        for key, value in money_groups.items()
    ]
    money_by_field.sort(key=lambda x: -x["total"])

    # ---- 派生结论 ----
    findings: list[str] = []
    approved_total = status_counter.get(2, 0)
    rejected_total = status_counter.get(3, 0)
    if approved_total + rejected_total:
        findings.append(
            f"整体通过率 {_pct(approved_total, approved_total + rejected_total) * 100:.1f}%"
            f"（通过 {approved_total} 单 / 驳回 {rejected_total} 单）。"
        )
    if by_template:
        worst = min(
            (t for t in by_template if t["approved"] + t["rejected"] > 0),
            key=lambda t: t["pass_rate"],
            default=None,
        )
        if worst:
            findings.append(
                f"通过率最低的模板为「{worst['template_name']}」，"
                f"通过率 {_pct(worst['approved'], worst['approved'] + worst['rejected']) * 100:.1f}%"
                f"（{worst['count']} 单）。"
            )
        slowest = max(
            (t for t in by_template if t["avg_duration_hours"] > 0),
            key=lambda t: t["avg_duration_hours"],
            default=None,
        )
        if slowest:
            findings.append(
                f"平均审批耗时最长的模板为「{slowest['template_name']}」，"
                f"平均 {slowest['avg_duration_hours']} 小时。"
            )
    pending_records = [
        r for r in records if int(r.get("sp_status", 0) or 0) == 1
    ]
    now = int(datetime.now().timestamp())
    stale = [
        r for r in pending_records
        if r.get("apply_time") and (now - int(r["apply_time"])) > pending_days * 86400
    ]
    if stale:
        findings.append(
            f"有 {len(stale)} 单处于「审批中」且已超过 {pending_days} 天未完成，"
            f"最早一单提交于 {min(str(r.get('apply_time_text', '')) for r in stale)}。"
        )
    if money_by_field:
        top_money = money_by_field[0]
        findings.append(
            f"金额合计最高的是「{top_money['template_name']} / {top_money['title']}」，"
            f"累计 {top_money['total']}（{top_money['count']} 次填写）。"
        )
    if by_applicant:
        top = by_applicant[0]
        findings.append(f"提交量最高的申请人为 {top['name']}，共 {top['count']} 单。")

    analysis = {
        "overview": {
            "total": total,
            "template_count": len(by_template),
            "applicant_count": len(by_applicant),
            "status_count": len(by_status),
            "day_span": len(by_day),
            "month_span": len(by_month),
            "amount_grand_total": round(
                sum(m["total"] for m in money_by_field if not m.get("nested")), 2
            ),
        },
        "by_status": by_status,
        "by_template": by_template,
        "by_applicant": by_applicant,
        "by_day": by_day,
        "by_month": by_month,
        "duration": duration,
        "money_by_field": money_by_field,
        "findings": findings,
    }
    analysis["markdown"] = to_markdown(analysis)
    return analysis


def _status_text(status: int, records: Sequence[dict[str, Any]]) -> str:
    for record in records:
        if int(record.get("sp_status", 0) or 0) == status and record.get("sp_status_text"):
            return str(record["sp_status_text"])
    return SP_STATUS_TEXT.get(status, str(status))


def _md_table(headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> str:
    lines = ["| " + " | ".join(str(h) for h in headers) + " |",
             "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        lines.append("| " + " | ".join("" if v is None else str(v) for v in row) + " |")
    return "\n".join(lines)


def to_markdown(analysis: dict[str, Any]) -> str:
    """把统计结果渲染为 markdown 文本 (供 MCP 客户端直接展示)。"""
    overview = analysis.get("overview") or {}
    if not overview.get("total"):
        return "_无符合条件的审批记录。_"

    blocks: list[str] = []
    blocks.append(
        f"共 **{overview['total']}** 条审批记录，"
        f"覆盖 {overview['template_count']} 个模板、{overview['applicant_count']} 位申请人、"
        f"{overview['status_count']} 种状态。"
    )

    by_status = analysis.get("by_status") or []
    if by_status:
        blocks.append("### 审批状态分布\n\n" + _md_table(
            ["状态", "单据数", "占比"],
            [(i["status_text"], i["count"], f"{i['ratio'] * 100:.1f}%") for i in by_status],
        ))

    by_template = analysis.get("by_template") or []
    if by_template:
        blocks.append("### 按模板统计\n\n" + _md_table(
            ["模板名称", "单据数", "通过", "驳回", "审批中", "通过率", "平均时长(h)", "金额合计"],
            [(
                t["template_name"], t["count"], t["approved"], t["rejected"], t["pending"],
                f"{t['pass_rate'] * 100:.1f}%" if t["approved"] + t["rejected"] else "-",
                t["avg_duration_hours"], t["money_total"],
            ) for t in by_template],
        ))

    by_applicant = (analysis.get("by_applicant") or [])[:15]
    if by_applicant:
        blocks.append("### 申请人 Top 15\n\n" + _md_table(
            ["申请人", "UserID", "部门", "单据数", "金额合计"],
            [(a["name"], a["userid"], a["department"], a["count"], a["money_total"])
             for a in by_applicant],
        ))

    duration = analysis.get("duration") or {}
    if duration.get("samples"):
        blocks.append("### 审批时长\n\n" + _md_table(
            ["样本数", "平均(h)", "中位(h)", "最长(h)", "最短(h)"],
            [(duration["samples"], duration["avg_hours"], duration["median_hours"],
              duration["max_hours"], duration["min_hours"])],
        ))

    money_by_field = analysis.get("money_by_field") or []
    if money_by_field:
        blocks.append("### 金额汇总\n\n" + _md_table(
            ["模板名称", "金额字段", "次数", "合计"],
            [(m["template_name"], m["title"], m["count"], m["total"]) for m in money_by_field],
        ))

    by_month = analysis.get("by_month") or []
    if by_month:
        blocks.append("### 按月提交量\n\n" + _md_table(
            ["月份", "单据数"],
            [(m["month"], m["count"]) for m in by_month],
        ))

    findings = analysis.get("findings") or []
    if findings:
        blocks.append("### 关键结论\n\n" + "\n".join(f"- {f}" for f in findings))

    return "\n\n".join(blocks)

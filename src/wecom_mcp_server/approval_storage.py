"""审批数据本地持久化 (SQLite)。

库文件: {data_dir}/wecom_approvals.db
表:
  approvals           审批单明细 (按 sp_no 去重, 重复拉取覆盖更新)
  approval_templates  审批模板元信息 (模板名 + 控件结构)

写入策略: 幂等 upsert —— 审批单状态会随流程变化, 重新拉取会刷新同一 sp_no 的记录。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Sequence

from .approval import fmt_ts

_JSON_FIELDS = ("form_fields", "flow", "comments", "money")


class ApprovalDB:
    """线程安全的审批数据 SQLite 存储。"""

    def __init__(self, data_dir: str) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "wecom_approvals.db"
        self._lock = threading.Lock()
        self._init_schema()

    # ------------------------------------------------------------------ #
    # 基础设施
    # ------------------------------------------------------------------ #
    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_schema(self) -> None:
        with self._lock, self._conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS approvals(
                  sp_no TEXT PRIMARY KEY,
                  template_id TEXT NOT NULL DEFAULT '',
                  template_name TEXT NOT NULL DEFAULT '',
                  sp_status INTEGER NOT NULL DEFAULT 0,
                  sp_status_text TEXT NOT NULL DEFAULT '',
                  apply_time INTEGER NOT NULL DEFAULT 0,
                  apply_date TEXT NOT NULL DEFAULT '',
                  finish_time INTEGER NOT NULL DEFAULT 0,
                  duration_seconds INTEGER NOT NULL DEFAULT 0,
                  applier_userid TEXT NOT NULL DEFAULT '',
                  applier_name TEXT NOT NULL DEFAULT '',
                  applier_partyid TEXT NOT NULL DEFAULT '',
                  applier_department TEXT NOT NULL DEFAULT '',
                  form_text TEXT NOT NULL DEFAULT '',
                  form_json TEXT NOT NULL DEFAULT '[]',
                  flow_text TEXT NOT NULL DEFAULT '',
                  flow_json TEXT NOT NULL DEFAULT '[]',
                  comments_json TEXT NOT NULL DEFAULT '[]',
                  money_json TEXT NOT NULL DEFAULT '[]',
                  money_total REAL NOT NULL DEFAULT 0,
                  node_count INTEGER NOT NULL DEFAULT 0,
                  file_count INTEGER NOT NULL DEFAULT 0,
                  notifyer_text TEXT NOT NULL DEFAULT '',
                  raw_json TEXT NOT NULL DEFAULT '{}',
                  fetched_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_approvals_apply_time ON approvals(apply_time);
                CREATE INDEX IF NOT EXISTS idx_approvals_template ON approvals(template_id);
                CREATE INDEX IF NOT EXISTS idx_approvals_applier ON approvals(applier_userid);
                CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(sp_status);

                CREATE TABLE IF NOT EXISTS approval_templates(
                  template_id TEXT PRIMARY KEY,
                  template_name TEXT NOT NULL DEFAULT '',
                  controls_json TEXT NOT NULL DEFAULT '[]',
                  fetched_at INTEGER NOT NULL
                );
                """
            )

    # ------------------------------------------------------------------ #
    # 写入
    # ------------------------------------------------------------------ #
    def upsert_approval(self, record: dict[str, Any]) -> None:
        self.upsert_approvals([record])

    def upsert_approvals(self, records: Sequence[dict[str, Any]]) -> int:
        if not records:
            return 0
        now = int(time.time())
        rows = []
        for record in records:
            rows.append((
                record.get("sp_no", ""),
                record.get("template_id", ""),
                record.get("template_name", ""),
                int(record.get("sp_status", 0) or 0),
                record.get("sp_status_text", ""),
                int(record.get("apply_time", 0) or 0),
                record.get("apply_date", ""),
                int(record.get("finish_time", 0) or 0),
                int(record.get("duration_seconds", 0) or 0),
                record.get("applier_userid", ""),
                record.get("applier_name", ""),
                record.get("applier_partyid", ""),
                record.get("applier_department", ""),
                record.get("form_text", ""),
                json.dumps(record.get("form_fields") or [], ensure_ascii=False),
                record.get("flow_text", ""),
                json.dumps(record.get("flow") or [], ensure_ascii=False),
                json.dumps(record.get("comments") or [], ensure_ascii=False),
                json.dumps(record.get("money") or [], ensure_ascii=False),
                float(record.get("money_total", 0) or 0),
                int(record.get("node_count", 0) or 0),
                int(record.get("file_count", 0) or 0),
                record.get("notifyer_text", ""),
                json.dumps(record.get("raw") or {}, ensure_ascii=False),
                now,
            ))
        with self._lock, self._conn() as conn:
            conn.executemany(
                "INSERT INTO approvals("
                "sp_no,template_id,template_name,sp_status,sp_status_text,"
                "apply_time,apply_date,finish_time,duration_seconds,"
                "applier_userid,applier_name,applier_partyid,applier_department,"
                "form_text,form_json,flow_text,flow_json,comments_json,money_json,money_total,"
                "node_count,file_count,notifyer_text,raw_json,fetched_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(sp_no) DO UPDATE SET "
                "template_id=excluded.template_id, template_name=excluded.template_name, "
                "sp_status=excluded.sp_status, sp_status_text=excluded.sp_status_text, "
                "apply_time=excluded.apply_time, apply_date=excluded.apply_date, "
                "finish_time=excluded.finish_time, duration_seconds=excluded.duration_seconds, "
                "applier_userid=excluded.applier_userid, applier_name=excluded.applier_name, "
                "applier_partyid=excluded.applier_partyid, "
                "applier_department=excluded.applier_department, "
                "form_text=excluded.form_text, form_json=excluded.form_json, "
                "flow_text=excluded.flow_text, flow_json=excluded.flow_json, "
                "comments_json=excluded.comments_json, money_json=excluded.money_json, "
                "money_total=excluded.money_total, node_count=excluded.node_count, "
                "file_count=excluded.file_count, notifyer_text=excluded.notifyer_text, "
                "raw_json=excluded.raw_json, fetched_at=excluded.fetched_at",
                rows,
            )
        return len(rows)

    def upsert_template(
        self, template_id: str, template_name: str = "", controls: Any = None
    ) -> None:
        if not template_id:
            return
        with self._lock, self._conn() as conn:
            conn.execute(
                "INSERT INTO approval_templates(template_id,template_name,controls_json,fetched_at) "
                "VALUES(?,?,?,?) ON CONFLICT(template_id) DO UPDATE SET "
                "template_name=CASE WHEN excluded.template_name<>'' THEN excluded.template_name "
                "ELSE approval_templates.template_name END, "
                "controls_json=CASE WHEN excluded.controls_json<>'[]' THEN excluded.controls_json "
                "ELSE approval_templates.controls_json END, "
                "fetched_at=excluded.fetched_at",
                (
                    template_id,
                    template_name or "",
                    json.dumps(controls if controls is not None else [], ensure_ascii=False),
                    int(time.time()),
                ),
            )

    # ------------------------------------------------------------------ #
    # 读取
    # ------------------------------------------------------------------ #
    @staticmethod
    def _decode(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["form_fields"] = json.loads(item.pop("form_json", "[]") or "[]")
        item["flow"] = json.loads(item.pop("flow_json", "[]") or "[]")
        item["comments"] = json.loads(item.pop("comments_json", "[]") or "[]")
        item["money"] = json.loads(item.pop("money_json", "[]") or "[]")
        try:
            item["raw"] = json.loads(item.pop("raw_json", "{}") or "{}")
        except json.JSONDecodeError:
            item["raw"] = {}
        item.pop("raw_json", None)
        # 展示型字段不落库, 读取时按时间戳派生 —— 否则历史数据永远缺这些字段
        item["apply_time_text"] = fmt_ts(item.get("apply_time"))
        item["finish_time_text"] = fmt_ts(item.get("finish_time"))
        duration = int(item.get("duration_seconds") or 0)
        item["duration_hours"] = round(duration / 3600, 2) if duration else 0
        return item

    def get_approval(self, sp_no: str) -> dict[str, Any] | None:
        with self._lock, self._conn() as conn:
            row = conn.execute("SELECT * FROM approvals WHERE sp_no=?", (sp_no,)).fetchone()
            return self._decode(row) if row else None

    def list_templates(self) -> list[dict[str, Any]]:
        """本地已知的审批模板 (来自已拉取的审批单)。"""
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                "SELECT template_id, template_name, COUNT(*) AS count, "
                "MAX(apply_time) AS last_apply_time "
                "FROM approvals WHERE template_id<>'' "
                "GROUP BY template_id, template_name ORDER BY count DESC"
            ).fetchall()
            return [dict(r) for r in rows]

    def template_ids_by_name(self, name: str) -> list[str]:
        """按模板名反查 template_id (精确或包含匹配)。"""
        if not name:
            return []
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                "SELECT DISTINCT template_id FROM approvals "
                "WHERE template_name = ? OR template_name LIKE ?",
                (name, f"%{name}%"),
            ).fetchall()
            return [str(r["template_id"]) for r in rows if r["template_id"]]

    def template_meta(self, template_id: str) -> dict[str, Any] | None:
        with self._lock, self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM approval_templates WHERE template_id=?", (template_id,)
            ).fetchone()
            if not row:
                return None
            item = dict(row)
            try:
                item["controls"] = json.loads(item.pop("controls_json") or "[]")
            except json.JSONDecodeError:
                item["controls"] = []
            return item

    def query(
        self,
        *,
        start_time: int = 0,
        end_time: int = 0,
        template_ids: Sequence[str] | None = None,
        template_name: str = "",
        applicant: str = "",
        sp_status: Sequence[int] | None = None,
        keyword: str = "",
        limit: int = 0,
        offset: int = 0,
        order: str = "desc",
    ) -> list[dict[str, Any]]:
        where: list[str] = []
        vals: list[Any] = []
        if start_time:
            where.append("apply_time>=?")
            vals.append(int(start_time))
        if end_time:
            where.append("apply_time<=?")
            vals.append(int(end_time))
        if template_ids:
            placeholders = ",".join("?" for _ in template_ids)
            where.append(f"template_id IN ({placeholders})")
            vals.extend(list(template_ids))
        if template_name:
            where.append("template_name LIKE ?")
            vals.append(f"%{template_name}%")
        if applicant:
            where.append("(applier_userid=? OR applier_name LIKE ?)")
            vals.extend([applicant, f"%{applicant}%"])
        if sp_status:
            placeholders = ",".join("?" for _ in sp_status)
            where.append(f"sp_status IN ({placeholders})")
            vals.extend([int(s) for s in sp_status])
        if keyword:
            where.append(
                "(form_text LIKE ? OR template_name LIKE ? OR applier_name LIKE ? "
                "OR sp_no LIKE ? OR flow_text LIKE ?)"
            )
            like = f"%{keyword}%"
            vals.extend([like, like, like, like, like])
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        direction = "ASC" if str(order).lower() == "asc" else "DESC"
        sql = f"SELECT * FROM approvals{clause} ORDER BY apply_time {direction}, sp_no ASC"
        if limit:
            sql += " LIMIT ? OFFSET ?"
            vals.extend([int(limit), int(offset or 0)])
        with self._lock, self._conn() as conn:
            rows = conn.execute(sql, vals).fetchall()
            return [self._decode(r) for r in rows]

    def count(self) -> int:
        with self._lock, self._conn() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0])

    def time_range(self) -> tuple[int, int]:
        """本地数据的 (最早, 最晚) 提交时间。"""
        with self._lock, self._conn() as conn:
            row = conn.execute(
                "SELECT MIN(apply_time) AS lo, MAX(apply_time) AS hi FROM approvals"
            ).fetchone()
            return int(row["lo"] or 0), int(row["hi"] or 0)


_db: ApprovalDB | None = None


def get_approval_db(settings: Any = None) -> ApprovalDB:
    """全局单例。"""
    global _db
    if _db is None:
        from .config import get_settings

        _db = ApprovalDB((settings or get_settings()).data_dir)
    return _db

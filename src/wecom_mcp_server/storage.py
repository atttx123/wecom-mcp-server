"""SQLite 消息持久化 + 文件落盘管理（仅 HTTP transport 模式启用）。

数据库: messages_YYYY 按自然年分表，元数据存 _meta / _year_tables。
文件: {data_dir}/files/{year}/{month}/{name}，file_path 存相对路径。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Settings
from .crypto import ReceivedMessage

_UNSAFE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_EXT_BY_TYPE = {"image": "jpg", "voice": "amr", "video": "mp4", "file": ""}


def sanitize_filename(name: str, max_len: int = 100) -> str:
    name = os.path.basename(name)
    name = _UNSAFE_CHARS.sub("_", name).strip(" ._") or "unnamed"
    if len(name) <= max_len:
        return name
    stem, dot, ext = name.rpartition(".")
    if dot and len(ext) < 20:
        return stem[: max_len - len(ext) - 1] + "." + ext
    return name[:max_len]


class MessageDB:
    """线程安全的 SQLite 消息存储，按自然年分表。"""

    def __init__(self, data_dir: str) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.files_dir = self.data_dir / "files"
        self.files_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "wecom_messages.db"
        self._lock = threading.Lock()
        with self._lock, self._conn() as c:
            c.executescript(
                "CREATE TABLE IF NOT EXISTS _meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);"
                "CREATE TABLE IF NOT EXISTS _year_tables("
                "  year INTEGER PRIMARY KEY, table_name TEXT NOT NULL,"
                "  created_at INTEGER NOT NULL, message_count INTEGER DEFAULT 0);"
                "INSERT OR IGNORE INTO _meta(key,value) VALUES('schema_version','1');"
            )

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    @staticmethod
    def _dt(ts: int) -> datetime:
        return datetime.fromtimestamp(ts, tz=timezone.utc)

    def _ensure_year_table(self, c: sqlite3.Connection, year: int) -> str:
        table = f"messages_{year}"
        if c.execute("SELECT 1 FROM _year_tables WHERE year=?", (year,)).fetchone():
            return table
        c.executescript(
            f"CREATE TABLE IF NOT EXISTS {table}("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT, msg_id TEXT NOT NULL UNIQUE,"
            "  msg_type TEXT NOT NULL, event TEXT, from_user TEXT NOT NULL,"
            "  to_user TEXT NOT NULL, agent_id TEXT, create_time INTEGER NOT NULL,"
            "  received_time INTEGER NOT NULL, year INTEGER NOT NULL, month INTEGER NOT NULL,"
            "  content TEXT, media_id TEXT, file_name TEXT, file_ext TEXT,"
            "  file_path TEXT, file_size INTEGER, file_md5 TEXT,"
            "  file_status TEXT DEFAULT 'pending', pic_url TEXT, thumb_media_id TEXT,"
            "  voice_format TEXT, location_x REAL, location_y REAL,"
            "  location_scale INTEGER, location_label TEXT,"
            "  link_title TEXT, link_description TEXT, link_url TEXT,"
            "  raw_xml TEXT, extra_json TEXT,"
            "  created_at INTEGER NOT NULL DEFAULT (strftime('%s','now')));"
            f"CREATE INDEX IF NOT EXISTS idx_{year}_create_time ON {table}(create_time);"
            f"CREATE INDEX IF NOT EXISTS idx_{year}_from_user ON {table}(from_user);"
            f"CREATE INDEX IF NOT EXISTS idx_{year}_msg_type ON {table}(msg_type);"
            f"CREATE INDEX IF NOT EXISTS idx_{year}_file_status ON {table}(file_status);"
        )
        c.execute(
            "INSERT OR REPLACE INTO _year_tables(year,table_name,created_at,message_count) VALUES(?,?,?,0)",
            (year, table, int(time.time())),
        )
        return table

    def _existing_tables(self, c: sqlite3.Connection, start_ts: int, end_ts: int) -> list[str]:
        years = range(self._dt(start_ts).year, self._dt(end_ts).year + 1)
        tables = []
        for y in years:
            if c.execute("SELECT 1 FROM _year_tables WHERE year=?", (y,)).fetchone():
                tables.append(f"messages_{y}")
        return tables

    # ------------------------------------------------------------------ #
    # 入库
    # ------------------------------------------------------------------ #
    def insert_message(self, msg: ReceivedMessage) -> bool:
        """返回 True=新插入, False=msg_id 已存在。"""
        dt = self._dt(msg.create_time)
        year, month = dt.year, dt.month
        extra = dict(msg.extra)
        is_file = msg.msg_type == "file"
        file_name = extra.pop("FileName", "") if is_file else ""
        file_md5 = extra.pop("FileMd5", "") if is_file else ""

        # 各类型专属字段提取
        fields = {
            "media_id": extra.get("MediaId", ""),
            "pic_url": extra.get("PicUrl", ""),
            "thumb_media_id": extra.get("ThumbMediaId", ""),
            "voice_format": extra.get("Format", ""),
            "location_x": extra.get("Location_X", ""),
            "location_y": extra.get("Location_Y", ""),
            "location_scale": extra.get("Scale", ""),
            "location_label": extra.get("Label", ""),
            "link_title": extra.get("Title", ""),
            "link_description": extra.get("Description", ""),
            "link_url": extra.get("Url", ""),
        }
        # 文件扩展名
        if is_file and file_name:
            fields["file_ext"] = os.path.splitext(file_name)[1].lstrip(".")
        elif msg.msg_type == "image":
            fields["file_ext"] = "jpg"
        elif msg.msg_type == "voice":
            fields["file_ext"] = fields["voice_format"] or "amr"
        elif msg.msg_type == "video":
            fields["file_ext"] = "mp4"

        event = extra.get("Event", "") if msg.msg_type == "event" else None
        has_file = msg.msg_type in ("image", "voice", "video", "file")
        file_status = "pending" if has_file else "not_applicable"

        extracted = {"MediaId", "PicUrl", "ThumbMediaId", "Format",
                     "Location_X", "Location_Y", "Scale", "Label",
                     "Title", "Description", "Url", "Event", "FileName", "FileMd5"}
        extra_json = json.dumps(
            {k: v for k, v in extra.items() if k not in extracted}, ensure_ascii=False
        )

        def _v(s: Any) -> Any:
            return s or None

        def _num(s: str, ctor=float) -> Any:
            return ctor(s) if s else None

        with self._lock, self._conn() as c:
            table = self._ensure_year_table(c, year)
            cur = c.execute(
                f"INSERT OR IGNORE INTO {table}(msg_id,msg_type,event,from_user,to_user,agent_id,"
                "create_time,received_time,year,month,content,"
                "media_id,file_name,file_ext,file_path,file_size,file_md5,file_status,"
                "pic_url,thumb_media_id,voice_format,"
                "location_x,location_y,location_scale,location_label,"
                "link_title,link_description,link_url,raw_xml,extra_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    msg.msg_id, msg.msg_type, event,
                    msg.from_user, msg.to_user, msg.agent_id,
                    msg.create_time, int(time.time()), year, month, msg.content,
                    _v(fields["media_id"]), _v(file_name), _v(fields.get("file_ext", "")),
                    None, None, _v(file_md5), file_status,
                    _v(fields["pic_url"]), _v(fields["thumb_media_id"]), _v(fields["voice_format"]),
                    _num(fields["location_x"]), _num(fields["location_y"]),
                    _num(fields["location_scale"], int), _v(fields["location_label"]),
                    _v(fields["link_title"]), _v(fields["link_description"]), _v(fields["link_url"]),
                    msg.raw_xml, extra_json,
                ),
            )
            if cur.rowcount > 0:
                c.execute("UPDATE _year_tables SET message_count=message_count+1 WHERE year=?", (year,))
                return True
            return False

    def update_file_info(
        self, msg_id: str, create_time: int, *,
        file_path: str | None = None, file_size: int | None = None,
        file_md5: str | None = None, file_status: str = "downloaded",
    ) -> None:
        year = self._dt(create_time).year
        with self._lock, self._conn() as c:
            table = self._ensure_year_table(c, year)
            sets, vals = ["file_status=?"], [file_status]
            for col, val in [("file_path", file_path), ("file_size", file_size), ("file_md5", file_md5)]:
                if val is not None:
                    sets.append(f"{col}=?")
                    vals.append(val)
            vals.append(msg_id)
            c.execute(f"UPDATE {table} SET {','.join(sets)} WHERE msg_id=?", vals)

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #
    def search_messages(
        self, *, start_time: int = 0, end_time: int = 0,
        from_user: str = "", msg_type: str = "", keyword: str = "",
        limit: int = 50, offset: int = 0,
    ) -> list[dict[str, Any]]:
        if end_time <= 0:
            end_time = int(time.time()) + 86400
        start_time = max(start_time, 0)
        with self._lock, self._conn() as c:
            tables = self._existing_tables(c, start_time, end_time)
            if not tables:
                return []
            where, vals = ["create_time>=?", "create_time<=?"], [start_time, end_time]
            if from_user:
                where.append("from_user=?")
                vals.append(from_user)
            if msg_type:
                where.append("msg_type=?")
                vals.append(msg_type)
            if keyword:
                where.append("content LIKE ?")
                vals.append(f"%{keyword}%")
            w = " AND ".join(where)
            union = " UNION ALL ".join(f"SELECT * FROM {t} WHERE {w}" for t in tables)
            vals.extend([limit, offset])
            return [dict(r) for r in c.execute(
                f"SELECT * FROM ({union}) ORDER BY create_time DESC LIMIT ? OFFSET ?", vals
            )]

    def get_message_by_id(self, create_year: int, msg_id: str) -> dict[str, Any] | None:
        with self._lock, self._conn() as c:
            table = self._ensure_year_table(c, create_year)
            row = c.execute(f"SELECT * FROM {table} WHERE msg_id=?", (msg_id,)).fetchone()
            return dict(row) if row else None

    def list_years(self) -> list[dict[str, Any]]:
        with self._lock, self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT year,table_name,message_count FROM _year_tables ORDER BY year"
            )]

    def get_stats(self, *, start_time: int = 0, end_time: int = 0) -> dict[str, Any]:
        if end_time <= 0:
            end_time = int(time.time()) + 86400
        with self._lock, self._conn() as c:
            tables = self._existing_tables(c, start_time, end_time)
            if not tables:
                return {"total": 0, "by_type": {}, "by_from_user": {}, "by_month": {}}
            union = " UNION ALL ".join(
                f"SELECT msg_type,from_user,month,create_time FROM {t} "
                f"WHERE create_time>={start_time} AND create_time<={end_time}"
                for t in tables
            )
            return {
                "total": c.execute(f"SELECT COUNT(*) FROM ({union})").fetchone()[0],
                "by_type": dict(c.execute(f"SELECT msg_type,COUNT(*) FROM ({union}) GROUP BY msg_type")),
                "by_from_user": dict(c.execute(
                    f"SELECT from_user,COUNT(*) FROM ({union}) GROUP BY from_user ORDER BY COUNT(*) DESC LIMIT 20"
                )),
                "by_month": dict(c.execute(f"SELECT month,COUNT(*) FROM ({union}) GROUP BY month")),
            }

    # ------------------------------------------------------------------ #
    # 文件落盘
    # ------------------------------------------------------------------ #
    def build_file_relpath(self, msg_type: str, msg_id: str, create_time: int, file_name: str = "") -> str:
        dt = self._dt(create_time)
        if file_name:
            safe = sanitize_filename(file_name)
        else:
            ext = _EXT_BY_TYPE.get(msg_type, "bin")
            safe = f"{msg_type}_{msg_id}.{ext}" if ext else f"{msg_type}_{msg_id}"
        return f"files/{dt.year}/{dt.month:02d}/{safe}"

    def save_file_bytes(self, rel_path: str, data: bytes) -> tuple[str, int, str]:
        abs_path = self.data_dir / rel_path
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_bytes(data)
        return rel_path, len(data), hashlib.md5(data).hexdigest()

    def abs_path(self, rel_path: str) -> Path:
        return self.data_dir / rel_path


_db: MessageDB | None = None


def get_message_db(settings: Settings | None = None) -> MessageDB:
    global _db
    if _db is None:
        from .config import get_settings
        _db = MessageDB((settings or get_settings()).data_dir)
    return _db

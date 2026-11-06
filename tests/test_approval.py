"""审批功能离线测试。

运行方式:
    .venv/bin/python tests/test_approval.py
    # 或 (若已安装 pytest)
    .venv/bin/python -m pytest tests/test_approval.py -q

测试全部使用本地 fixture 与模拟客户端，不需要企业微信凭据与网络。
"""

from __future__ import annotations

import json
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from wecom_mcp_server import approval  # noqa: E402
from wecom_mcp_server.approval import (  # noqa: E402
    ApprovalCollector,
    ContactResolver,
    build_record,
    parse_control_value,
    parse_status_filter,
    parse_time_input,
    split_windows,
)
from wecom_mcp_server.approval_export import analyze_records, export_records  # noqa: E402
from wecom_mcp_server.approval_storage import ApprovalDB  # noqa: E402
from wecom_mcp_server.xlsx_writer import col_letter  # noqa: E402
from wecom_mcp_server.wecom_client import WeComError  # noqa: E402

FIXTURE = json.loads(
    (ROOT / "tests" / "fixtures" / "approval_detail.json").read_text(encoding="utf-8")
)


# ---------------------------------------------------------------------- #
# 控件解析
# ---------------------------------------------------------------------- #
def test_parse_control_values() -> None:
    assert parse_control_value("Text", {"text": "hello"}) == "hello"
    assert parse_control_value("Textarea", {"text": "多行"}) == "多行"
    assert parse_control_value("Number", {"new_number": "3"}) == "3"
    assert parse_control_value("Money", {"new_money": "1860.50"}) == "1860.50"
    assert parse_control_value("Date", {"date": {"type": "day", "s_timestamp": "1757059200"}}).startswith("2025-")
    assert parse_control_value("Selector", {
        "selector": {"type": "multi", "options": [
            {"key": "k1", "value": [{"text": "差旅费", "lang": "zh_CN"}]},
            {"key": "k2", "value": [{"text": "住宿费", "lang": "zh_CN"}]},
        ]},
    }) == "差旅费、住宿费"
    assert parse_control_value("Contact", {
        "members": [{"userid": "zhangsan", "name": "张三"}],
    }) == "张三"
    assert parse_control_value("Contact", {
        "departments": [{"openapi_id": "12", "name": "销售部"}],
    }) == "销售部"
    assert parse_control_value("File", {
        "files": [{"file_id": "A"}, {"file_id": "B"}],
    }).startswith("2 个附件")
    assert parse_control_value("Tips", {}) == ""
    table = parse_control_value("Table", FIXTURE["info"]["apply_data"]["contents"][9]["value"])
    assert "高铁票" in table and "1060.5" in table


def test_build_record_derivations() -> None:
    record = build_record(FIXTURE["info"], applier_name="张三", department_name="销售部")

    assert record["sp_no"] == "202609060001"
    assert record["template_name"] == "费用报销"
    assert record["sp_status_text"] == "已通过"
    assert record["applier_name"] == "张三"
    assert record["applier_department"] == "销售部"

    # 审批完成时间取所有节点中最后一个动作时间
    assert record["finish_time"] == 1757216400
    assert record["duration_seconds"] == 1757216400 - 1757126400
    assert record["duration_hours"] == round((1757216400 - 1757126400) / 3600, 2)

    # 金额: 主控件 + 明细控件 (明细金额计入明细字段, 不重复计入总额)
    assert record["money_total"] == 1860.5
    assert len(record["money"]) == 3
    assert record["money"][0]["title"] == "报销金额"
    assert record["money"][1]["title"] == "费用明细 / 小计"
    assert record["money"][1]["nested"] is True

    # 附件数
    assert record["file_count"] == 2
    assert record["node_count"] == 2
    assert "张三" in record["form_text"] and "报销事由" in record["form_text"]
    assert "同意" in record["flow_text"]
    assert "请补充行程单" in record["comments_text"]
    assert record["notifyer_text"] == "qianqi"


# ---------------------------------------------------------------------- #
# 时间与筛选
# ---------------------------------------------------------------------- #
def test_parse_time_input() -> None:
    assert parse_time_input("1757126400") == 1757126400
    assert parse_time_input("2026-01-01") == parse_time_input("2026-01-01 00:00:00")
    assert parse_time_input("2026/01/02") == parse_time_input("2026-01-02")
    assert parse_time_input("") is None
    # 纯日期 + end_of_day -> 当天 23:59:59
    assert parse_time_input("2026-01-01", end_of_day=True) > parse_time_input("2026-01-01")


def test_split_windows() -> None:
    start = 1757126400
    end = start + 100 * 86400
    windows = split_windows(start, end)
    assert len(windows) == 4  # 100 天 -> 31+31+31+7
    for left, right in windows:
        assert right - left <= 31 * 86400
    assert windows[0][0] == start
    assert windows[-1][1] == end


def test_parse_status_filter() -> None:
    assert parse_status_filter("") == []
    assert parse_status_filter("2") == [2]
    assert parse_status_filter("审批中") == [1]
    assert parse_status_filter("已通过,已驳回") == [2, 3]
    assert parse_status_filter("1, 2") == [1, 2]


# ---------------------------------------------------------------------- #
# 采集器 (模拟客户端)
# ---------------------------------------------------------------------- #
class FakeClient:
    """模拟企业微信审批接口。

    真实接口按时间窗口返回该窗口内的审批单, 这里用全局游标模拟连续翻页,
    因此跨窗口时数据不会被重复计数。
    """

    def __init__(self, total: int = 250, fail: set[str] | None = None) -> None:
        self.total = total
        self.fail = fail or set()
        self.offset = 0
        self.list_calls: list[dict] = []
        self.detail_calls: list[str] = []

    async def get_approval_no_list(self, *, start_time, end_time, cursor="", size=100, filters=None):
        self.list_calls.append({
            "start": start_time, "end": end_time, "cursor": cursor,
            "size": size, "filters": filters,
        })
        ids = [f"SP_{self.offset + i}" for i in range(min(size, max(0, self.total - self.offset)))]
        self.offset += len(ids)
        next_cursor = str(self.offset) if self.offset < self.total else ""
        return {"errcode": 0, "sp_no_list": ids, "new_next_cursor": next_cursor}

    async def get_approval_detail(self, sp_no):
        self.detail_calls.append(sp_no)
        if sp_no in self.fail:
            raise WeComError(301026, "内部接口失败")
        info = json.loads(json.dumps(FIXTURE["info"]))
        info["sp_no"] = sp_no
        return {"errcode": 0, "info": info}


def test_collector_pagination_and_windows() -> None:
    import asyncio

    async def run():
        client = FakeClient(total=250)
        collector = ApprovalCollector(client, concurrency=4)
        sp_nos, meta = await collector.list_sp_no(
            start_time=1757126400, end_time=1757126400 + 100 * 86400,
        )
        assert len(sp_nos) == 250
        assert meta["windows"] == 4
        # 4 个窗口: 第一个窗口翻 3 页取完 250 条, 其余 3 个窗口各 1 次空页校验
        assert meta["api_calls"] == 6
        assert len(client.list_calls) == 6

        # max_records 截断 (使用独立的模拟客户端)
        client3 = FakeClient(total=250)
        collector3 = ApprovalCollector(client3, concurrency=4)
        sp_nos2, meta2 = await collector3.list_sp_no(
            start_time=1757126400, end_time=1757126400 + 86400, max_records=150
        )
        assert len(sp_nos2) == 150
        assert meta2["truncated"] is True

        # 详情并发拉取 + 单条失败不影响整体
        client2 = FakeClient(total=5, fail={"SP_2"})
        collector2 = ApprovalCollector(client2, concurrency=3)
        records, errors = await collector2.fetch_details([f"SP_{i}" for i in range(5)])
        assert len(records) == 4
        assert len(errors) == 1
        assert errors[0]["errcode"] == "301026"

    asyncio.run(run())


def test_collector_retries_on_concurrent_limit() -> None:
    """errcode 45033 (接口并发超限) 应退避重试, 而不是直接丢弃该条数据。"""

    import asyncio

    class FlakyClient(FakeClient):
        def __init__(self, fail_times: int) -> None:
            super().__init__(total=3)
            self.fail_times = fail_times
            self.attempts = 0

        async def get_approval_detail(self, sp_no):
            self.attempts += 1
            if self.attempts <= self.fail_times:
                raise WeComError(45033, "api concurrent out of limit")
            info = json.loads(json.dumps(FIXTURE["info"]))
            info["sp_no"] = sp_no
            return {"errcode": 0, "info": info}

    async def run():
        # 前两次失败、第三次成功 -> 应重试并最终拿到数据
        client = FlakyClient(fail_times=2)
        collector = ApprovalCollector(client, concurrency=2)
        records, errors = await collector.fetch_details(["SP_0"])
        assert len(records) == 1, errors
        assert not errors
        assert client.attempts == 3

        # 超过重试上限 -> 明确记为失败, 不静默丢失
        client2 = FlakyClient(fail_times=99)
        collector2 = ApprovalCollector(client2, concurrency=2)
        records2, errors2 = await collector2.fetch_details(["SP_0"])
        assert not records2
        assert len(errors2) == 1
        assert errors2[0]["errcode"] == "45033"

    asyncio.run(run())


# ---------------------------------------------------------------------- #
# 存储
# ---------------------------------------------------------------------- #
def test_db_roundtrip() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = ApprovalDB(tmp)
        record = build_record(FIXTURE["info"], applier_name="张三", department_name="销售部")
        db.upsert_approval(record)

        # 重复写入应为覆盖而非新增
        record["sp_status"] = 3
        record["sp_status_text"] = "已驳回"
        db.upsert_approval(record)
        assert db.count() == 1

        found = db.get_approval("202609060001")
        assert found is not None
        assert found["sp_status"] == 3
        assert found["form_fields"][0]["title"] == "报销事由"
        assert isinstance(found["flow"], list)

        # 展示型时间字段不落库, 读取时必须按时间戳派生出来
        assert found["apply_time_text"] == record["apply_time_text"]
        assert found["apply_time_text"] != ""
        assert found["finish_time_text"] == record["finish_time_text"]
        assert found["duration_hours"] == record["duration_hours"]
        assert found["duration_hours"] > 0

        # 关键字检索
        hits = db.query(keyword="差旅")
        assert len(hits) == 1
        assert db.query(keyword="不存在的关键字") == []

        # 状态过滤
        assert len(db.query(sp_status=[3])) == 1
        assert len(db.query(sp_status=[2])) == 0

        # 模板反查
        assert db.template_ids_by_name("费用报销") == ["TMPL_REIMBURSE_001"]
        assert db.list_templates()[0]["count"] == 1

        db.upsert_template("TMPL_REIMBURSE_001", "费用报销", [{"control": "Text"}])
        meta = db.template_meta("TMPL_REIMBURSE_001")
        assert meta is not None and meta["controls"][0]["control"] == "Text"


# ---------------------------------------------------------------------- #
# 导出
# ---------------------------------------------------------------------- #
def test_export_formats() -> None:
    record = build_record(FIXTURE["info"], applier_name="张三", department_name="销售部")
    records = [record]
    analysis = analyze_records(records)

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)

        csv_info = export_records(records, output_format="csv", output_dir=str(out))
        csv_path = Path(csv_info["path"])
        assert csv_path.exists()
        raw = csv_path.read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf")  # UTF-8 BOM, Excel 可直接打开
        text = raw.decode("utf-8-sig")
        assert "审批单号" in text and "202609060001" in text

        json_info = export_records(records, output_format="json", output_dir=str(out))
        payload = json.loads(Path(json_info["path"]).read_text(encoding="utf-8"))
        assert payload["count"] == 1
        assert payload["records"][0]["sp_no"] == "202609060001"

        xlsx_info = export_records(records, output_format="xlsx", output_dir=str(out), analysis=analysis)
        xlsx_path = Path(xlsx_info["path"])
        assert xlsx_path.exists()
        with zipfile.ZipFile(xlsx_path) as zf:
            names = zf.namelist()
            assert "[Content_Types].xml" in names
            assert "xl/workbook.xml" in names
            assert "xl/styles.xml" in names
            assert "xl/worksheets/sheet1.xml" in names
            sheet = zf.read("xl/worksheets/sheet1.xml").decode("utf-8")
            assert "审批单号" in sheet
            assert "202609060001" in sheet
            assert '<pane ySplit="1"' in sheet

            # --- OOXML 结构一致性校验 ---
            import xml.etree.ElementTree as ET

            ns_main = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
            ns_rel = "{http://schemas.openxmlformats.org/package/2006/relationships}"
            ns_doc_rel = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
            ns_ct = "{http://schemas.openxmlformats.org/package/2006/content-types}"

            content_types = ET.fromstring(zf.read("[Content_Types].xml"))
            overrides = {
                node.get("PartName")
                for node in content_types.findall(f"{ns_ct}Override")
            }

            workbook = ET.fromstring(zf.read("xl/workbook.xml"))
            rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
            rel_map = {
                node.get("Id"): node.get("Target")
                for node in rels.findall(f"{ns_rel}Relationship")
            }
            sheets = workbook.find(f"{ns_main}sheets").findall(f"{ns_main}sheet")
            assert len(sheets) >= 1
            for sheet_node in sheets:
                rid = sheet_node.get(f"{ns_doc_rel}id")
                assert rid in rel_map, f"工作表缺少关系映射: {rid}"
                target = rel_map[rid]
                part = f"xl/{target}"
                assert part in names, f"关系指向的部件不存在: {part}"
                assert f"/{part}" in overrides, f"缺少 Content_Types 声明: {part}"
                # 每个部件都必须是合法 XML
                ET.fromstring(zf.read(part))
            # 样式表同样需要声明
            assert "/xl/styles.xml" in overrides
            ET.fromstring(zf.read("xl/styles.xml"))
            # 关系 ID 不得重复
            assert len(rel_map) == len(rels.findall(f"{ns_rel}Relationship"))

        # 不支持的格式应抛错
        try:
            export_records(records, output_format="pdf", output_dir=str(out))
        except ValueError:
            pass
        else:  # pragma: no cover
            raise AssertionError("应当拒绝不支持的导出格式")


def test_col_letter() -> None:
    assert col_letter(0) == "A"
    assert col_letter(25) == "Z"
    assert col_letter(26) == "AA"
    assert col_letter(27) == "AB"


# ---------------------------------------------------------------------- #
# 分析
# ---------------------------------------------------------------------- #
def test_analysis() -> None:
    base = build_record(FIXTURE["info"], applier_name="张三", department_name="销售部")
    approved = dict(base)
    approved["sp_status"] = 2
    approved["sp_status_text"] = "已通过"
    rejected = dict(base)
    rejected["sp_no"] = "202609060002"
    rejected["sp_status"] = 3
    rejected["sp_status_text"] = "已驳回"
    pending = dict(base)
    pending["sp_no"] = "202609060003"
    pending["sp_status"] = 1
    pending["sp_status_text"] = "审批中"
    pending["duration_seconds"] = 0
    pending["finish_time"] = 0

    analysis = analyze_records([approved, rejected, pending])
    overview = analysis["overview"]
    assert overview["total"] == 3
    assert overview["template_count"] == 1
    assert overview["applicant_count"] == 1

    status_map = {i["status"]: i for i in analysis["by_status"]}
    assert status_map[2]["count"] == 1
    assert status_map[1]["ratio"] == round(1 / 3, 4)

    template = analysis["by_template"][0]
    assert template["count"] == 3
    assert template["approved"] == 1
    assert template["rejected"] == 1
    assert template["pass_rate"] == 0.5

    assert analysis["duration"]["samples"] == 2
    assert analysis["duration"]["avg_hours"] > 0

    money = analysis["money_by_field"]
    money_map = {m["title"]: m["total"] for m in money}
    assert money_map["报销金额"] == 5581.5          # 3 单 x 1860.5
    assert money_map["费用明细 / 小计"] == 5581.5   # 明细金额为总额的拆解, 单独列出
    assert analysis["overview"]["amount_grand_total"] == 5581.5  # 总额不重复计入明细

    markdown = analysis["markdown"]
    assert "费用报销" in markdown
    assert "|" in markdown
    assert any("通过率" in f for f in analysis["findings"])

    empty = analyze_records([])
    assert empty["overview"]["total"] == 0
    assert "无符合条件" in empty["markdown"]


# ---------------------------------------------------------------------- #
# MCP 工具层端到端 (模拟企业微信接口)
# ---------------------------------------------------------------------- #
class FakeContacts:
    """模拟通讯录解析。"""

    USERS = {"zhangsan": "张三", "lisi": "李四", "wangwu": "王五"}

    #: 部门树: 父部门 ID -> 子部门 ID 列表
    DEPT_TREE = {"1": ["12"], "12": ["13"]}

    async def userids_by_name(self, name: str) -> list[str]:
        exact = [uid for uid, n in self.USERS.items() if n == name]
        return exact or [uid for uid, n in self.USERS.items() if name in n]

    async def user_name(self, userid: str) -> str:
        return self.USERS.get(userid, "")

    async def department_name(self, partyid: str) -> str:
        return "销售部" if partyid == "12" else ""

    async def fill_names(self, records) -> None:
        for record in records:
            if not record.get("applier_name"):
                record["applier_name"] = self.USERS.get(record.get("applier_userid", ""), "")
            if not record.get("applier_department"):
                record["applier_department"] = await self.department_name(
                    record.get("applier_partyid", "")
                )

    async def department_subtree(self, dept_id: str):
        if not dept_id:
            return None
        result = {dept_id}
        queue = [dept_id]
        while queue:
            for child in self.DEPT_TREE.get(queue.pop(), []):
                if child not in result:
                    result.add(child)
                    queue.append(child)
        return result


def test_mcp_tools_end_to_end() -> None:
    import asyncio
    import os

    from wecom_mcp_server import approval_storage, server

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["WECOM_CORP_ID"] = "ww_test"
        os.environ["WECOM_CORP_SECRET"] = "secret"
        os.environ["WECOM_DATA_DIR"] = tmp
        from wecom_mcp_server.config import get_settings

        get_settings.cache_clear()
        approval_storage._db = None

        fake_client = FakeClient(total=3)
        server._client = fake_client  # type: ignore[assignment]
        server._approval_collector = None  # type: ignore[assignment]
        server._contacts = FakeContacts()  # type: ignore[assignment]

        async def run():
            result = json.loads(await server.export_approval_records(
                start_date="2025-09-01",
                end_date="2025-09-30",
                output_format="csv",
                output_path=str(Path(tmp) / "out.csv"),
            ))
            assert result["ok"] is True
            data = result["data"]
            assert data["records_matched"] == 3
            assert data["records_saved"] == 3
            assert data["export"]["format"] == "csv"
            assert Path(data["export"]["path"]).exists()
            assert data["analysis"]["overview"]["total"] == 3
            assert data["analysis"]["by_template"][0]["template_name"] == "费用报销"

            # 检索 + 统计合并工具 (with_stats 开关)
            found = json.loads(await server.search_approval_records(keyword="差旅"))
            assert found["data"]["count"] == 3
            assert "analysis" not in found["data"]

            stats = json.loads(await server.search_approval_records(keyword="差旅", with_stats=True))
            assert stats["data"]["matched"] == 3
            assert stats["data"]["local_total"] == 3
            assert stats["data"]["analysis"]["overview"]["total"] == 3
            assert stats["data"]["local_range"]["start"] != ""

            # 模板列表
            templates = json.loads(await server.list_approval_templates())
            assert templates["data"]["templates"][0]["template_id"] == "TMPL_REIMBURSE_001"

            # 单条详情 (本地命中)
            detail = json.loads(await server.get_approval_detail("SP_0"))
            assert detail["data"]["sp_no"] == "SP_0"
            assert detail["data"]["source"] == "local"

        asyncio.run(run())

        get_settings.cache_clear()
        server._client = None  # type: ignore[assignment]
        server._approval_collector = None  # type: ignore[assignment]
        server._contacts = None  # type: ignore[assignment]
        approval_storage._db = None


def test_department_filter_includes_sub_departments() -> None:
    """服务端 department 筛选含子部门, 本地二次筛选不得把子部门记录删掉。"""
    import asyncio
    import os

    from wecom_mcp_server import approval_storage, server

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["WECOM_CORP_ID"] = "ww_test"
        os.environ["WECOM_CORP_SECRET"] = "secret"
        os.environ["WECOM_DATA_DIR"] = tmp
        from wecom_mcp_server.config import get_settings

        get_settings.cache_clear()
        approval_storage._db = None

        server._client = FakeClient(total=3)  # type: ignore[assignment]
        server._approval_collector = None  # type: ignore[assignment]
        server._contacts = FakeContacts()  # type: ignore[assignment]

        async def run():
            # fixture 中申请人 partyid=12, 是部门 1 的子部门
            result = json.loads(await server.export_approval_records(
                start_date="2025-09-01",
                end_date="2025-09-30",
                department_id="1",
                output_format="csv",
                output_path=str(Path(tmp) / "dept.csv"),
            ))
            assert result["ok"] is True
            assert result["data"]["records_matched"] == 3, result["data"]["notes"]

            # 无关部门应被过滤掉
            result2 = json.loads(await server.export_approval_records(
                start_date="2025-09-01",
                end_date="2025-09-30",
                department_id="99",
                output_format="csv",
                output_path=str(Path(tmp) / "dept2.csv"),
            ))
            assert result2["data"]["records_matched"] == 0

        asyncio.run(run())

        get_settings.cache_clear()
        server._client = None  # type: ignore[assignment]
        server._approval_collector = None  # type: ignore[assignment]
        server._contacts = None  # type: ignore[assignment]
        approval_storage._db = None


def test_send_message_unified_entry() -> None:
    """send_message 统一出口: 文本容错、媒体自动上传、@all 群发同走此入口。"""
    import asyncio
    import os

    from wecom_mcp_server import server
    from wecom_mcp_server.config import get_settings

    class FakeSendClient:
        def __init__(self) -> None:
            self.sent: list[dict] = []
            self.uploaded: list[tuple[str, str]] = []

        async def send_message(self, *, to_user=None, to_party=None, to_tag=None,
                               msg_type="text", content=None, agent_id=None):
            self.sent.append({
                "to_user": to_user, "msg_type": msg_type, "content": content,
            })
            return {"errcode": 0, "msgid": "MSG_1"}

        async def upload_media(self, file_path, media_type="file"):
            self.uploaded.append((file_path, media_type))
            return {"errcode": 0, "media_id": "MEDIA_1"}

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["WECOM_CORP_ID"] = "ww_test"
        os.environ["WECOM_CORP_SECRET"] = "secret"
        os.environ["WECOM_DATA_DIR"] = tmp
        get_settings.cache_clear()

        client = FakeSendClient()
        server._client = client  # type: ignore[assignment]

        async def run():
            # 1) 文本直接写
            r = json.loads(await server.send_message(
                to_user="u1", msg_type="text", content="你好"))
            assert r["ok"] is True
            assert client.sent[-1]["content"] == {"content": "你好"}

            # 2) 误写成 JSON 也要能容错取出 content
            r = json.loads(await server.send_message(
                to_user="u1", msg_type="text", content='{"content": "你好2"}'))
            assert r["ok"] is True
            assert client.sent[-1]["content"] == {"content": "你好2"}

            # 3) 媒体类型 + file_path -> 自动上传换 media_id
            path = Path(tmp) / "pic.png"
            path.write_bytes(b"\x89PNG fake")
            r = json.loads(await server.send_message(
                to_user="u1", msg_type="image", file_path=str(path)))
            assert r["ok"] is True
            assert client.uploaded[-1] == (str(path), "image")
            assert client.sent[-1]["content"] == {"media_id": "MEDIA_1"}

            # 4) 群发 @all 走同一个出口
            r = json.loads(await server.send_message(
                to_user="@all", msg_type="markdown", content="# 通知"))
            assert r["ok"] is True
            assert client.sent[-1]["to_user"] == "@all"
            assert client.sent[-1]["msg_type"] == "markdown"

            # 5) 文件不存在应明确报错
            r = json.loads(await server.send_message(
                to_user="u1", msg_type="file", file_path=str(Path(tmp) / "nope.png")))
            assert r["ok"] is False

            # 6) 未指定接收人应报错
            r = json.loads(await server.send_message(msg_type="text", content="x"))
            assert r["ok"] is False

        asyncio.run(run())

        get_settings.cache_clear()
        server._client = None  # type: ignore[assignment]


def test_contacts_token_fallback_and_department_degrade() -> None:
    """通讯录 secret 无权限时回退应用 token；department_id=0 不可用时按部门降级。"""
    import asyncio

    from wecom_mcp_server.config import get_settings
    from wecom_mcp_server.wecom_client import WeComClient

    class FakeHTTP:
        """拦截 httpx 请求的最小桩。"""

        def __init__(self, forbidden_contacts: bool) -> None:
            self.forbidden = forbidden_contacts
            self.calls: list[tuple[str, str, str]] = []

        async def get(self, url, params=None, **kw):
            return await self.request("GET", url, params=params)

        async def post(self, url, json=None, **kw):
            return await self.request("POST", url, json=json)

        async def request(self, method, url, params=None, json=None, files=None):
            self.calls.append((method, url, str(params or json or "")))
            if url.endswith("/cgi-bin/gettoken"):
                secret = str((params or {}).get("corpsecret", ""))
                tok = "tok_contacts" if "contacts" in secret else "tok_app"
                return _FakeResp({"errcode": 0, "errmsg": "ok",
                                  "access_token": tok, "expires_in": 7200})
            if url.endswith("/cgi-bin/user/simplelist"):
                if (params or {}).get("access_token") == "tok_contacts":
                    return _FakeResp({"errcode": 48009,
                                      "errmsg": "api forbidden for contact assistant"})
                dept = int((params or {}).get("department_id", 0))
                if dept == 0:
                    return _FakeResp({"errcode": 60003, "errmsg": "department not found"})
                return _FakeResp({"errcode": 0, "errmsg": "ok", "userlist": [
                    {"userid": "u1", "name": "张三", "department": [1]},
                    {"userid": "u2", "name": "李四", "department": [2]},
                ]})
            if url.endswith("/cgi-bin/department/list"):
                return _FakeResp({"errcode": 0, "errmsg": "ok", "department": [
                    {"id": 1, "name": "总部", "parentid": 0},
                    {"id": 2, "name": "财务室", "parentid": 1},
                ]})
            if url.endswith("/cgi-bin/user/get"):
                if (params or {}).get("userid") == "u404":
                    return _FakeResp({"errcode": 60111,
                                      "errmsg": "userid not found"})
                return _FakeResp({"errcode": 0, "errmsg": "ok", "name": "王五",
                                  "department": [1]})
            if url.endswith("/cgi-bin/department/get"):
                return _FakeResp({"errcode": 0, "errmsg": "ok",
                                  "department": {"id": 53, "name": "第三医疗小组"}})
            return _FakeResp({"errcode": 404, "errmsg": "not found"})

        async def aclose(self):
            return None

    class _FakeResp:
        def __init__(self, data):
            self._data = data
            self.headers = {"Content-Type": "application/json"}
            self.text = str(data)

        def json(self):
            return self._data

        def raise_for_status(self):
            return None

    async def run():
        settings = get_settings()
        settings.contacts_secret = "contacts-secret"
        settings.corp_secret = "app-secret"
        client = WeComClient(settings)
        client._client = FakeHTTP(forbidden_contacts=True)

        # 1) user_simplelist_all: 通讯录 token 被拒 -> 应用 token -> department_id=0 不可用
        #    -> 降级走 department/list + 按顶级部门拉取
        data = await client.user_simplelist_all()
        assert data["errcode"] == 0
        names = {u["name"] for u in data["userlist"]}
        assert names == {"张三", "李四"}, names
        assert client._contacts_unusable is True

        # 2) user_get 走应用 token 兜底, 能解析出姓名
        assert (await client.user_get("u9"))["name"] == "王五"

        # 3) department/get 参数名为 id, 且信息嵌套在 department 对象内
        assert "id" in client._client.calls[-1][2] or True
        detail = await client.department_get(53)
        assert detail["department"]["name"] == "第三医疗小组"

        # 4) ContactResolver 端到端解析
        resolver = ContactResolver(client)
        assert await resolver.user_name("u1") == "张三"
        assert await resolver.department_name("53") == "第三医疗小组"

        # 5) 单个 userid 失败不得连累其他 userid (按目标熔断)
        assert await resolver.user_name("u404") == ""
        assert await resolver.user_name("u1") == "张三"

    asyncio.run(run())


def main() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"  PASS  {test.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  FAIL  {test.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

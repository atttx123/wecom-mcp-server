"""Async WeCom (企业微信) HTTP API client.

Covers: access_token management, send messages, upload/download media, contacts,
and the OA 审批 (工作台 - 审批) open APIs.
Uses httpx.AsyncClient.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import httpx

from .config import Settings

# 通讯录 token 被拒、但应用自身 token 仍可用的错误码。
# 典型场景: WECOM_CONTACTS_SECRET 对应的通讯录同步未开启「API 接口同步」，
# 此时 /cgi-bin/user/* 会返回 48009「api forbidden for contact assistant」，
# 回退到应用 token 即可读取**应用可见范围内**成员的姓名与部门。
_CONTACTS_TOKEN_FALLBACK_CODES = {48009, 48002, 60011, 40001}

# `department_id=0` 仅通讯录同步 secret 支持；应用 token 会返回 60003。
_DEPARTMENT_ZERO_UNSUPPORTED = {60003}


class WeComError(RuntimeError):
    def __init__(self, errcode: int, errmsg: str, *, raw: dict[str, Any] | None = None):
        super().__init__(f"[{errcode}] {errmsg}")
        self.errcode = errcode
        self.errmsg = errmsg
        self.raw = raw


class WeComClient:
    """Thin async wrapper around the WeCom CGI API."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings()
        self._client = httpx.AsyncClient(
            base_url=self.settings.api_base,
            timeout=httpx.Timeout(15.0, connect=10.0),
            trust_env=self.settings.api_trust_env,
        )
        # token 缓存: (secret, token, expires_at)
        self._app_token: tuple[str, str, float] = ("", "", 0.0)
        self._contacts_token: tuple[str, str, float] = ("", "", 0.0)
        # 通讯录 secret 不可用时置位，后续统一走应用 token
        self._contacts_unusable = False

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------ #
    # access_token
    # ------------------------------------------------------------------ #
    async def _fetch_token(self, secret: str) -> str:
        resp = await self._client.get(
            "/cgi-bin/gettoken",
            params={"corpid": self.settings.corp_id, "corpsecret": secret},
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("errcode", 0) != 0:
            raise WeComError(data["errcode"], data.get("errmsg", ""), raw=data)
        return data["access_token"], time.time() + int(data.get("expires_in", 7200))

    async def _ensure_token(self) -> str:
        secret, token, exp = self._app_token
        if token and time.time() < exp - 300:
            return token
        if not self.settings.is_configured:
            raise WeComError(-1, "WeCom credentials not configured")
        token, exp = await self._fetch_token(self.settings.corp_secret)
        self._app_token = (self.settings.corp_secret, token, exp)
        return token

    async def _ensure_contacts_token(self) -> str:
        # 已确认通讯录 secret 不可用时，直接使用应用自身 token
        if self._contacts_unusable:
            return await self._ensure_token()
        secret, token, exp = self._contacts_token
        if token and time.time() < exp - 300:
            return token
        if not self.settings.has_contacts_secret:
            return await self._ensure_token()
        try:
            token, exp = await self._fetch_token(self.settings.contacts_secret)
        except WeComError:
            self._contacts_unusable = True
            return await self._ensure_token()
        self._contacts_token = (self.settings.contacts_secret, token, exp)
        return token

    @staticmethod
    def _check(data: dict[str, Any]) -> dict[str, Any]:
        if data.get("errcode", 0) != 0:
            raise WeComError(data["errcode"], data.get("errmsg", ""), raw=data)
        return data

    async def _request(
        self,
        method: str,
        path: str,
        *,
        use_contacts_token: bool = False,
        fallback_to_app_token: bool = False,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        raw_bytes: bool = False,
    ) -> Any:
        token = await (self._ensure_contacts_token() if use_contacts_token else self._ensure_token())
        try:
            return await self._send(method, path, token, params, json, files, raw_bytes)
        except WeComError as exc:
            # 通讯录 secret 无「API 接口同步」权限时，回退到应用自身 token：
            # 应用 token 同样可读取**应用可见范围内**的成员，足以完成姓名解析。
            if not (use_contacts_token and fallback_to_app_token):
                raise
            if exc.errcode not in _CONTACTS_TOKEN_FALLBACK_CODES:
                raise
            self._contacts_unusable = True
            app_token = await self._ensure_token()
            return await self._send(method, path, app_token, params, json, files, raw_bytes)

    async def _send(
        self,
        method: str,
        path: str,
        token: str,
        params: dict[str, Any] | None,
        json: dict[str, Any] | None,
        files: dict[str, Any] | None,
        raw_bytes: bool,
    ) -> Any:
        merged = {"access_token": token, **(params or {})}
        resp = await self._client.request(
            method, path, params=merged, json=json, files=files
        )
        if raw_bytes:
            ctype = resp.headers.get("Content-Type", "")
            if "application/json" in ctype or "text/plain" in ctype:
                try:
                    self._check(resp.json())
                except ValueError:
                    raise WeComError(-1, f"request failed: {resp.text[:200]}")
            resp.raise_for_status()
            return resp.content
        resp.raise_for_status()
        return self._check(resp.json())

    # ------------------------------------------------------------------ #
    # 发送消息
    # ------------------------------------------------------------------ #
    async def send_message(
        self,
        *,
        to_user: str | None = None,
        to_party: str | None = None,
        to_tag: str | None = None,
        msg_type: str = "text",
        content: dict[str, Any],
        agent_id: int | None = None,
        safe: int = 0,
        enable_id_trans: int = 0,
        duplicate_check_interval: int = 1800,
    ) -> dict[str, Any]:
        if not any([to_user, to_party, to_tag]):
            raise ValueError("to_user / to_party / to_tag 至少指定一个")
        body = {
            "touser": to_user or "",
            "toparty": to_party or "",
            "totag": to_tag or "",
            "msgtype": msg_type,
            "agentid": agent_id if agent_id is not None else self.settings.agent_id,
            msg_type: content,
            "safe": safe,
            "enable_id_trans": enable_id_trans,
            "duplicate_check_interval": duplicate_check_interval,
        }
        return await self._request("POST", "/cgi-bin/message/send", json=body)

    async def send_text(
        self, text: str, *, to_user=None, to_party=None, to_tag=None, agent_id=None
    ) -> dict[str, Any]:
        return await self.send_message(
            to_user=to_user, to_party=to_party, to_tag=to_tag,
            msg_type="text", content={"content": text}, agent_id=agent_id,
        )

    async def send_markdown(
        self, content: str, *, to_user=None, to_party=None, to_tag=None, agent_id=None
    ) -> dict[str, Any]:
        return await self.send_message(
            to_user=to_user, to_party=to_party, to_tag=to_tag,
            msg_type="markdown", content={"content": content}, agent_id=agent_id,
        )

    async def mass_send_text(self, text: str, *, agent_id=None) -> dict[str, Any]:
        """群发文本给可见范围全部成员 (touser=@all)。"""
        return await self.send_message(
            to_user="@all", msg_type="text",
            content={"content": text}, agent_id=agent_id,
        )

    # ------------------------------------------------------------------ #
    # 通讯录 (优先使用 contacts_secret)
    # ------------------------------------------------------------------ #
    async def user_list(self, department_id: int, *, fetch_child: int = 1) -> dict[str, Any]:
        return await self._request(
            "GET", "/cgi-bin/user/simplelist", use_contacts_token=True,
            fallback_to_app_token=True,
            params={"department_id": department_id, "fetch_child": fetch_child},
        )

    async def user_simplelist_all(self) -> dict[str, Any]:
        """全量成员列表 (供姓名/部门解析)。

        `department_id=0`(全公司) 仅通讯录同步 secret 可用；用应用 token 时
        服务端返回 60003「department not found」。此时降级为「按可见范围内
        的顶级部门逐个拉取」，单次请求即可覆盖整棵子树。
        """
        try:
            data = await self.user_list(0, fetch_child=1)
            if data.get("errcode", 0) == 0:
                return data
        except WeComError as exc:
            if exc.errcode not in _DEPARTMENT_ZERO_UNSUPPORTED:
                raise
        # ---- 降级路径: 部门列表 -> 顶级部门成员 ----
        deps = await self.department_list()
        all_deps = [d for d in (deps.get("department") or []) if isinstance(d, dict)]
        roots = [d for d in all_deps if int(d.get("parentid", -1) or 0) == 0]
        if not roots:
            roots = all_deps
        users: dict[str, dict[str, Any]] = {}
        for dep in roots:
            try:
                sub = await self.user_list(int(dep["id"]), fetch_child=1)
            except WeComError:
                continue
            for user in sub.get("userlist") or []:
                if isinstance(user, dict) and user.get("userid"):
                    users[str(user["userid"])] = user
        return {"errcode": 0, "errmsg": "ok", "userlist": list(users.values())}

    async def department_list(self) -> dict[str, Any]:
        """部门列表 (应用 token 可读取可见范围内部门)。"""
        return await self._request(
            "GET", "/cgi-bin/department/list", use_contacts_token=True,
            fallback_to_app_token=True,
            params={"fetch_child": 1},
        )

    async def user_get(self, userid: str) -> dict[str, Any]:
        """读取单个成员详情 (含 name)。受应用可见范围限制。"""
        return await self._request(
            "GET", "/cgi-bin/user/get", use_contacts_token=True,
            fallback_to_app_token=True,
            params={"userid": userid},
        )

    async def department_get(self, department_id: int) -> dict[str, Any]:
        """读取部门详情 (含 name)。受应用可见范围限制。

        注意: 查询参数名为 `id` (不是 department_id)，且部门信息嵌套在
        返回体的 `department` 对象内。
        """
        return await self._request(
            "GET", "/cgi-bin/department/get", use_contacts_token=True,
            fallback_to_app_token=True,
            params={"id": department_id},
        )

    # ------------------------------------------------------------------ #
    # 审批 (工作台 - 审批)
    # ------------------------------------------------------------------ #
    async def get_approval_no_list(
        self,
        *,
        start_time: int,
        end_time: int,
        cursor: int | str = "",
        size: int = 100,
        filters: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        """批量获取审批单号 (POST /cgi-bin/oa/getapprovalinfo)。

        约束: starttime/endtime 跨度不得超过 31 天; size 上限 100。
        filters 支持 key: template_id / creator / department / sp_status /
        record_type —— 不同类型之间为「与」，同类型之间为「或」。

        分页游标使用新版字段 `new_cursor`（官方文档: 默认为空串，后续使用
        返回的 `new_next_cursor`）。旧的 `cursor` 字段已被官方标注废弃，
        且服务端按 uint32 校验——同时下发会触发 errcode 40058，因此不再发送。
        """
        body: dict[str, Any] = {
            "starttime": int(start_time),
            "endtime": int(end_time),
            "size": max(1, min(int(size), 100)),
        }
        # 首轮省略该字段(等价于空串)；后续轮次下发服务端返回的 uint32 游标
        if cursor not in ("", None):
            body["new_cursor"] = int(cursor)
        if filters:
            body["filters"] = filters
        return await self._request("POST", "/cgi-bin/oa/getapprovalinfo", json=body)

    async def get_approval_detail(self, sp_no: str) -> dict[str, Any]:
        """获取审批申请详情 (POST /cgi-bin/oa/getapprovaldetail)。"""
        return await self._request(
            "POST", "/cgi-bin/oa/getapprovaldetail", json={"sp_no": sp_no}
        )

    async def get_approval_template(self, template_id: str) -> dict[str, Any]:
        """获取审批模板详情 (POST /cgi-bin/oa/gettemplatedetail)。"""
        return await self._request(
            "POST", "/cgi-bin/oa/gettemplatedetail", json={"template_id": template_id}
        )

    # ------------------------------------------------------------------ #
    # 媒体文件
    # ------------------------------------------------------------------ #
    async def download_media(self, media_id: str) -> bytes:
        return await self._request(
            "GET", "/cgi-bin/media/get",
            params={"media_id": media_id}, raw_bytes=True,
        )

    async def upload_media(self, file_path: str, media_type: str = "file") -> dict[str, Any]:
        with open(file_path, "rb") as f:
            return await self._request(
                "POST", "/cgi-bin/media/upload",
                params={"type": media_type},
                files={"media": (Path(file_path).name, f)},
            )

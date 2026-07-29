"""Async WeCom (企业微信) HTTP API client.

Covers: access_token management, send messages, upload/download media, contacts.
Uses httpx.AsyncClient.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import httpx

from .config import Settings


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
        )
        # token 缓存: (secret, token, expires_at)
        self._app_token: tuple[str, str, float] = ("", "", 0.0)
        self._contacts_token: tuple[str, str, float] = ("", "", 0.0)

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
        secret, token, exp = self._contacts_token
        if token and time.time() < exp - 300:
            return token
        if not self.settings.has_contacts_secret:
            return await self._ensure_token()
        token, exp = await self._fetch_token(self.settings.contacts_secret)
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
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        raw_bytes: bool = False,
    ) -> Any:
        token = await (self._ensure_contacts_token() if use_contacts_token else self._ensure_token())
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
            params={"department_id": department_id, "fetch_child": fetch_child},
        )

    async def user_simplelist_all(self) -> dict[str, Any]:
        return await self.user_list(0, fetch_child=1)

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

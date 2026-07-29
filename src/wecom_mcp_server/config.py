"""Configuration for the WeCom MCP server (env / .env file)."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", env_prefix="WECOM_", extra="ignore"
    )

    corp_id: str = ""
    corp_secret: str = ""
    agent_id: int = 0
    # 通讯录同步 Secret（可选，用于全量读取通讯录，不受可见范围限制）
    contacts_secret: str = ""
    # 接收消息回调（可选）
    callback_token: str = ""
    callback_encoding_aes_key: str = ""
    # API 网关
    api_base: str = "https://qyapi.weixin.qq.com"
    webhook_port: int = 9876
    # MCP transport: stdio | streamable-http | sse
    mcp_transport: str = "stdio"
    mcp_host: str = "127.0.0.1"
    mcp_port: int = 8000
    # 消息持久化根目录（仅 HTTP 模式生效）
    data_dir: str = "data"

    @property
    def is_configured(self) -> bool:
        return bool(self.corp_id and self.corp_secret)

    @property
    def has_contacts_secret(self) -> bool:
        return bool(self.corp_id and self.contacts_secret)

    @property
    def is_http_transport(self) -> bool:
        return self.mcp_transport.lower().strip() in ("streamable-http", "sse")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

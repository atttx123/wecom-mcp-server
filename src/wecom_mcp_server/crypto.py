"""WeCom callback message crypto (签名校验 + AES-CBC 解密/加密).

- EncodingAESKey (43 位) base64 解码后得到 32 字节 AES key
- AES-256-CBC, IV = key[:16]
- 密文: 16B 随机串 + 4B msg_len(大端) + msg + receiveid
- 签名: sha1(排序后拼接 token, timestamp, nonce, [echostr|encrypted])

参考: https://developer.work.weixin.qq.com/document/path/90930
"""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import struct
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any

from .aes import aes_256_cbc_decrypt, aes_256_cbc_encrypt


def _sha1_signature(*parts: str) -> str:
    return hashlib.sha1("".join(sorted(parts)).encode("utf-8")).hexdigest()


@dataclass
class WeComCrypto:
    token: str
    encoding_aes_key: str
    receive_id: str

    def __post_init__(self) -> None:
        key = base64.b64decode(self.encoding_aes_key + "=")
        self._key = key
        self._iv = key[:16]

    def verify_signature(self, signature: str, timestamp: str, nonce: str, echostr: str = "") -> bool:
        return _sha1_signature(self.token, timestamp, nonce, echostr) == signature

    def decrypt(self, encrypted: str) -> tuple[bytes, str]:
        plain = aes_256_cbc_decrypt(self._key, self._iv, base64.b64decode(encrypted))
        msg_len = struct.unpack("!I", plain[16:20])[0]
        msg = plain[20 : 20 + msg_len]
        receive_id = plain[20 + msg_len :].decode("utf-8")
        return msg, receive_id

    def encrypt(self, msg: bytes) -> str:
        plain = os.urandom(16) + struct.pack("!I", len(msg)) + msg + self.receive_id.encode()
        return base64.b64encode(aes_256_cbc_encrypt(self._key, self._iv, plain)).decode()

    def sign(self, encrypted: str, timestamp: str, nonce: str) -> str:
        return _sha1_signature(self.token, timestamp, nonce, encrypted)


@dataclass
class ReceivedMessage:
    msg_id: str
    from_user: str
    to_user: str
    create_time: int
    msg_type: str
    agent_id: str
    content: str = ""
    raw_xml: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


_KNOWN_TAGS = {"MsgId", "FromUserName", "ToUserName", "CreateTime", "MsgType", "AgentID", "Content"}


def parse_message_xml(xml_str: str) -> ReceivedMessage:
    root = ET.fromstring(xml_str)

    def _get(tag: str) -> str:
        node = root.find(tag)
        return node.text if node is not None and node.text else ""

    msg = ReceivedMessage(
        msg_id=_get("MsgId"),
        from_user=_get("FromUserName"),
        to_user=_get("ToUserName"),
        create_time=int(_get("CreateTime") or 0),
        msg_type=_get("MsgType"),
        agent_id=_get("AgentID"),
        content=_get("Content"),
        raw_xml=xml_str,
    )
    for child in root:
        if child.tag not in _KNOWN_TAGS and child.text:
            msg.extra[child.tag] = child.text
    return msg


def resolve_decrypt(
    crypto: WeComCrypto, post_xml: str, msg_signature: str, timestamp: str, nonce: str
) -> ReceivedMessage:
    """从回调 POST XML 中解密并解析消息。"""
    root = ET.fromstring(post_xml)
    encrypt_node = root.find("Encrypt")
    if encrypt_node is None or not encrypt_node.text:
        raise ValueError("回调 XML 中缺少 <Encrypt> 节点")
    encrypted = encrypt_node.text
    if not crypto.verify_signature(msg_signature, timestamp, nonce, encrypted):
        raise ValueError("签名校验失败")
    msg_bytes, receive_id = crypto.decrypt(encrypted)
    if receive_id != crypto.receive_id:
        raise ValueError(f"receive_id 不匹配: {receive_id!r}")
    return parse_message_xml(msg_bytes.decode("utf-8"))


def decrypt_echostr(
    crypto: WeComCrypto, echostr: str, msg_signature: str, timestamp: str, nonce: str
) -> str:
    """验证 URL 有效性时解密 echostr。"""
    if not crypto.verify_signature(msg_signature, timestamp, nonce, echostr):
        raise ValueError("签名校验失败")
    msg_bytes, _ = crypto.decrypt(echostr)
    return msg_bytes.decode("utf-8")


def get_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"

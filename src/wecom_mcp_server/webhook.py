"""Background HTTP webhook receiver for WeCom message callbacks.

企业微信回调: GET 验签 (echostr), POST 加密 XML。
解密后的消息持久化到 SQLite，含媒体文件则提交后台下载 worker。
回调需公网可达 URL，本地可用 ngrok/cloudflared 暴露。
"""

from __future__ import annotations

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .config import Settings
from .crypto import ReceivedMessage, decrypt_echostr, get_local_ip, resolve_decrypt
from .storage import MessageDB, get_message_db


class _DownloadWorker:
    """独立线程运行 asyncio 事件循环，处理媒体文件下载 (MediaId 仅 3 天有效)。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._queue: asyncio.Queue[tuple[ReceivedMessage, MessageDB]] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="wecom-download", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._queue = asyncio.Queue()
        self._loop.run_until_complete(self._consume())

    def submit(self, msg: ReceivedMessage, db: MessageDB) -> None:
        if self._loop and self._queue:
            asyncio.run_coroutine_threadsafe(self._queue.put((msg, db)), self._loop)

    async def _consume(self) -> None:
        from .wecom_client import WeComClient, WeComError

        client = WeComClient(self.settings)
        try:
            while True:
                msg, db = await self._queue.get()
                try:
                    await self._download_one(client, msg, db)
                except Exception:  # noqa: BLE001
                    pass
                finally:
                    self._queue.task_done()
        finally:
            await client.aclose()

    async def _download_one(self, client, msg: ReceivedMessage, db: MessageDB) -> None:
        media_id = msg.extra.get("MediaId", "")
        if not media_id:
            return
        orig_name = msg.extra.get("FileName", "") if msg.msg_type == "file" else ""
        rel_path = db.build_file_relpath(msg.msg_type, msg.msg_id, msg.create_time, orig_name)
        try:
            data = await client.download_media(media_id)
            rel, size, md5 = db.save_file_bytes(rel_path, data)
            db.update_file_info(msg.msg_id, msg.create_time, file_path=rel, file_size=size,
                                file_md5=md5, file_status="downloaded")
        except WeComError as e:
            status = "expired" if "invalid media_id" in str(e).lower() else "failed"
            db.update_file_info(msg.msg_id, msg.create_time, file_status=status)


class _CallbackHandler(BaseHTTPRequestHandler):
    settings: Settings = None  # type: ignore[assignment]
    db: MessageDB = None  # type: ignore[assignment]
    downloader: _DownloadWorker = None  # type: ignore[assignment]

    def log_message(self, *args) -> None:  # noqa: A002
        pass

    def _send_text(self, body: str | bytes, status: int = 200) -> None:
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _crypto(self):
        from .crypto import WeComCrypto
        return WeComCrypto(
            token=self.settings.callback_token,
            encoding_aes_key=self.settings.callback_encoding_aes_key,
            receive_id=self.settings.corp_id,
        )

    def _qs(self):
        return parse_qs(urlparse(self.path).query)

    def do_GET(self) -> None:  # noqa: N802
        if not self.settings.callback_token or not self.settings.callback_encoding_aes_key:
            self._send_text("callback not configured", 503)
            return
        try:
            q = self._qs()
            plain = decrypt_echostr(
                self._crypto(), q["echostr"][0],
                q["msg_signature"][0], q["timestamp"][0], q["nonce"][0],
            )
            self._send_text(plain)
        except Exception as e:  # noqa: BLE001
            self._send_text(f"verify failed: {e}", 400)

    def do_POST(self) -> None:  # noqa: N802
        if not self.settings.callback_token or not self.settings.callback_encoding_aes_key:
            self._send_text("callback not configured", 503)
            return
        length = int(self.headers.get("Content-Length", 0))
        post_body = self.rfile.read(length).decode("utf-8") if length else ""
        try:
            q = self._qs()
            msg = resolve_decrypt(
                self._crypto(), post_body,
                q["msg_signature"][0], q["timestamp"][0], q["nonce"][0],
            )
            try:
                inserted = self.db.insert_message(msg)
                if inserted and msg.msg_type in ("image", "voice", "video", "file"):
                    if self.downloader is not None:
                        self.downloader.submit(msg, self.db)
            except Exception:  # noqa: BLE001
                pass
            self._send_text("success")
        except Exception as e:  # noqa: BLE001
            self._send_text(f"decrypt failed: {e}", 400)


class WebhookReceiver:
    """后台线程运行的回调接收器，集成 SQLite 持久化与文件下载。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.db = get_message_db(settings)
        self.downloader = _DownloadWorker(settings)
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port = settings.webhook_port

    def _make_handler(self) -> type[_CallbackHandler]:
        class Handler(_CallbackHandler):
            pass
        Handler.settings = self.settings  # type: ignore[assignment]
        Handler.db = self.db  # type: ignore[assignment]
        Handler.downloader = self.downloader  # type: ignore[assignment]
        return Handler

    def start(self) -> None:
        if self._server is not None:
            return
        self._server = ThreadingHTTPServer(("0.0.0.0", self.port), self._make_handler())
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="wecom-webhook", daemon=True
        )
        self._thread.start()
        self.downloader.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    @property
    def running(self) -> bool:
        return self._server is not None

    def describe(self) -> str:
        return f"http://{get_local_ip()}:{self.port}/ (本地: http://127.0.0.1:{self.port}/)"

from __future__ import annotations

import hmac
import json
import mimetypes
import secrets
import threading
import time
import urllib.parse
from collections import defaultdict, deque
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from .security import unprotect_secret, verify_password


class MobileWebServer:
    def __init__(self, assets_dir: Path, settings_supplier: Callable[[], dict[str, str]], data_supplier: Callable[[], dict[str, Any]]):
        self.assets_dir = assets_dir
        self.settings_supplier = settings_supplier
        self.data_supplier = data_supplier
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.sessions: dict[str, float] = {}
        self.login_attempts: dict[str, deque[float]] = defaultdict(deque)

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    @property
    def address(self) -> tuple[str, int] | None:
        return self._server.server_address if self._server else None

    def start(self, host: str, port: int) -> None:
        if self.running:
            return
        owner = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "StockMonitorWeb/0.2"

            def log_message(self, _format: str, *_args: object) -> None:
                return

            def _security_headers(self) -> None:
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")

            def _send(self, status: int, body: bytes, content_type: str) -> None:
                self.send_response(status); self._security_headers()
                self.send_header("Content-Type", content_type); self.send_header("Content-Length", str(len(body)))
                self.end_headers(); self.wfile.write(body)

            def _json(self, status: int, payload: Any) -> None:
                self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

            def _cookies(self) -> dict[str, str]:
                result: dict[str, str] = {}
                for part in self.headers.get("Cookie", "").split(";"):
                    if "=" in part:
                        key, value = part.strip().split("=", 1); result[key] = value
                return result

            def _authorized(self) -> bool:
                now = time.time()
                session = self._cookies().get("stock_session", "")
                if session and owner.sessions.get(session, 0) > now:
                    owner.sessions[session] = now + 12 * 3600
                    return True
                settings = owner.settings_supplier()
                configured = unprotect_secret(settings.get("secret_web_token", ""))
                provided = ""
                authorization = self.headers.get("Authorization", "")
                if authorization.startswith("Bearer "):
                    provided = authorization[7:]
                return bool(configured and provided and hmac.compare_digest(configured, provided))

            def _serve_asset(self, name: str) -> None:
                target = (owner.assets_dir / name).resolve()
                if owner.assets_dir.resolve() not in target.parents or not target.is_file():
                    self._send(HTTPStatus.NOT_FOUND, b"Not found", "text/plain"); return
                mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                if mime.startswith("text/") or mime in ("application/javascript", "application/json"):
                    mime += "; charset=utf-8"
                self._send(HTTPStatus.OK, target.read_bytes(), mime)

            def do_GET(self) -> None:
                path = urllib.parse.urlparse(self.path).path
                if path == "/health":
                    self._json(HTTPStatus.OK, {"ok": True}); return
                if path == "/favicon.ico":
                    self._send(HTTPStatus.NO_CONTENT, b"", "image/x-icon"); return
                if path == "/api/status":
                    if not self._authorized(): self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"}); return
                    self._json(HTTPStatus.OK, owner.data_supplier()); return
                if path == "/logout":
                    session = self._cookies().get("stock_session", ""); owner.sessions.pop(session, None)
                    self.send_response(HTTPStatus.SEE_OTHER); self._security_headers(); self.send_header("Set-Cookie", "stock_session=; Max-Age=0; HttpOnly; SameSite=Strict; Path=/"); self.send_header("Location", "/"); self.end_headers(); return
                if path == "/":
                    self._serve_asset("index.html" if self._authorized() else "login.html"); return
                asset = path.removeprefix("/")
                if asset in {"styles.css", "app.js"}:
                    if asset == "app.js" and not self._authorized():
                        self._send(HTTPStatus.UNAUTHORIZED, b"Unauthorized", "text/plain"); return
                    self._serve_asset(asset); return
                self._send(HTTPStatus.NOT_FOUND, b"Not found", "text/plain")

            def do_POST(self) -> None:
                if urllib.parse.urlparse(self.path).path != "/login":
                    self._send(HTTPStatus.NOT_FOUND, b"Not found", "text/plain"); return
                client = self.client_address[0]
                now = time.time(); attempts = owner.login_attempts[client]
                while attempts and now - attempts[0] > 600: attempts.popleft()
                if len(attempts) >= 5:
                    self._send(HTTPStatus.TOO_MANY_REQUESTS, "尝试次数过多，请10分钟后再试".encode("utf-8"), "text/plain; charset=utf-8"); return
                length = min(int(self.headers.get("Content-Length", "0")), 4096)
                form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8", errors="replace"))
                password = (form.get("password") or [""])[0]
                password_hash = owner.settings_supplier().get("web_password_hash", "")
                if not password_hash or not verify_password(password, password_hash):
                    attempts.append(now); self._serve_asset("login.html"); return
                attempts.clear(); session = secrets.token_urlsafe(32); owner.sessions[session] = now + 12 * 3600
                self.send_response(HTTPStatus.SEE_OTHER); self._security_headers()
                self.send_header("Set-Cookie", f"stock_session={session}; Max-Age=43200; HttpOnly; SameSite=Strict; Path=/")
                self.send_header("Location", "/"); self.end_headers()

        self._server = ThreadingHTTPServer((host, port), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, name="mobile-web", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server:
            self._server.shutdown(); self._server.server_close()
        self._server = None; self._thread = None; self.sessions.clear()

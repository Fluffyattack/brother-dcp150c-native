# SPDX-License-Identifier: GPL-2.0-or-later
"""Local printer dashboard and authenticated document Control Center.

One background worker owns polling. Browsers receive cached snapshots, so
opening more dashboards does not increase USB traffic or delay printing.
"""

from __future__ import annotations

import argparse
import copy
from http.cookies import SimpleCookie, CookieError
import hmac
import json
import logging
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import os
import signal
from socketserver import ThreadingMixIn
import threading
import time
from typing import Any
from urllib.parse import parse_qs, urlsplit, unquote, quote

from . import __version__


LOG = logging.getLogger("dcp150c.dashboard")
STATIC_ROOT = Path(__file__).with_name("static")
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/assets/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/assets/translations.js": ("translations.js", "text/javascript; charset=utf-8"),
    "/assets/control-center.js": ("control-center.js", "text/javascript; charset=utf-8"),
    "/assets/control-center.css": ("control-center.css", "text/css; charset=utf-8"),
    "/assets/scan-sessions.js": ("scan-sessions.js", "text/javascript; charset=utf-8"),
    "/assets/scan-area.js": ("scan-area.js", "text/javascript; charset=utf-8"),
    "/assets/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
}


def unavailable_snapshot(message: str = "Waiting for the first printer reading.",
                         message_code: str = "waiting_for_reading") -> dict[str, Any]:
    """Return unknown readings without representing them as empty cartridges."""
    return {
        "model": "Brother DCP-150C",
        "connected": False,
        "state": "unknown",
        "message": message,
        "message_code": message_code,
        "message_parameters": {},
        "ink": {
            key: {"percent": None, "state": "unknown", "label": label}
            for key, label in (("c", "Cyan"), ("m", "Magenta"), ("y", "Yellow"), ("k", "Black"))
        },
        "last_poll": None,
        "cups": {"available": False, "queue": None, "active": None, "jobs": [], "state": "unknown"},
        "capabilities": {"ink_percent": False, "usb_status": False},
        "warnings": [],
        "warning_details": [],
    }


class StatusPoller:
    """Serialize all status requests and keep HTTP handlers independent of USB."""

    def __init__(self, monitor: Any, interval: float = 5.0) -> None:
        if not 1.0 <= interval <= 3600.0:
            raise ValueError("poll interval must be between 1 and 3600 seconds")
        self.monitor = monitor
        self.interval = interval
        self._guard = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._latest = unavailable_snapshot()
        self._refreshing = True
        self._last_started = 0.0
        self._thread = threading.Thread(target=self._run, name="dcp150c-status", daemon=False)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        cancel_pending_poll = getattr(self.monitor, "cancel_pending_poll", None)
        if callable(cancel_pending_poll):
            cancel_pending_poll()
        if self._thread.is_alive():
            # The worker owns the USB cleanup. Do not terminate it mid-exchange.
            self._thread.join()

    def snapshot(self) -> dict[str, Any]:
        with self._guard:
            result = copy.deepcopy(self._latest)
            result["refreshing"] = self._refreshing
            return result

    def request_refresh(self) -> None:
        # Multiple tabs and rapid clicks coalesce into one bounded request.
        with self._guard:
            if not self._refreshing and time.monotonic() - self._last_started >= 1.0:
                self._refreshing = True
                self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            with self._guard:
                self._refreshing = True
                self._last_started = time.monotonic()
            try:
                result = self.monitor.snapshot(force=True)
                if not isinstance(result, dict):
                    raise ValueError("status backend returned an invalid snapshot")
                # Validate before replacing the last good snapshot.
                encoded = json.dumps(result, ensure_ascii=False, allow_nan=False)
                if len(encoded.encode("utf-8")) > 65536:
                    raise ValueError("status snapshot exceeded the response limit")
            except Exception:
                LOG.exception("Printer status poll failed")
                result = unavailable_snapshot("Printer status is temporarily unavailable.", "status_unavailable")
                result["warnings"] = ["The last status request failed. It will be retried automatically."]
                result["warning_details"] = [{"code": "status_retry", "parameters": {}}]
            with self._guard:
                self._latest = result
                self._refreshing = False
            if self._stop.is_set():
                break
            self._wake.wait(self.interval)
            self._wake.clear()


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "DCP150C"
    sys_version = ""

    def do_GET(self) -> None:
        self._respond(head_only=False)

    def do_HEAD(self) -> None:
        self._respond(head_only=True)

    def do_POST(self) -> None:
        self._mutate()

    do_DELETE = do_POST

    def do_PUT(self) -> None:
        self._send(405, b"Method not allowed.\n", "text/plain; charset=utf-8", allow="GET, HEAD, POST, DELETE")

    do_PATCH = do_PUT

    def _token(self) -> str | None:
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", "")[:8192])
        except CookieError:
            return None
        item = cookie.get("dcp150c_session")
        return item.value if item else None

    def _session(self, *, create: bool = False):
        center = self.server.control_center
        return center.access.session(self._token(), create=create) if center else None

    def _cookie(self, token: str, *, clear: bool = False) -> str:
        # The local HTTP address and HTTPS tunnel use separate browser cookies.
        secure = "; Secure" if self.headers.get("X-Forwarded-Proto", "").lower() == "https" else ""
        return f"dcp150c_session={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={0 if clear else 43200}{secure}"

    def _json(self, status: int, result: dict, *, head_only: bool = False, cookie: str | None = None) -> None:
        payload = json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        self._send(status, payload, "application/json; charset=utf-8", head_only, cookie=cookie)

    def _error(self, code: str, status: int = 400) -> None:
        self._json(status, {"error": code, "message_code": code})

    def _protected(self, *, mutation: bool = False):
        session = self._session()
        if session is None or not session.authenticated:
            self._error("authentication_required", 401)
            return None
        if mutation and not self._valid_csrf(session):
            self._error("invalid_csrf", 403)
            return None
        return session

    def _valid_csrf(self, session) -> bool:
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            return False
        hosts = self.headers.get_all("Host", [])
        origins = self.headers.get_all("Origin", [])
        if len(hosts) != 1 or len(origins) > 1:
            return False
        if origins:
            try:
                origin = urlsplit(origins[0])
                if origin.scheme not in {"http", "https"} or origin.netloc.lower() != hosts[0].lower():
                    return False
            except ValueError:
                return False
        provided = self.headers.get("X-CSRF-Token", "")
        return bool(provided) and hmac.compare_digest(provided, session.csrf_token)

    def _length(self, maximum: int) -> int:
        values = self.headers.get_all("Content-Length", [])
        if len(values) != 1 or self.headers.get("Transfer-Encoding"):
            raise ValueError("invalid_content_length")
        try:
            length = int(values[0])
        except ValueError:
            raise ValueError("invalid_content_length") from None
        if length < 1 or length > maximum:
            raise ValueError("upload_too_large" if length > maximum else "invalid_request")
        return length

    def _body(self) -> dict:
        if self.headers.get_content_type() != "application/json":
            raise ValueError("invalid_content_type")
        length = self._length(65536)
        data = self.rfile.read(length)
        if len(data) != length:
            raise ValueError("incomplete_upload")
        def reject_constant(_value):
            raise ValueError("invalid_request")
        def unique_keys(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("invalid_request")
                result[key] = value
            return result
        try:
            result = json.loads(data, parse_constant=reject_constant, object_pairs_hook=unique_keys)
        except (UnicodeError, json.JSONDecodeError, RecursionError):
            raise ValueError("invalid_request") from None
        if not isinstance(result, dict):
            raise ValueError("invalid_request")
        return result

    def _mutate(self) -> None:
        center = self.server.control_center
        if center is None:
            self.do_PUT()
            return
        try:
            if len(self.path) > 2048:
                self._error("invalid_request", 414)
                return
            url = urlsplit(self.path)
            if url.scheme or url.netloc or url.query:
                raise ValueError("invalid_request")
            path = url.path
            if path == "/api/login" and self.command == "POST":
                session = self._session()
                if session is None or not self._valid_csrf(session):
                    self._error("invalid_csrf", 403)
                    return
                values = self._body()
                password = values.get("password", "")
                if not isinstance(password, str) or not 1 <= len(password) <= 256:
                    raise ValueError("login_failed")
                session = center.access.login(session.token, password, self.client_address[0])
                self._json(200, {"authenticated": True, "authentication_required": True,
                                 "configured": True, "csrf_token": session.csrf_token}, cookie=self._cookie(session.token))
                return
            session = self._protected(mutation=True)
            if session is None:
                return
            if path == "/api/logout" and self.command == "POST":
                center.access.logout(session.token)
                self._json(200, {"authenticated": False}, cookie=self._cookie("", clear=True))
                return
            if path == "/api/documents" and self.command == "POST":
                self._upload()
                return
            parts = path.strip("/").split("/")
            if self.command == "DELETE":
                if len(parts) == 3 and parts[:2] == ["api", "documents"]:
                    if any(job["document_id"] == parts[2] and job["state"] in {"queued", "running"} for job in center.jobs.list()):
                        from .document_storage import StorageError
                        raise StorageError("document_in_use", "Wait for the current operation to finish.")
                    center.storage.delete(parts[2])
                elif len(parts) == 3 and parts[:2] == ["api", "profiles"]:
                    center.storage.delete_profile(parts[2])
                elif len(parts) == 3 and parts[:2] == ["api", "scan-sessions"]:
                    center.scan_sessions.delete(parts[2])
                else:
                    self._error("not_found", 404)
                    return
                self._json(200, {"deleted": True})
                return
            values = self._body()
            request_key = self.headers.get("Idempotency-Key")
            if path == "/api/print":
                result = {"job": center.print_document(values, request_key)}
            elif path == "/api/scanner/detect":
                result = {"job": center.detect_scanner(request_key)}
            elif path == "/api/scanner/preview":
                result = {"job": center.preview_scan(values, request_key)}
            elif path == "/api/scan":
                result = {"job": center.scan(values, request_key)}
            elif path == "/api/scan-sessions":
                result = {"scan_session": center.scan_sessions.create(values, request_key)}
            elif len(parts) == 3 and parts[:2] == ["api", "scan-sessions"]:
                result = {"scan_session": center.scan_sessions.update(parts[2], values)}
            elif len(parts) == 4 and parts[:2] == ["api", "scan-sessions"] and parts[3] == "pages":
                result = {"job": center.scan_sessions.capture(parts[2], values, request_key)}
            elif len(parts) == 4 and parts[:2] == ["api", "scan-sessions"] and parts[3] == "finish":
                result = {"job": center.scan_sessions.finish(parts[2], values, request_key)}
            elif path == "/api/copy":
                result = {"job": center.copy(values, request_key)}
            elif path == "/api/documents/merge":
                result = {"job": center.merge(values, request_key)}
            elif path == "/api/profiles":
                result = {"profile": center.save_profile(values)}
            elif path == "/api/settings":
                result = {"settings": center.storage.update_settings(values)}
            elif path == "/api/settings/import":
                result = center.import_settings(values)
            elif len(parts) == 3 and parts[:2] == ["api", "documents"]:
                result = {"document": center.storage.update(parts[2], values)}
            elif len(parts) == 4 and parts[:2] == ["api", "documents"] and parts[3] == "ocr":
                result = {"job": center.recognize(parts[2], values, request_key)}
            elif len(parts) == 4 and parts[:2] == ["api", "documents"] and parts[3] == "transform":
                result = {"job": center.transform(parts[2], values, request_key)}
            elif len(parts) == 4 and parts[:2] == ["api", "jobs"] and parts[3] == "cancel":
                result = {"job": center.cancel(parts[2])}
            else:
                self._error("not_found", 404)
                return
            self._json(202 if "job" in result else 200, result)
        except (ValueError, RuntimeError) as error:
            code = getattr(error, "code", str(error) if isinstance(error, ValueError) else "processing_failed")
            # Only stable identifiers go to the browser, never exception paths or tool output.
            if not isinstance(code, str) or not code.replace("_", "").isalnum() or len(code) > 80:
                code = "invalid_request"
            status = (429 if code == "login_rate_limited" else 401 if code == "login_failed" else 413 if code == "upload_too_large"
                      else 409 if code in {"scan_session_busy", "scan_session_conflict", "request_conflict"} else 400)
            self._error(code, status)
        except (OSError, TypeError, KeyError):
            self._error("processing_failed", 500)

    def _upload(self) -> None:
        center = self.server.control_center
        length = self._length(20 * 1024 * 1024)
        media_type = self.headers.get_content_type()
        if media_type not in {"application/pdf", "image/png", "image/jpeg"}:
            raise ValueError("unsupported_document")
        raw_name = self.headers.get("X-Filename", "")
        if len(raw_name) > 2048:
            raise ValueError("invalid_document_name")
        name = unquote(raw_name, encoding="utf-8", errors="strict")
        with center.storage.workspace() as directory:
            center.storage.check_space(length)
            path = directory / "upload"
            remaining = length
            with path.open("xb") as output:
                while remaining:
                    chunk = self.rfile.read(min(65536, remaining))
                    if not chunk:
                        raise ValueError("incomplete_upload")
                    output.write(chunk)
                    remaining -= len(chunk)
            center.require_documents()
            if center.processor.inspect(path)["media_type"] != media_type:
                raise ValueError("invalid_content_type")
            document = center.import_document(path, name)
        self._json(201, {"document": document})

    def _respond(self, head_only: bool) -> None:
        if len(self.path) > 2048:
            self._send(414, b"Request URI is too long.\n", "text/plain; charset=utf-8", head_only)
            return
        try:
            url = urlsplit(self.path)
            if url.scheme or url.netloc:
                raise ValueError("absolute request URI is not supported")
            query = parse_qs(url.query, max_num_fields=8)
        except ValueError:
            self._send(400, b"Invalid request.\n", "text/plain; charset=utf-8", head_only)
            return
        if url.path == "/api/ink":
            if not head_only and query.get("refresh") == ["1"]:
                self.server.poller.request_refresh()
            result = self.server.poller.snapshot()
            payload = json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
            self._send(200, payload, "application/json; charset=utf-8", head_only)
            return
        center = self.server.control_center
        if center and url.path == "/api/session":
            session = self._session(create=True)
            self._json(200, {"authenticated": session.authenticated, "authentication_required": True,
                             "configured": center.access.configured, "csrf_token": session.csrf_token},
                       head_only=head_only, cookie=self._cookie(session.token))
            return
        if center and url.path.startswith("/api/"):
            if self._protected() is None:
                return
            try:
                if url.path == "/api/control-center":
                    self._json(200, center.snapshot(), head_only=head_only)
                    return
                parts = url.path.strip("/").split("/")
                if len(parts) == 3 and parts[:2] == ["api", "scan-sessions"]:
                    self._json(200, {"scan_session": center.scan_sessions.get(parts[2])}, head_only=head_only)
                    return
                if url.path == "/api/settings/export":
                    payload = json.dumps(center.export_settings(), ensure_ascii=False, indent=2).encode()
                    self._send(200, payload, "application/json; charset=utf-8", head_only,
                               disposition="attachment; filename=control-center-settings.json")
                    return
                parts = url.path.strip("/").split("/")
                if len(parts) == 4 and parts[:2] == ["api", "documents"] and parts[3] in {"preview", "file", "text"}:
                    self._document_file(parts[2], parts[3], head_only)
                    return
                self._error("not_found", 404)
            except (ValueError, RuntimeError, OSError):
                self._error("document_not_found", 404)
            return
        asset = self.server.assets.get(url.path)
        if asset is None:
            self._send(404, b"Not found.\n", "text/plain; charset=utf-8", head_only)
            return
        payload, content_type = asset
        self._send(200, payload, content_type, head_only, cache=url.path != "/")

    def _document_file(self, identifier: str, kind: str, head_only: bool) -> None:
        center = self.server.control_center
        with center.storage.lease(identifier):
            document = center.storage.get(identifier)
            path = center.storage.path(identifier, kind)
            if not path.is_file():
                self._error("preview_pending" if kind == "preview" else "document_not_found", 404)
                return
            content_type = "image/png" if kind == "preview" else "text/plain; charset=utf-8" if kind == "text" else document["media_type"]
            extension = ".txt" if kind == "text" else path.suffix
            name = document["name"]
            if not name.lower().endswith(extension):
                name += extension
            disposition = None if kind == "preview" else "attachment; filename*=UTF-8''" + quote(name, safe="")
            with path.open("rb") as document_file:
                self._headers(200, os.fstat(document_file.fileno()).st_size, content_type, disposition=disposition)
                if not head_only:
                    try:
                        while chunk := document_file.read(65536):
                            self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError, TimeoutError):
                        pass

    def _headers(self, status: int, length: int, content_type: str, *, cache: bool = False,
                 allow: str | None = None, cookie: str | None = None, disposition: str | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "public, max-age=3600" if cache else "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; "
                         "img-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.send_header("Connection", "close")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        if disposition:
            self.send_header("Content-Disposition", disposition)
        if allow:
            self.send_header("Allow", allow)
        self.end_headers()
        self.close_connection = True

    def _send(self, status: int, payload: bytes, content_type: str, head_only: bool = False,
              cache: bool = False, allow: str | None = None, cookie: str | None = None,
              disposition: str | None = None) -> None:
        self._headers(status, len(payload), content_type, cache=cache, allow=allow,
                      cookie=cookie, disposition=disposition)
        if not head_only and self.command != "HEAD":
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                pass

    def log_message(self, fmt: str, *args: Any) -> None:
        LOG.debug("%s: %s", self.client_address[0], fmt % args)


class DashboardServer(ThreadingMixIn, HTTPServer):
    """Limit connection threads and keep static paths on an explicit allowlist."""

    daemon_threads = False
    block_on_close = True
    allow_reuse_address = True
    request_queue_size = 8

    def __init__(self, address: tuple[str, int], monitor: Any, poll_interval: float = 5.0,
                 *, start_polling: bool = True, control_center: Any = None) -> None:
        self.control_center = control_center
        self.assets = {
            route: ((STATIC_ROOT / name).read_bytes(), content_type)
            for route, (name, content_type) in ASSETS.items()
        }
        self._slots = threading.BoundedSemaphore(8)
        self.poller = StatusPoller(monitor, poll_interval)
        super().__init__(address, DashboardHandler)
        if start_polling:
            self.poller.start()

    def get_request(self) -> tuple[Any, Any]:
        request, address = super().get_request()
        request.settimeout(30.0)
        return request, address

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._slots.acquire(blocking=False):
            try:
                request.sendall(b"HTTP/1.0 503 Service Unavailable\r\nConnection: close\r\n"
                                b"Retry-After: 1\r\nContent-Length: 0\r\n\r\n")
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def server_close(self) -> None:
        self.poller.stop()
        super().server_close()
        if self.control_center:
            self.control_center.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Brother DCP-150C local printer dashboard")
    parser.add_argument("--host", default="0.0.0.0", help="listen address (default: all IPv4 interfaces)")
    parser.add_argument("--port", type=int, default=8080, help="HTTP port (default: 8080)")
    parser.add_argument("--queue", default=os.environ.get("DCP150C_QUEUE") or None, help="CUPS queue to monitor")
    parser.add_argument("--device", default=os.environ.get("DCP150C_DEVICE", "/dev/usb/lp0"), help="USB printer device")
    parser.add_argument("--lock-path", default=os.environ.get("DCP150C_LOCK_PATH", "/run/lock/dcp150c-device.lock"),
                        help="lock shared with the CUPS wrapper")
    parser.add_argument("--poll-interval", type=float, default=5.0, help="seconds between polls (1–3600)")
    parser.add_argument("--timeout", type=float, default=10.0,
                        help="seconds allowed for each printer response (default: 10)")
    model_status_options = parser.add_mutually_exclusive_group()
    model_status_options.add_argument("--enable-model-status", dest="enable_model_status", action="store_true",
                                      help="read cartridge levels through the managed USB backend")
    model_status_options.add_argument("--disable-model-status", dest="enable_model_status", action="store_false",
                                      help="read basic connection and queue status without cartridge queries")
    parser.set_defaults(enable_model_status=os.environ.get("DCP150C_MODEL_STATUS", "1") == "1")
    parser.add_argument("--verbose", action="store_true", help="log HTTP requests and diagnostics")
    parser.add_argument("--state-directory", default=os.environ.get("DCP150C_STATE_DIRECTORY", "/var/lib/dcp150c-control-center"),
                        help="private Control Center document and settings directory")
    parser.add_argument("--password-file", default=os.environ.get("DCP150C_PASSWORD_FILE", "/etc/dcp150c-native/control-center-password.json"),
                        help="password hash file for browser actions")
    parser.add_argument("--disable-control-center", action="store_true", help="serve printer status without document controls")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if not 1.0 <= args.poll_interval <= 3600.0:
        parser.error("--poll-interval must be between 1 and 3600 seconds")
    if not 0.1 <= args.timeout <= 30.0:
        parser.error("--timeout must be between 0.1 and 30 seconds")
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    from .status import StatusMonitor

    monitor = StatusMonitor(queue=args.queue, device=args.device, lock_path=args.lock_path,
                            poll_interval=args.poll_interval, timeout=args.timeout,
                            enable_model_status=args.enable_model_status)
    control_center = None
    try:
        if not args.disable_control_center:
            from .control_center import ControlCenter
            control_center = ControlCenter(args.state_directory, args.password_file,
                                           queue=args.queue or "DCP150C-Native", lock_path=args.lock_path)
        server = DashboardServer((args.host, args.port), monitor, args.poll_interval, start_polling=False,
                                 control_center=control_center)
    except OSError as exc:
        if control_center:
            control_center.close()
        LOG.error("Cannot start dashboard: %s", exc)
        return 1

    shutdown_requested = False

    def terminate(_signum: int, _frame: Any) -> None:
        nonlocal shutdown_requested
        if not shutdown_requested:
            shutdown_requested = True
            raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    try:
        server.poller.start()
        LOG.info("Dashboard listening on %s:%s", args.host, args.port)
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        LOG.info("Stopping dashboard")
    finally:
        server.server_close()
    return 0

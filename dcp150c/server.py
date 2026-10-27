# SPDX-License-Identifier: GPL-2.0-or-later
"""Small, dependency-free local HTTP dashboard for the DCP-150C.

One background worker owns polling. Browsers receive cached snapshots, so
opening more dashboards does not increase USB traffic or delay printing.
"""

from __future__ import annotations

import argparse
import copy
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
from urllib.parse import parse_qs, urlsplit

from . import __version__


LOG = logging.getLogger("dcp150c.dashboard")
STATIC_ROOT = Path(__file__).with_name("static")
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/assets/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/assets/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
}


def unavailable_snapshot(message: str = "Waiting for the first printer reading.") -> dict[str, Any]:
    """Return unknown readings without representing them as empty cartridges."""
    return {
        "model": "Brother DCP-150C",
        "connected": False,
        "state": "unknown",
        "message": message,
        "ink": {
            key: {"percent": None, "state": "unknown", "label": label}
            for key, label in (("c", "Cyan"), ("m", "Magenta"), ("y", "Yellow"), ("k", "Black"))
        },
        "last_poll": None,
        "cups": {"available": False, "queue": None, "active": None, "jobs": [], "state": "unknown"},
        "capabilities": {"ink_percent": False, "usb_status": False},
        "warnings": [],
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
                result = unavailable_snapshot("Printer status is temporarily unavailable.")
                result["warnings"] = ["The last status request failed. It will be retried automatically."]
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
        self._send(405, b"Method not allowed.\n", "text/plain; charset=utf-8", allow="GET, HEAD")

    do_PUT = do_DELETE = do_PATCH = do_POST

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
        asset = self.server.assets.get(url.path)
        if asset is None:
            self._send(404, b"Not found.\n", "text/plain; charset=utf-8", head_only)
            return
        payload, content_type = asset
        self._send(200, payload, content_type, head_only, cache=url.path != "/")

    def _send(self, status: int, payload: bytes, content_type: str, head_only: bool = False,
              cache: bool = False, allow: str | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "public, max-age=3600" if cache else "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; "
                         "img-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.send_header("Connection", "close")
        if allow:
            self.send_header("Allow", allow)
        self.end_headers()
        self.close_connection = True
        if not head_only:
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                pass

    def log_message(self, fmt: str, *args: Any) -> None:
        LOG.debug("%s: %s", self.client_address[0], fmt % args)


class DashboardServer(ThreadingMixIn, HTTPServer):
    """Limit connection threads and keep static paths on an explicit allowlist."""

    daemon_threads = True
    block_on_close = False
    allow_reuse_address = True
    request_queue_size = 8

    def __init__(self, address: tuple[str, int], monitor: Any, poll_interval: float = 5.0,
                 *, start_polling: bool = True) -> None:
        self.assets = {
            route: ((STATIC_ROOT / name).read_bytes(), content_type)
            for route, (name, content_type) in ASSETS.items()
        }
        self._slots = threading.BoundedSemaphore(4)
        self.poller = StatusPoller(monitor, poll_interval)
        super().__init__(address, DashboardHandler)
        if start_polling:
            self.poller.start()

    def get_request(self) -> tuple[Any, Any]:
        request, address = super().get_request()
        request.settimeout(5.0)
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Brother DCP-150C local printer dashboard")
    parser.add_argument("--host", default="0.0.0.0", help="listen address (default: all IPv4 interfaces)")
    parser.add_argument("--port", type=int, default=8080, help="HTTP port (default: 8080)")
    parser.add_argument("--queue", default=os.environ.get("DCP150C_QUEUE") or None, help="CUPS queue to monitor")
    parser.add_argument("--device", default=os.environ.get("DCP150C_DEVICE", "/dev/usb/lp0"), help="USB printer device")
    parser.add_argument("--lock-path", default=os.environ.get("DCP150C_LOCK_PATH", "/run/lock/dcp150c-device.lock"),
                        help="lock shared with the CUPS wrapper")
    parser.add_argument("--poll-interval", type=float, default=5.0, help="seconds between polls (1–3600)")
    parser.add_argument("--timeout", type=float, default=2.0, help="USB status timeout in seconds")
    parser.add_argument("--enable-model-status", action="store_true",
                        default=os.environ.get("DCP150C_MODEL_STATUS", "0") == "1",
                        help="enable experimental stepped ink readings through the managed USB backend")
    parser.add_argument("--verbose", action="store_true", help="log HTTP requests and diagnostics")
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
    try:
        server = DashboardServer((args.host, args.port), monitor, args.poll_interval, start_polling=False)
    except OSError as exc:
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

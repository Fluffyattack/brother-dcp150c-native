# SPDX-License-Identifier: GPL-2.0-or-later
"""Password verification, short-lived browser sessions and request forgery protection."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import getpass
import grp
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import threading
import time


@dataclass
class BrowserSession:
    token: str
    csrf_token: str
    authenticated: bool
    expires_at: float


class AccessControl:
    def __init__(self, password_file: Path | str) -> None:
        self.password_file = Path(password_file)
        self._sessions: dict[str, BrowserSession] = {}
        self._failed_attempts: dict[str, list[float]] = {}
        self._guard = threading.Lock()

    @property
    def configured(self) -> bool:
        try:
            self._read_password()
            return True
        except (OSError, ValueError, KeyError, TypeError):
            return False

    def _read_password(self) -> dict:
        if self.password_file.is_symlink() or self.password_file.stat().st_size > 4096:
            raise ValueError("Invalid password configuration")
        settings = json.loads(self.password_file.read_text(encoding="ascii"))
        if settings.get("format") != 1 or len(bytes.fromhex(settings["salt"])) != 16 or len(bytes.fromhex(settings["hash"])) != 32:
            raise ValueError("Invalid password configuration")
        return settings

    def session(self, token: str | None, *, create: bool = False) -> BrowserSession | None:
        with self._guard:
            now = time.monotonic()
            self._sessions = {key: value for key, value in self._sessions.items() if value.expires_at > now}
            session = self._sessions.get(token or "")
            if session or not create:
                return session
            if len(self._sessions) >= 128:
                oldest = min(self._sessions, key=lambda key: self._sessions[key].expires_at)
                del self._sessions[oldest]
            session = BrowserSession(secrets.token_urlsafe(32), secrets.token_urlsafe(32), False, now + 900)
            self._sessions[session.token] = session
            return session

    def login(self, token: str, password: str, client: str) -> BrowserSession:
        now = time.monotonic()
        with self._guard:
            attempts = [attempt for attempt in self._failed_attempts.get(client, []) if attempt > now - 900]
            self._failed_attempts[client] = attempts
            if len(attempts) >= 5:
                raise ValueError("login_rate_limited")
            # Charge before expensive hashing; concurrent requests share the limit.
            attempts.append(now)
            if len(self._failed_attempts) > 256:
                self._failed_attempts = {key: value for key, value in self._failed_attempts.items() if value and value[-1] > now - 900}
        try:
            settings = self._read_password()
            provided = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(settings["salt"]),
                                      n=16384, r=8, p=1, dklen=32)
            valid = hmac.compare_digest(provided, bytes.fromhex(settings["hash"]))
        except (OSError, ValueError, KeyError, TypeError):
            valid = False
        if not valid:
            raise ValueError("login_failed")
        with self._guard:
            self._sessions.pop(token, None)
            self._failed_attempts.pop(client, None)
            session = BrowserSession(secrets.token_urlsafe(32), secrets.token_urlsafe(32), True, now + 43200)
            self._sessions[session.token] = session
        return session

    def logout(self, token: str) -> None:
        with self._guard:
            self._sessions.pop(token, None)


def save_password(path: Path, password: str) -> None:
    if not 12 <= len(password) <= 256:
        raise ValueError("Use a password between 12 and 256 characters.")
    salt = secrets.token_bytes(16)
    password_hash = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=16384, r=8, p=1, dklen=32)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + secrets.token_hex(8))
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640)
    try:
        if os.geteuid() == 0:
            try:
                os.fchown(descriptor, -1, grp.getgrnam("dcp150c").gr_gid)
            except KeyError:
                pass
        with os.fdopen(descriptor, "w", encoding="ascii") as output:
            json.dump({"format": 1, "salt": salt.hex(), "hash": password_hash.hex()}, output)
            output.write("\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Set the private Control Center password")
    parser.add_argument("--password-file", type=Path, default=Path("/etc/dcp150c-native/control-center-password.json"))
    args = parser.parse_args()
    password = getpass.getpass("New Control Center password (at least 12 characters): ")
    if password != getpass.getpass("Repeat password: "):
        parser.error("The passwords do not match.")
    save_password(args.password_file, password)
    print("Password saved. Restart the dashboard to close existing browser sessions.")


if __name__ == "__main__":
    main()

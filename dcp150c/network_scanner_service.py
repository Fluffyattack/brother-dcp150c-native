# SPDX-License-Identifier: GPL-2.0-or-later
"""Explicit LAN sharing through an isolated, supervised eSCL process.

Reading browser state never starts the process or discovers a USB device. The
modified AirSane binary holds the same kernel lock as printing and browser
scans. A stuck acquisition is stopped as a process, rather than unlocking a
device that its backend might still be using. Failed operations are not replayed.
"""

from __future__ import annotations

import copy
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import threading
import time

from .document_storage import StorageError


INTERFACE_NAME = re.compile(r"[A-Za-z0-9_.-]{1,15}\Z")
SOURCE_REVISION = "ac117f185e06afc7c3557f9839a42d8fd9fd18fa"


def local_network_interfaces() -> list[dict]:
    """Inspect local addresses only; no packets, discovery, or device access."""
    try:
        result = subprocess.run(["ip", "-j", "address", "show", "up"], check=True,
                                capture_output=True, timeout=3)
        records = json.loads(result.stdout)
        if not isinstance(records, list):
            return []
        interfaces = []
        for record in records:
            if not isinstance(record, dict):
                continue
            name = record.get("ifname", "")
            if not INTERFACE_NAME.fullmatch(name) or "LOOPBACK" in record.get("flags", []):
                continue
            addresses = []
            for entry in record.get("addr_info", []):
                if not isinstance(entry, dict):
                    continue
                try:
                    address = ipaddress.ip_address(entry["local"])
                except (KeyError, ValueError):
                    continue
                if not address.is_loopback and not address.is_multicast and not address.is_unspecified:
                    addresses.append(str(address))
            if addresses:
                interfaces.append({"name": name, "addresses": sorted(set(addresses))})
        return sorted(interfaces, key=lambda item: item["name"])
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        return []


class NetworkScannerService:
    def __init__(self, storage, *, lock_path="/run/lock/dcp150c-device.lock",
                 executable="/usr/lib/dcp150c-native/network-scanner/airsaned",
                 information_file="/usr/share/dcp150c-native/network-scanner/build-information.json",
                 sane_configuration="/etc/dcp150c-native/network-scanner/sane",
                 interfaces_provider=local_network_interfaces, start_worker=True,
                 allow_test_backend=False, metadata_timeout=60.0, session_timeout=180.0,
                 stop_grace=5.0, poll_interval=0.25):
        if min(metadata_timeout, session_timeout, stop_grace, poll_interval) <= 0:
            raise ValueError("Network scanner time limits must be positive")
        self.storage = storage
        self.lock_path = str(lock_path)
        self.executable, self.information_file = Path(executable), Path(information_file)
        self.sane_configuration = Path(sane_configuration)
        self.interfaces_provider = interfaces_provider
        self.allow_test_backend = allow_test_backend
        self.metadata_timeout, self.session_timeout = metadata_timeout, session_timeout
        self.stop_grace, self.poll_interval = stop_grace, poll_interval
        self.directory = storage.root / "network-scanner"
        if self.directory.is_symlink():
            raise ValueError("The network scanner state directory must not be a symbolic link")
        self.directory.mkdir(mode=0o700, exist_ok=True)
        self.directory.chmod(0o700)
        self._guard = threading.RLock()
        self._stop, self._wake, self._listening = threading.Event(), threading.Event(), threading.Event()
        self._worker = self._process = self._reader = None
        self._process_revision = self._failed_revision = None
        self._active_operation = self._last_exit_code = None
        self._started_at = 0.0
        self._bound_addresses = None
        self._interfaces = self.interfaces_provider()
        self._next_interfaces_check = time.monotonic() + 5
        self._available = self._installation_available()
        with storage.guard, storage.connection:
            storage.connection.executescript("""
                CREATE TABLE IF NOT EXISTS network_scanner_configuration (
                    id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL,
                    enabled INTEGER NOT NULL, interface TEXT NOT NULL, port INTEGER NOT NULL
                );
                INSERT OR IGNORE INTO network_scanner_configuration VALUES (1,0,0,'',8090);
            """)
            row = storage.connection.execute("SELECT * FROM network_scanner_configuration WHERE id=1").fetchone()
        self._configuration = {"revision": row["revision"], "enabled": bool(row["enabled"]),
                               "interface": row["interface"], "port": row["port"]}
        self._status_code = "network_scanner_disabled"
        if start_worker:
            self._worker = threading.Thread(target=self._run, name="dcp150c-network-scanner", daemon=False)
            self._worker.start()

    def _installation_available(self) -> bool:
        """Check installed files without executing even a help/version command."""
        try:
            for path in (self.executable, self.information_file):
                details = path.lstat()
                if not stat.S_ISREG(details.st_mode) or details.st_mode & 0o022:
                    return False
            if not os.access(self.executable, os.X_OK) or self.executable.stat().st_size > 16 * 1024 * 1024:
                return False
            information = json.loads(self.information_file.read_text())
            if (information.get("format") != 1 or information.get("protocol") != "eSCL"
                    or information.get("shared_device_access") is not True
                    or information.get("source", {}).get("revision") != SOURCE_REVISION):
                return False
            digest = hashlib.sha256(self.executable.read_bytes()).hexdigest()
            if digest != information.get("binary_sha256"):
                return False
            configuration = self.sane_configuration
            backend_list = configuration / "dll.conf"
            expected = "test" if self.allow_test_backend else "brother2"
            if (configuration.is_symlink() or backend_list.is_symlink() or (configuration / "dll.d").exists()
                    or backend_list.read_text().strip() != expected):
                return False
            return True
        except (OSError, ValueError, TypeError):
            return False

    def snapshot(self) -> dict:
        with self._guard:
            code = self._status_code
            if not self._configuration["enabled"] and self._process is None:
                code = "network_scanner_disabled"
            elif self._configuration["enabled"] and not self._available:
                code = "network_scanner_missing"
            return {"configuration": copy.deepcopy(self._configuration), "available": self._available,
                    "status_code": code, "interfaces": copy.deepcopy(self._interfaces),
                    "active_operation": self._active_operation, "last_exit_code": self._last_exit_code,
                    "hardware_verified": False}

    @staticmethod
    def _validate_configuration(values: dict) -> dict:
        if (not isinstance(values, dict) or set(values) != {"revision", "enabled", "interface", "port"}
                or type(values["revision"]) is not int or values["revision"] < 0
                or type(values["enabled"]) is not bool or type(values["port"]) is not int
                or not 1024 <= values["port"] <= 65535 or not isinstance(values["interface"], str)
                or (values["interface"] and not INTERFACE_NAME.fullmatch(values["interface"]))):
            raise StorageError("invalid_settings", "Choose a local network interface and a port from 1024 to 65535.")
        return copy.deepcopy(values)

    def _save_configuration(self, values: dict) -> None:
        with self.storage.guard, self.storage.connection:
            self.storage.connection.execute(
                "UPDATE network_scanner_configuration SET revision=?,enabled=?,interface=?,port=? WHERE id=1",
                (values["revision"], int(values["enabled"]), values["interface"], values["port"]))
        self._configuration = values
        self._failed_revision = None
        self._status_code = ("network_scanner_stopping" if self._process is not None
                             else "network_scanner_starting" if values["enabled"] else "network_scanner_disabled")
        self._wake.set()

    def configure(self, values: dict) -> dict:
        values = self._validate_configuration(values)
        with self._guard:
            if self._stop.is_set():
                raise StorageError("network_scanner_stopping", "The network scanner is stopping.")
            current = self._configuration
            if all(values[field] == current[field] for field in ("enabled", "interface", "port")):
                return self.snapshot()
            if values["revision"] != current["revision"]:
                raise StorageError("request_conflict", "Network scanner settings changed in another browser. Reload them first.")
            self._available = self._installation_available()
            self._interfaces = self.interfaces_provider()
            if values["enabled"]:
                if not self._available:
                    raise StorageError("network_scanner_missing", "Install the optional network scanner before enabling the LAN share.")
                if values["interface"] not in {entry["name"] for entry in self._interfaces}:
                    raise StorageError("network_scanner_network_changed", "Choose an available local network interface.")
            self._save_configuration({**values, "revision": current["revision"] + 1})
            return self.snapshot()

    def restart(self, values: dict) -> dict:
        if not isinstance(values, dict) or set(values) != {"revision"} or type(values["revision"]) is not int:
            raise StorageError("invalid_settings", "Reload the network scanner settings before restarting.")
        with self._guard:
            if self._stop.is_set():
                raise StorageError("network_scanner_stopping", "The network scanner is stopping.")
            if values["revision"] != self._configuration["revision"]:
                raise StorageError("request_conflict", "Network scanner settings changed in another browser. Reload them first.")
            if not self._configuration["enabled"]:
                raise StorageError("invalid_settings", "Enable the LAN share before restarting its service.")
            self._available = self._installation_available()
            if not self._available:
                raise StorageError("network_scanner_missing", "Install the optional network scanner before restarting.")
            self._save_configuration({**self._configuration, "revision": values["revision"] + 1})
            return self.snapshot()

    def _read_diagnostics(self, process) -> None:
        """Drain bounded lines; never retain client paths or acquired image data."""
        try:
            while line := process.stderr.readline(4096):
                if b"listening on " in line:
                    self._listening.set()
        finally:
            process.stderr.close()

    def _start_process(self, configuration: dict) -> None:
        access = self.directory / "access.conf"
        # At least one rule is essential: upstream treats an empty ACL as allow
        # all. This rule restricts clients to this interface's connected subnets.
        access.write_text("allow local on " + configuration["interface"] + "\n", encoding="ascii")
        options, ignored = self.directory / "options.conf", self.directory / "ignore.conf"
        options.write_text("", encoding="ascii")
        ignored.write_text("", encoding="ascii")
        for path in (access, options, ignored):
            path.chmod(0o600)
        environment = {name: value for name, value in os.environ.items()
                       if name in {"PATH", "LANG", "LC_ALL", "TZ"}}
        environment.update(SANE_CONFIG_DIR=str(self.sane_configuration), DCP150C_LOCK_PATH=self.lock_path,
                           DCP150C_NETWORK_SCANNER_STATE_DIRECTORY=str(self.directory))
        if self.allow_test_backend:
            environment["DCP150C_NETWORK_SCANNER_ALLOW_TEST_BACKEND"] = "1"
        arguments = [str(self.executable), "--listen-port=" + str(configuration["port"]),
                     "--interface=" + configuration["interface"], "--hotplug=false", "--network-hotplug=false",
                     # The pinned upstream version suppresses its listening
                     # notification otherwise. Drain diagnostics through the
                     # private pipe; do not retain its request/device details.
                     "--debug=true",
                     "--mdns-announce=" + ("false" if self.allow_test_backend else "true"),
                     "--web-interface=false", "--reset-option=false", "--disclose-version=false",
                     "--local-scanners-only=true", "--job-timeout=30", "--purge-interval=2",
                     "--access-file=" + str(access), "--options-file=" + str(options), "--ignore-list=" + str(ignored)]
        self._listening.clear()
        (self.directory / "lease.json").unlink(missing_ok=True)
        process = subprocess.Popen(arguments, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.PIPE, env=environment, start_new_session=True, close_fds=True)
        self._process = process
        self._bound_addresses = next(entry["addresses"][:] for entry in self._interfaces
                                     if entry["name"] == configuration["interface"])
        self._process_revision = configuration["revision"]
        self._started_at = time.monotonic()
        self._last_exit_code = None
        self._status_code = "network_scanner_starting"
        self._reader = threading.Thread(target=self._read_diagnostics, args=(process,),
                                        name="dcp150c-network-scanner-output", daemon=False)
        self._reader.start()

    def _stop_process(self, reason: str) -> None:
        process = self._process
        if process is None:
            return
        self._status_code = "network_scanner_stopping"
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=self.stop_grace)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                # Do not pretend the device is free before the child really
                # exits. An uninterruptible kernel operation can delay this.
                process.wait()
        self._last_exit_code = process.returncode
        self._reader.join()
        self._reader = self._process = None
        self._bound_addresses = None
        self._active_operation = None
        self._status_code = reason

    def _lease(self) -> tuple[str | None, float]:
        path = self.directory / "lease.json"
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except FileNotFoundError:
            return None, 0.0
        with os.fdopen(descriptor, "rb") as source:
            details = os.fstat(source.fileno())
            if not stat.S_ISREG(details.st_mode) or details.st_size > 1024 or details.st_uid != os.geteuid():
                raise ValueError("Unsafe network scanner lease")
            lease = json.loads(source.read(1025))
        if not isinstance(lease, dict):
            raise ValueError("Invalid network scanner lease")
        acquired = lease.get("acquired_monotonic")
        kind = lease.get("kind")
        if (kind not in {"metadata", "session"} or type(lease.get("process_id")) is not int
                or lease["process_id"] != self._process.pid or type(acquired) not in {int, float}
                or not math.isfinite(acquired) or acquired < 0 or acquired > time.monotonic() + 1):
            raise ValueError("Invalid network scanner lease")
        return kind, max(0.0, time.monotonic() - acquired)

    def _iteration(self) -> None:
        """Only the supervisor thread owns child-process creation and shutdown."""
        with self._guard:
            configuration = copy.deepcopy(self._configuration)
            if self._process is not None and (self._stop.is_set() or not configuration["enabled"]
                                              or configuration["revision"] != self._process_revision):
                self._stop_process("network_scanner_disabled")
            if self._stop.is_set():
                return
            if time.monotonic() >= self._next_interfaces_check:
                self._interfaces = self.interfaces_provider()
                self._next_interfaces_check = time.monotonic() + 5
            if not configuration["enabled"] or self._failed_revision == configuration["revision"]:
                return
            selected = next((entry for entry in self._interfaces if entry["name"] == configuration["interface"]), None)
            if selected is None or (self._process is not None and selected["addresses"] != self._bound_addresses):
                self._stop_process("network_scanner_network_changed")
                self._status_code = "network_scanner_network_changed"
                self._failed_revision = configuration["revision"]
                return
            if self._process is None:
                self._available = self._installation_available()
                if not self._available:
                    self._status_code = "network_scanner_missing"
                    self._failed_revision = configuration["revision"]
                    return
                try:
                    self._start_process(configuration)
                except (OSError, ValueError):
                    self._status_code = "network_scanner_failed"
                    self._failed_revision = configuration["revision"]
                return
            if self._process.poll() is not None:
                self._stop_process("network_scanner_failed")
                self._failed_revision = configuration["revision"]
                return
            try:
                kind, elapsed = self._lease()
                self._active_operation = kind
                deadline = self.session_timeout if kind == "session" else self.metadata_timeout
                timed_out = (kind is not None and elapsed > deadline)
                timed_out |= not self._listening.is_set() and time.monotonic() - self._started_at > self.metadata_timeout
                if timed_out:
                    self._stop_process("network_scanner_timed_out")
                    self._failed_revision = configuration["revision"]
                elif self._listening.is_set():
                    self._status_code = "network_scanner_busy" if kind == "session" else "network_scanner_ready"
            except (OSError, ValueError, TypeError):
                self._stop_process("network_scanner_failed")
                self._failed_revision = configuration["revision"]

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                self._iteration()
                self._wake.wait(self.poll_interval)
                self._wake.clear()
        finally:
            with self._guard:
                self._stop_process("network_scanner_disabled")

    def close(self) -> None:
        with self._guard:
            self._stop.set()
            self._wake.set()
        if self._worker is not None:
            self._worker.join()
        else:
            with self._guard:
                self._stop_process("network_scanner_disabled")

# SPDX-License-Identifier: GPL-2.0-or-later
"""Opt-in scan-button profiles, cached state and durable document jobs.

No USB access occurs while disabled. Two consecutive framed responses without
a button event establish a baseline after enabling, reconnecting or finishing
a button job. Repeated event responses cannot enqueue another job until that
baseline has been established again. Actual device event semantics still
require hardware validation; generated status frames only verify this policy.
"""

from __future__ import annotations

import copy
import json
import logging
import threading
import uuid

from .document_storage import IDENTIFIER, StorageError, utc_time


LOG = logging.getLogger("dcp150c.scan-button")
BUTTON_ACTIONS = {3: "image", 2: "ocr", 8: "email", 5: "file"}


def decode_scan_button_status(status: bytes) -> str | None:
    """Return the observed USB action, or None for a framed non-event response."""
    if (not isinstance(status, bytes) or not 5 <= len(status) <= 255
            or status[1] != 0x10 or status[2] != 0x03):
        raise StorageError("scan_button_status_invalid", "The scanner-button response is not recognised.")
    if status[3] != 0x20:
        return None
    action = BUTTON_ACTIONS.get(status[4])
    if action is None:
        raise StorageError("scan_button_unassigned", "This scanner-button action is not supported.")
    return action


class ScanButtonService:
    def __init__(self, storage, jobs, resolve_binding, perform_action, *, transport=None,
                 lock_path="/run/lock/dcp150c-device.lock", poll_interval=1.0,
                 start_worker=True):
        if not 0.05 <= poll_interval <= 60:
            raise ValueError("Choose a scanner-button polling interval between 0.05 and 60 seconds.")
        if transport is None:
            from .scan_button_transport import ScanButtonTransport
            transport = ScanButtonTransport(lock_path=lock_path)
        self.storage, self.jobs = storage, jobs
        self.resolve_binding, self.perform_action = resolve_binding, perform_action
        self.transport, self.poll_interval = transport, poll_interval
        self._guard = threading.RLock()
        self._poll_guard = threading.Lock()
        self._stop, self._wake = threading.Event(), threading.Event()
        self._poll_cancel = threading.Event()
        self._worker = None
        self._quiet_readings = 0
        self._device_identity = None
        self._last_checked_at = None
        with storage.guard, storage.connection:
            storage.connection.executescript("""
                CREATE TABLE IF NOT EXISTS scan_button_configuration (
                    id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL,
                    enabled INTEGER NOT NULL, bindings TEXT NOT NULL,
                    last_job_id TEXT, last_action TEXT
                );
                INSERT OR IGNORE INTO scan_button_configuration VALUES (1,0,0,'{}',NULL,NULL);
            """)
            row = storage.connection.execute("SELECT * FROM scan_button_configuration WHERE id=1").fetchone()
        self._configuration = {"revision": row["revision"], "enabled": bool(row["enabled"]),
                               "bindings": json.loads(row["bindings"])}
        self._last_job_id, self._last_action = row["last_job_id"], row["last_action"]
        self._status_code = "scan_button_waiting" if self._configuration["enabled"] else "scan_button_disabled"
        if start_worker:
            self._worker = threading.Thread(target=self._run, name="dcp150c-scan-button", daemon=False)
            self._worker.start()

    def _job_active(self) -> bool:
        if not self._last_job_id:
            return False
        try:
            return self.jobs.get(self._last_job_id)["state"] in {"queued", "running"}
        except StorageError:
            return False

    def snapshot(self) -> dict:
        """Return cached configuration and job metadata without device access."""
        with self._guard:
            code = self._status_code
            if not self._configuration["enabled"]:
                code = "scan_button_disabled"
            elif self._job_active():
                code = "scan_button_running"
            elif code == "scan_button_running":
                code = "scan_button_waiting"
            return {"configuration": copy.deepcopy(self._configuration), "status_code": code,
                    "last_checked_at": self._last_checked_at, "last_job_id": self._last_job_id,
                    "last_action": self._last_action, "hardware_verified": False}

    @staticmethod
    def _validate_configuration(values: dict) -> dict:
        if (not isinstance(values, dict) or set(values) != {"revision", "enabled", "bindings"}
                or type(values["revision"]) is not int or values["revision"] < 0
                or type(values["enabled"]) is not bool or not isinstance(values["bindings"], dict)
                or set(values["bindings"]) - set(BUTTON_ACTIONS.values())):
            raise StorageError("invalid_settings", "Choose profiles for the scanner button.")
        for action, binding in values["bindings"].items():
            fields = {"scan_profile_id", "ocr_profile_id"} if action == "ocr" else {"scan_profile_id"}
            if (not isinstance(binding, dict) or set(binding) != fields
                    or any(not isinstance(identifier, str) or not IDENTIFIER.fullmatch(identifier)
                           for identifier in binding.values())):
                raise StorageError("invalid_profile", "Choose saved scanning and text-recognition profiles.")
        if values["enabled"] and not values["bindings"]:
            raise StorageError("invalid_profile", "Choose at least one scanner-button profile before enabling it.")
        return copy.deepcopy(values)

    def configure(self, values: dict) -> dict:
        values = self._validate_configuration(values)
        with self._guard:
            current = self._configuration
            if values["enabled"] == current["enabled"] and values["bindings"] == current["bindings"]:
                return self.snapshot()  # A lost save response must not rearm the listener.
            if values["revision"] != current["revision"]:
                raise StorageError("request_conflict", "Scanner-button settings changed in another browser. Reload them first.")
            if values["enabled"]:
                for action, binding in values["bindings"].items():
                    self.resolve_binding(action, binding)
            updated = {**values, "revision": current["revision"] + 1}
            with self.storage.guard, self.storage.connection:
                self.storage.connection.execute("UPDATE scan_button_configuration SET revision=?,enabled=?,bindings=? WHERE id=1",
                    (updated["revision"], int(updated["enabled"]), json.dumps(updated["bindings"])))
            self._configuration = updated
            self._quiet_readings, self._device_identity = 0, None
            self._status_code = "scan_button_waiting" if updated["enabled"] else "scan_button_disabled"
            self._poll_cancel.set()
            self._wake.set()
            return self.snapshot()

    def poll_once(self) -> dict:
        """One explicit receiver iteration; also usable with an isolated transport."""
        if not self._poll_guard.acquire(blocking=False):
            return self.snapshot()
        try:
            with self._guard:
                if self._stop.is_set() or not self._configuration["enabled"]:
                    return self.snapshot()
                if self._job_active():
                    self._quiet_readings = 0
                    self._status_code = "scan_button_running"
                    return self.snapshot()
                revision = self._configuration["revision"]
                cancel = threading.Event()
                self._poll_cancel = cancel
            try:
                reading = self.transport.read_status(cancel)
                identity = reading["device_identity"]
                if not isinstance(identity, str) or not identity or len(identity) > 4096:
                    raise StorageError("scan_button_status_invalid", "The scanner-button device identity is invalid.")
                action = decode_scan_button_status(reading["status"])
                error_code = None
            except Exception as error:
                action, identity = None, None
                error_code = getattr(error, "code", "scan_button_status_failed")
            with self._guard:
                if self._stop.is_set() or revision != self._configuration["revision"]:
                    return self.snapshot()
                self._last_checked_at = utc_time()
                if error_code:
                    if error_code == "device_busy":
                        # Preserve an already established baseline while
                        # printing holds the lock; incomplete observations
                        # must still start again with two clear readings.
                        if self._quiet_readings < 2:
                            self._quiet_readings = 0
                        self._status_code = "scan_button_busy"
                    else:
                        self._quiet_readings, self._device_identity = 0, None
                        self._status_code = error_code
                    return self.snapshot()
                if identity != self._device_identity:
                    self._quiet_readings, self._device_identity = 0, identity
                if action is None:
                    self._quiet_readings = min(2, self._quiet_readings + 1)
                    self._status_code = "scan_button_ready" if self._quiet_readings == 2 else "scan_button_waiting"
                    return self.snapshot()
                if self._quiet_readings < 2:
                    self._quiet_readings = 0
                    self._status_code = "scan_button_waiting"
                    return self.snapshot()
                self._quiet_readings = 0
                binding = self._configuration["bindings"].get(action)
                if binding is None:
                    self._status_code = "scan_button_unassigned"
                    return self.snapshot()
                try:
                    parameters = self.resolve_binding(action, copy.deepcopy(binding))
                    parameters = copy.deepcopy({**parameters, "button_action": action})
                    def reserve(identifier):
                        self.storage.connection.execute("UPDATE scan_button_configuration SET last_job_id=?,last_action=? WHERE id=1",
                            (identifier, action))
                    accepted = self.jobs.submit("scan_button", parameters,
                        lambda event, identifier: self.perform_action(event, identifier, parameters),
                        "scan-button-" + uuid.uuid4().hex, on_reserved=reserve)
                except Exception as error:
                    self._status_code = getattr(error, "code", "scan_button_status_failed")
                    return self.snapshot()
                self._last_job_id, self._last_action = accepted["id"], action
                self._status_code = "scan_button_running"
                return self.snapshot()
        finally:
            self._poll_guard.release()

    def _run(self) -> None:
        while not self._stop.is_set():
            with self._guard:
                enabled = self._configuration["enabled"]
            if enabled:
                try:
                    self.poll_once()
                except Exception:
                    LOG.warning("Scanner-button reception is temporarily unavailable.")
            self._wake.wait(self.poll_interval if enabled else None)
            self._wake.clear()

    def close(self) -> None:
        # Publish stop and cancel the currently owned read atomically with
        # poll_once installing its token; no new read may escape cancellation.
        with self._guard:
            self._stop.set()
            self._poll_cancel.set()
            self._wake.set()
        if self._worker is not None:
            self._worker.join()

"""Printer status with one lock shared with the printing backend.

Linux USB printer-class status and local printing activity are read by default.
Optional Printer Job Language (PJL) requests also read the model's stepped ink
levels. They require the managed printing backend and explicit configuration.
Missing ink data stays unknown.
"""

from __future__ import annotations

import array
import copy
from datetime import datetime, timezone
import errno
import fcntl
import http.client
import os
from pathlib import Path
import stat
import struct
import threading
import time
from urllib.parse import quote, unquote, urlsplit

from .status_protocol import StatusTransportError, query_printer_status


MODEL = "Brother DCP-150C"
USB_VENDOR = "04f9"
USB_PRODUCT = "01cf"
DEFAULT_LOCK = "/run/lock/dcp150c-device.lock"
LPGETSTATUS = 0x060B
_MAX_IPP = 1024 * 1024
_PRINTER_ATTRIBUTES = (
    "printer-name", "printer-make-and-model", "device-uri", "printer-state",
    "printer-state-message", "printer-state-reasons", "marker-colors",
    "marker-names", "marker-types", "marker-levels", "marker-low-levels",
    "marker-high-levels",
)


class StatusError(Exception):
    """A failed status read; never evidence that the printer is ready."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _ipp_attribute(tag: int, name: str, values: list[str]) -> bytes:
    result = bytearray()
    for index, value in enumerate(values):
        key = name.encode("ascii") if index == 0 else b""
        data = value.encode("utf-8")
        result.extend(struct.pack(">BH", tag, len(key)) + key)
        result.extend(struct.pack(">H", len(data)) + data)
    return bytes(result)


def _ipp_request(operation: int, attributes: list[tuple[int, str, list[str]]]) -> bytes:
    data = bytearray(struct.pack(">BBHI", 1, 1, operation, 1) + b"\x01")
    data.extend(_ipp_attribute(0x47, "attributes-charset", ["utf-8"]))
    data.extend(_ipp_attribute(0x48, "attributes-natural-language", ["en"]))
    for tag, name, values in attributes:
        data.extend(_ipp_attribute(tag, name, values))
    data.append(3)
    return bytes(data)


def _decode_ipp(data: bytes) -> list[tuple[int, dict[str, list[object]]]]:
    """Decode bounded IPP attribute groups; reject incomplete/status-error replies."""
    if len(data) < 9 or data[0] not in (1, 2):
        raise StatusError("Invalid IPP response")
    code = struct.unpack_from(">H", data, 2)[0]
    if code > 0x00FF:
        raise StatusError("The local print service could not provide the requested queue information.")
    offset = 8
    groups: list[tuple[int, dict[str, list[object]]]] = []
    group: dict[str, list[object]] | None = None
    name: str | None = None
    while offset < len(data):
        tag = data[offset]
        offset += 1
        if tag == 3:
            return groups
        if tag < 0x10:
            group = {}
            groups.append((tag, group))
            name = None
            continue
        if group is None or offset + 2 > len(data):
            raise StatusError("Malformed IPP attribute group")
        size = struct.unpack_from(">H", data, offset)[0]
        offset += 2
        if offset + size + 2 > len(data):
            raise StatusError("Truncated IPP attribute name")
        if size:
            name = data[offset:offset + size].decode("utf-8", "replace")
        elif name is None:
            raise StatusError("IPP continuation without attribute name")
        offset += size
        size = struct.unpack_from(">H", data, offset)[0]
        offset += 2
        if offset + size > len(data):
            raise StatusError("Truncated IPP attribute value")
        raw = data[offset:offset + size]
        offset += size
        if tag in (0x21, 0x23):
            if size != 4:
                raise StatusError("Invalid IPP integer")
            value: object = struct.unpack(">i", raw)[0]
        elif tag == 0x22:
            if size != 1:
                raise StatusError("Invalid IPP boolean")
            value = bool(raw[0])
        elif tag < 0x20:
            value = None
        elif tag in (0x35, 0x36):
            if size < 4:
                raise StatusError("Invalid IPP language string")
            language_size = struct.unpack_from(">H", raw)[0]
            start = 2 + language_size
            if start + 2 > size:
                raise StatusError("Truncated IPP language string")
            text_size = struct.unpack_from(">H", raw, start)[0]
            if start + 2 + text_size != size:
                raise StatusError("Invalid IPP language string length")
            value = raw[start + 2:].decode("utf-8", "replace")
        else:
            value = raw.decode("utf-8", "replace")
        group.setdefault(name, []).append(value)
    raise StatusError("IPP response has no end-of-attributes marker")


def _first(attributes: dict, key: str, default=None):
    values = attributes.get(key)
    return values[0] if values else default


def _is_model_queue(attributes: dict) -> bool:
    description = "".join(str(_first(attributes, key, "")) for key in (
        "printer-name", "printer-make-and-model", "device-uri"))
    return "dcp150c" in "".join(character for character in description.lower()
                                if character.isalnum())


def _uses_shared_backend(attributes: dict) -> bool:
    device_uri = str(_first(attributes, "device-uri", ""))
    expected_uri = "dcp150c:usb://Brother/DCP-150C"
    return device_uri == expected_uri or device_uri.startswith(expected_uri + "?")


def _uses_configured_device(attributes: dict, configured_device: Path) -> bool:
    """Recognize local device aliases even when their queue has a generic name."""
    try:
        device_path = unquote(urlsplit(str(_first(attributes, "device-uri", ""))).path)
        if not device_path.startswith("/dev/"):
            return False
        candidate = Path(device_path)
        if candidate.resolve() == configured_device.resolve():
            return True
        candidate_metadata = candidate.stat()
        configured_metadata = configured_device.stat()
        return (stat.S_ISCHR(candidate_metadata.st_mode)
                and stat.S_ISCHR(configured_metadata.st_mode)
                and candidate_metadata.st_rdev == configured_metadata.st_rdev)
    except (OSError, ValueError):
        return False


def _model_polling_obstacle(printers: list[dict], selected_queue: str | None,
                           configured_device: Path = Path("/dev/usb/lp0")) -> str | None:
    """Every enabled queue for this model must respect the shared USB lock."""
    selected = next((attributes for attributes in printers
                     if _first(attributes, "printer-name") == selected_queue), None)
    if selected is None or not _uses_shared_backend(selected):
        return "Ink polling requires a queue installed with the shared dcp150c USB backend."
    for attributes in printers:
        if _first(attributes, "printer-state") == 5:
            continue  # A stopped legacy queue cannot start another print job.
        uses_printer = _is_model_queue(attributes) or _uses_configured_device(attributes, configured_device)
        if uses_printer and not _uses_shared_backend(attributes):
            return "Ink polling is deferred while another enabled DCP-150C queue bypasses the shared USB lock."
    return None


class CupsClient:
    """A small read-only client restricted to the local CUPS scheduler."""

    def __init__(self, timeout: float = 2.0):
        self.timeout = timeout

    def _request(self, operation: int, path: str, attributes: list) -> list:
        connection = http.client.HTTPConnection("localhost", 631, timeout=self.timeout)
        try:
            connection.request("POST", path, body=_ipp_request(operation, attributes),
                               headers={"Content-Type": "application/ipp"})
            response = connection.getresponse()
            if response.status != 200:
                raise StatusError("The local print service did not accept the status request.")
            data = response.read(_MAX_IPP + 1)
            if len(data) > _MAX_IPP:
                raise StatusError("The local print service returned too much status data.")
            return _decode_ipp(data)
        except (OSError, http.client.HTTPException) as exc:
            raise StatusError("The local print service is unavailable.") from exc
        finally:
            connection.close()

    def printers(self) -> list[dict]:
        groups = self._request(0x4002, "/", [
            (0x44, "requested-attributes", list(_PRINTER_ATTRIBUTES)),
        ])
        return [attributes for tag, attributes in groups if tag == 4]

    def printer(self, queue: str) -> dict:
        path = "/printers/" + quote(queue, safe="")
        groups = self._request(0x000B, path, [
            (0x45, "printer-uri", ["ipp://localhost" + path]),
            (0x44, "requested-attributes", list(_PRINTER_ATTRIBUTES)),
        ])
        return next((attributes for tag, attributes in groups if tag == 4), {})

    def jobs(self) -> list[dict]:
        # All local jobs are checked, including sibling queues for the same USB
        # printer. An unrelated job can defer a probe, but cannot permit a race.
        groups = self._request(0x000A, "/", [
            (0x45, "printer-uri", ["ipp://localhost/"]),
            (0x44, "which-jobs", ["not-completed"]),
            (0x44, "requested-attributes", ["job-id", "job-state", "job-printer-uri"]),
        ])
        jobs = []
        for tag, attributes in groups:
            if tag != 2:
                continue
            job_id = _first(attributes, "job-id")
            state = _first(attributes, "job-state")
            uri = _first(attributes, "job-printer-uri", "")
            jobs.append({"id": job_id, "state": state,
                         "queue": str(uri).rsplit("/", 1)[-1]})
        return jobs


def _unknown_ink() -> dict:
    return {key: {"percent": None, "state": "unknown", "label": label,
                  "source": None}
            for key, label in (("c", "Cyan"), ("m", "Magenta"),
                               ("y", "Yellow"), ("k", "Black"))}


def _cups_ink(attributes: dict) -> dict:
    """Use explicit CUPS marker percentages only; sentinel values stay unknown."""
    ink = _unknown_ink()
    levels = attributes.get("marker-levels", [])
    names = attributes.get("marker-names", [])
    colors = attributes.get("marker-colors", [])
    types = attributes.get("marker-types", [])
    aliases = {"cyan": "c", "magenta": "m", "yellow": "y", "black": "k",
               "#00ffff": "c", "#ff00ff": "m", "#ffff00": "y", "#000000": "k"}
    seen = set()
    for index, level in enumerate(levels):
        if index < len(types) and types[index] not in ("ink", "ink-cartridge", "ink-ribbon"):
            continue
        name = str(names[index]).strip().lower() if index < len(names) else ""
        color = str(colors[index]).strip().lower() if index < len(colors) else ""
        key = aliases.get(color) or aliases.get(name)
        if key is None:
            for alias in ("cyan", "magenta", "yellow", "black"):
                if alias in name:
                    key = aliases[alias]
                    break
        if key is None or key in seen:
            continue
        seen.add(key)
        if type(level) is not int or not 0 <= level <= 100:
            continue
        low = attributes.get("marker-low-levels", [])
        threshold = low[index] if index < len(low) and type(low[index]) is int else None
        state = "empty" if level == 0 else "low" if threshold is not None and level <= threshold else "ok"
        ink[key].update(percent=level, state=state, source="cups")
    return ink


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="ascii", errors="replace").strip()
    except OSError:
        return ""


def _usb_devices(sysfs_root: Path) -> list[Path]:
    devices = sysfs_root / "bus/usb/devices"
    try:
        return [path.resolve() for path in devices.iterdir()
                if _read_text(path / "idVendor").lower() == USB_VENDOR
                and _read_text(path / "idProduct").lower() == USB_PRODUCT]
    except OSError:
        return []


def _matching_node(device: Path, sysfs_root: Path, usb_devices: list[Path]) -> bool:
    """Do not query an arbitrary lp0 belonging to a different printer."""
    try:
        if not stat.S_ISCHR(device.stat().st_mode):
            return False
        for subsystem in ("usbmisc", "usb"):
            node = sysfs_root / "class" / subsystem / device.resolve().name / "device"
            if node.exists():
                ancestors = (node.resolve(), *node.resolve().parents)
                return any(target in ancestors for target in usb_devices)
    except OSError:
        pass
    return False


def _port_status(device: Path) -> int:
    # Linux drivers/usb/class/usblp.c implements LPGETSTATUS as the standard
    # USB printer-class GET_PORT_STATUS control request, returning an int.
    # The kernel bounds this control transfer to five seconds. No bulk stream
    # bytes, resets, protocol switches or undocumented commands are written.
    descriptor = os.open(device, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        value = array.array("i", [0])
        fcntl.ioctl(descriptor, LPGETSTATUS, value, True)
        return value[0] & 0xFF
    finally:
        os.close(descriptor)


class StatusMonitor:
    """Cache JSON-safe snapshots. Run forced polls outside HTTP request threads.

    ``timeout`` bounds each of the two model response phases. Local print-service
    requests use at most two seconds. A USB class-status ioctl has the Linux
    driver's own five-second timeout. Polling never waits for the printer lock.
    """

    def __init__(self, queue: str | None = None, device: str = "/dev/usb/lp0",
                 lock_path: str = DEFAULT_LOCK, poll_interval: float = 5.0,
                 sysfs_root: str = "/sys", timeout: float = 10.0,
                 enable_model_status: bool = False):
        if not 0.1 <= float(timeout) <= 30:
            raise ValueError("timeout must be between 0.1 and 30 seconds")
        if not 0.1 <= float(poll_interval) <= 3600:
            raise ValueError("poll_interval must be between 0.1 and 3600 seconds")
        self.queue = queue
        self.device = Path(device)
        self.lock_path = Path(lock_path)
        self.sysfs_root = Path(sysfs_root)
        self.poll_interval = float(poll_interval)
        self.timeout = float(timeout)
        self.enable_model_status = bool(enable_model_status)
        self.cups = CupsClient(min(float(timeout), 2.0))
        self._stop_requested = threading.Event()
        self._mutex = threading.Lock()
        self._last_monotonic = float("-inf")
        self._cached = self._blank()

    def _blank(self) -> dict:
        return {"model": MODEL, "connected": False, "state": "unknown",
                "message": "Waiting for the first status poll.", "ink": _unknown_ink(),
                "last_poll": None,
                "cups": {"available": False, "queue": self.queue, "active": None,
                         "jobs": [], "state": "unknown"},
                "capabilities": {"ink_percent": False, "ink_stepped": False,
                                 "usb_status": False, "model_status": False},
                "warnings": []}

    def _read_model_status(self, result: dict) -> None:
        """Called only while holding the USB lock and after checking identity."""
        try:
            obstacle = _model_polling_obstacle(self.cups.printers(), result["cups"]["queue"], self.device)
            if self._stop_requested.is_set():
                return
            if obstacle:
                result["warnings"].append(obstacle)
                return
            # Recheck after acquiring the lock. A newly queued job takes priority.
            if self.cups.jobs():
                result["warnings"].append("Ink polling is deferred because a print job has just arrived.")
                return
            if self._stop_requested.is_set():
                return
            model_status = query_printer_status(str(self.device), timeout=self.timeout)
        except StatusError:
            result["warnings"].append("Ink polling was skipped because current printing activity could not be checked.")
            return
        except StatusTransportError as exception:
            result["warnings"].append(str(exception))
            result["warnings"].extend(exception.cleanup_errors)
            result.update(state="unknown", message="The printer status request could not be completed.")
            if exception.partial_status is not None:
                self._merge_model_status(result, exception.partial_status, exchange_complete=False)
            return
        except OSError:
            result["warnings"].append("The printer's bidirectional ink query could not be completed.")
            return
        self._merge_model_status(result, model_status, exchange_complete=True)

    def _merge_model_status(self, result: dict, model_status: dict, *, exchange_complete: bool) -> None:
        result["model_status"] = copy.deepcopy(model_status)
        result["capabilities"]["model_status"] = exchange_complete
        for cartridge, reading in model_status["ink"].items():
            if reading["source"] is not None:
                # An explicitly received unknown counter supersedes cached data.
                result["ink"][cartridge] = copy.deepcopy(reading)
        result["capabilities"]["ink_percent"] = any(
            reading["percent"] is not None for reading in result["ink"].values())
        result["capabilities"]["ink_stepped"] = any(
            reading.get("granularity") == "stepped" and reading["percent"] is not None
            for reading in result["ink"].values())
        if model_status["state"] != "unknown":
            result.update(state=model_status["state"], message=model_status["message"])
        elif model_status["code"] is not None or model_status["display"]:
            # An unfamiliar model status must not be hidden by a generic ready bit.
            result.update(state="unknown", message=model_status["message"])
        result["warnings"].extend(model_status["warnings"])
        if not model_status.get("hardware_verified"):
            result["warnings"].append("This model response has not been verified through the printer connection.")

    def cancel_pending_poll(self) -> None:
        """Finish an in-flight exchange, but do not start another USB operation."""
        self._stop_requested.set()

    def snapshot(self, force: bool = False) -> dict:
        with self._mutex:
            if not self._stop_requested.is_set() and (
                    force or time.monotonic() - self._last_monotonic >= self.poll_interval):
                self._cached = self._poll()
                self._cached["last_poll"] = _utc_now()
                self._last_monotonic = time.monotonic()
            return copy.deepcopy(self._cached)

    def _poll(self) -> dict:
        result = self._blank()
        result["last_poll"] = _utc_now()
        devices = _usb_devices(self.sysfs_root)
        result["connected"] = bool(devices)
        result["state"] = "unknown" if devices else "offline"
        result["message"] = ("USB printer detected; status has not been queried."
                             if devices else "DCP-150C is disconnected or switched off.")
        attributes = {}
        try:
            if self.queue:
                attributes = self.cups.printer(self.queue)
            else:
                matches = [item for item in self.cups.printers() if _is_model_queue(item)]
                if len(matches) == 1:
                    attributes = matches[0]
                elif len(matches) > 1:
                    result["warnings"].append("Multiple DCP-150C print queues were found; choose one in the dashboard settings.")
            queue = _first(attributes, "printer-name", self.queue)
            if self._stop_requested.is_set():
                return result
            jobs = self.cups.jobs()
            cups_state = {3: "idle", 4: "printing", 5: "stopped"}.get(
                _first(attributes, "printer-state"), "unknown")
            active = cups_state == "printing" or any(job["queue"] == queue for job in jobs)
            result["cups"].update(available=True, queue=queue, active=active,
                                  jobs=jobs, state=cups_state,
                                  message=_first(attributes, "printer-state-message", ""),
                                  reasons=attributes.get("printer-state-reasons", []))
            if devices:
                result["ink"] = _cups_ink(attributes)
                result["capabilities"]["ink_percent"] = any(
                    item["percent"] is not None for item in result["ink"].values())
                if result["capabilities"]["ink_percent"]:
                    result["warnings"].append("Ink values come from the print service and may be saved from an earlier reading.")
            if active or jobs:
                if devices:
                    if active:
                        result.update(state="printing", message="Print jobs are active; printer status requests will wait.")
                    else:
                        result.update(state="busy", message="Another print queue has active jobs; printer status requests will wait.")
                result["warnings"].append("Printer status requests wait while print jobs are active.")
                return result
        except StatusError as exc:
            result["warnings"].append(str(exc))
            result["warnings"].append("Printer status requests wait because printing activity could not be checked.")
            return result

        if not devices or self._stop_requested.is_set():
            return result
        if not _matching_node(self.device, self.sysfs_root, devices):
            result["warnings"].append("The configured USB printer device is unavailable or belongs to a different model.")
            return result

        lock_descriptor = None
        try:
            # Installation creates the lock with the same permissions used by
            # the CUPS backend. Missing/broken lock means no USB probe.
            lock_descriptor = os.open(self.lock_path, os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW)
            if not stat.S_ISREG(os.fstat(lock_descriptor).st_mode):
                raise OSError(errno.EINVAL, "Printer lock is not a regular file")
            try:
                fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                result.update(state="busy", message="Printer USB connection is in use; polling is deferred.")
                return result

            # The native backend shares the flock, so a job queued after the
            # check above cannot interleave its data with this status request.
            if self._stop_requested.is_set():
                return result
            status = _port_status(self.device)
            result["capabilities"]["usb_status"] = True
            result["usb"] = {"port_status": status, "source": "linux-usblp"}
            if status & 0x20:
                result.update(state="error", message="Printer reports that paper is empty.")
            elif not status & 0x08:
                result.update(state="error", message="Printer reports an error; check its control panel.")
            elif not status & 0x10:
                result.update(state="offline", message="Printer is not selected/online.")
            else:
                result.update(state="ready", message="USB printer-class status reports ready.")
            if self.enable_model_status and not self._stop_requested.is_set():
                self._read_model_status(result)
            if not result["capabilities"]["ink_percent"]:
                result["warnings"].append("Ink levels are unavailable from the current status sources.")
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EPERM):
                result["warnings"].append("The dashboard does not have permission to use the printer connection.")
            elif exc.errno in (errno.ENOENT, errno.ENODEV):
                result["warnings"].append("The shared printer connection is unavailable.")
            elif exc.errno == errno.EBUSY:
                result.update(state="busy", message="Printer USB connection is busy; polling is deferred.")
            else:
                result["warnings"].append("USB printer-class status query failed.")
        finally:
            if lock_descriptor is not None:
                os.close(lock_descriptor)
        return result

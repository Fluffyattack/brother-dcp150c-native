# SPDX-License-Identifier: GPL-2.0-or-later
"""Explicit, cancellable SANE scanner operations sharing the printer's USB lock.

Importing this module, reading cached capabilities, and starting the dashboard
never enumerate USB devices. Only an authenticated user operation should call
``probe`` or ``scan``. Distances are millimetres and resolution is dots per inch.
"""

from __future__ import annotations

import fcntl
import math
import os
from pathlib import Path
import re
import shutil
import stat
import threading
import time
from typing import Any


class ScannerError(RuntimeError):
    """A stable error code that the web interface can translate."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def parse_scanner_options(description: str) -> dict[str, Any]:
    """Read scanimage's advertised choices without inventing device features."""
    modes_match = re.search(r"^\s*--mode\s+(.+?)(?:\s+\[[^\n]*\])?\s*$", description, re.MULTILINE)
    raw_modes = []
    if modes_match:
        raw_modes = [mode.strip() for mode in modes_match.group(1).split("|") if mode.strip()]
    mode_values: dict[str, str] = {}
    for identifier, candidates in (
        ("color", ("24bit Color", "Color", "24bit Color[Fast]", "RGB")),
        ("gray", ("True Gray", "Gray", "Grayscale")),
        ("lineart", ("Black & White", "Lineart", "Binary")),
    ):
        for candidate in candidates:
            if candidate in raw_modes:
                mode_values[identifier] = candidate
                break

    resolution_match = re.search(r"^\s*--resolution\s+(.+?)(?:\s+\[[^\n]*\])?\s*$", description, re.MULTILINE)
    resolutions: list[int] = []
    if resolution_match:
        specification = resolution_match.group(1)
        range_match = re.search(r"(\d+)\.\.(\d+)", specification)
        if range_match:
            lower, upper = map(int, range_match.groups())
            resolutions = [value for value in (75, 100, 150, 200, 300, 600) if lower <= value <= upper]
        else:
            resolutions = sorted({int(value) for value in re.findall(r"\d+", specification) if 50 <= int(value) <= 600})

    area: dict[str, float] = {}
    for option, key in (("x", "width"), ("y", "height")):
        match = re.search(r"^\s*-" + option + r"\s+([\d.]+)\.\.([\d.]+)mm", description, re.MULTILINE)
        if match:
            value = float(match.group(2))
            if math.isfinite(value) and 1 <= value <= 500:
                area[key] = value
    return {"modes": list(mode_values), "mode_values": mode_values,
            "resolutions": resolutions, "area": area}


class ScannerService:
    def __init__(self, command_runner: Any = None,
                 lock_path: str = "/run/lock/dcp150c-device.lock", device: str = "",
                 scanimage: str = "scanimage", *, allow_test_backend: bool = False) -> None:
        self.command_runner = command_runner
        self.lock_path = Path(lock_path)
        self.device = device or os.environ.get("DCP150C_SCANNER_DEVICE", "")
        self.scanimage = scanimage
        self.allow_test_backend = allow_test_backend
        self._guard = threading.Lock()
        self._capabilities: dict[str, Any] = {
            "available": False, "configured": bool(self.device), "device": self.device,
            "modes": [], "resolutions": [], "area": {}, "reason_code": "scanner_not_checked",
            "probed_at": None, "hardware_verified": False,
        }

    def capabilities(self) -> dict[str, Any]:
        """Return a copy of the last explicit probe; do not touch the device."""
        import copy
        with self._guard:
            result = copy.deepcopy(self._capabilities)
        if not shutil.which(self.scanimage):
            result.update(available=False, reason_code="scanner_software_missing")
        return result

    def _set_capabilities(self, values: dict[str, Any]) -> dict[str, Any]:
        with self._guard:
            self._capabilities.update(values)
        return self.capabilities()

    def _lock_device(self, cancel_event: threading.Event, timeout: float = 30) -> int:
        try:
            descriptor = os.open(self.lock_path, os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW)
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                os.close(descriptor)
                raise OSError("Unsafe device lock")
        except OSError as error:
            raise ScannerError("scanner_lock_unavailable", "The shared printer lock is unavailable.") from error
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return descriptor
            except BlockingIOError:
                if cancel_event.wait(0.1) or time.monotonic() >= deadline:
                    os.close(descriptor)
                    if cancel_event.is_set():
                        raise ScannerError("operation_cancelled", "The scanner operation was cancelled.")
                    raise ScannerError("device_busy", "The printer is busy. Try again after its current operation.")
            except BaseException:
                os.close(descriptor)
                raise

    def _run(self, arguments: list[str], cancel_event: threading.Event, timeout: float,
             output_path: Path | None = None) -> bytes:
        """Bound both captured diagnostics and files, and reap every child."""
        from .command_runner import CommandError, run_command
        runner = getattr(self.command_runner, "run", self.command_runner) if self.command_runner is not None else run_command
        try:
            result = runner(arguments, cancel_event=cancel_event, timeout=timeout, output_path=output_path)
        except CommandError as error:
            codes = {"job_cancelled": "operation_cancelled", "processing_timeout": "scanner_timeout",
                     "dependency_unavailable": "scanner_software_missing", "processing_output_limit": "scanner_output_too_large"}
            raise ScannerError(codes.get(error.code, "scanner_operation_failed"), str(error)) from error
        if output_path is not None:
            if output_path.stat().st_size > 100 * 1024 * 1024:
                raise ScannerError("scanner_output_too_large", "The scanner output exceeded the size limit.")
            return b""
        response = result.stdout if hasattr(result, "stdout") else result
        if not isinstance(response, bytes) or len(response) > 1024 * 1024:
            raise ScannerError("scanner_output_too_large", "The scanner response exceeded the size limit.")
        return response

    def probe(self, cancel_event: threading.Event | None = None) -> dict[str, Any]:
        """Explicitly enumerate this model and read its supported scan options."""
        cancel_event = cancel_event or threading.Event()
        if not shutil.which(self.scanimage):
            return self._set_capabilities({"available": False, "reason_code": "scanner_software_missing"})
        descriptor = self._lock_device(cancel_event)
        try:
            listing = self._run([self.scanimage, "--formatted-device-list=%d\t%v\t%m\t%t%n"],
                                cancel_event, 25).decode("utf-8", "replace")
            selected_device = ""
            for line in listing.splitlines():
                columns = line.split("\t")
                if len(columns) < 3:
                    continue
                name, vendor, model = columns[:3]
                matches_model = vendor.casefold() == "brother" and bool(re.search(r"DCP[- _]?150C", model, re.IGNORECASE))
                matches_test = self.allow_test_backend and name.startswith("test:")
                if not (matches_model or matches_test):
                    continue
                if self.device and name != self.device:
                    continue
                if selected_device:
                    raise ScannerError("scanner_selection_required", "More than one matching scanner is connected. Select a scanner in the local configuration.")
                selected_device = name
            if not selected_device:
                return self._set_capabilities({"available": False, "reason_code": "scanner_not_found", "probed_at": time.time()})
            description = self._run([self.scanimage, "--device-name=" + selected_device, "--help"],
                                    cancel_event, 25).decode("utf-8", "replace")
            options = parse_scanner_options(description)
            usable = bool(options["modes"] and options["resolutions"] and len(options["area"]) == 2)
            self.device = selected_device
            return self._set_capabilities({**options, "available": usable, "configured": True,
                                           "device": selected_device, "probed_at": time.time(),
                                           "reason_code": "scanner_ready" if usable else "scanner_options_unavailable",
                                           "hardware_verified": False})
        except ScannerError as error:
            self._set_capabilities({"available": False, "reason_code": error.code, "probed_at": time.time()})
            raise
        finally:
            os.close(descriptor)

    def scan(self, output_path: Path | str, settings: dict[str, Any],
             cancel_event: threading.Event) -> dict[str, Any]:
        capabilities = self.capabilities()
        if not capabilities["available"]:
            raise ScannerError(capabilities["reason_code"], "Check the scanner connection before starting a scan.")
        if cancel_event.is_set():
            raise ScannerError("operation_cancelled", "The scanner operation was cancelled.")
        resolution = settings.get("resolution", 300)
        mode = settings.get("mode", "color")
        if type(resolution) is not int or resolution not in capabilities["resolutions"] or mode not in capabilities["mode_values"]:
            raise ScannerError("invalid_scan_settings", "The scanner does not support the selected mode or resolution.")
        requested_area = settings.get("area") or {}
        if not isinstance(requested_area, dict):
            raise ScannerError("invalid_scan_settings", "The scan area is invalid.")
        area = {}
        for key, default in (("left", 0), ("top", 0), ("width", min(210, capabilities["area"]["width"])),
                             ("height", min(297, capabilities["area"]["height"]))):
            value = requested_area.get(key, default)
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ScannerError("invalid_scan_settings", "The scan area is invalid.")
            area[key] = float(value)
        if (area["width"] < 1 or area["height"] < 1 or
                area["left"] + area["width"] > capabilities["area"]["width"] + 0.01 or
                area["top"] + area["height"] > capabilities["area"]["height"] + 0.01):
            raise ScannerError("invalid_scan_settings", "The selected area lies outside the scanner glass.")
        # Limit decoded pixels before invoking scanimage (RGB adds three bytes/pixel).
        if area["width"] * area["height"] * (resolution / 25.4) ** 2 > 40_000_000:
            raise ScannerError("scan_too_large", "Choose a smaller scan area or a lower resolution.")
        output_path = Path(output_path)
        if output_path.exists() or output_path.is_symlink():
            raise ScannerError("scan_destination_exists", "The scan destination already exists.")
        arguments = [self.scanimage, "--device-name=" + self.device, "--format=png",
                     "--mode=" + capabilities["mode_values"][mode], "--resolution=" + str(resolution),
                     "-l", str(area["left"]), "-t", str(area["top"]),
                     "-x", str(area["width"]), "-y", str(area["height"])]
        descriptor = self._lock_device(cancel_event)
        succeeded = False
        try:
            self._run(arguments, cancel_event, 180, output_path)
            from PIL import Image
            with Image.open(output_path) as image:
                if image.format != "PNG" or image.width * image.height > 40_000_000:
                    raise ScannerError("invalid_scan_output", "The scanner did not produce a valid image.")
                image.verify()
                result = {"width": image.width, "height": image.height,
                          "resolution": resolution, "mode": mode, "area": area,
                          "content_type": "image/png", "hardware_verified": not self.allow_test_backend}
            self._set_capabilities({"hardware_verified": not self.allow_test_backend})
            succeeded = True
            return result
        finally:
            os.close(descriptor)
            if not succeeded:
                output_path.unlink(missing_ok=True)

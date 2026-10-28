# SPDX-License-Identifier: GPL-2.0-or-later
"""Explicit, read-only scan-button status requests for the Brother DCP-150C.

Construction and import perform no device discovery or input/output. Each
``read_status`` call reads kernel-cached USB metadata, takes the printer's shared
lock without waiting, and makes exactly one device-to-host vendor request.
After a successful request it holds the lock for a further 100 milliseconds,
including when cancellation is requested during that interval.
It never changes configuration, claims an interface, detaches a driver or resets
the device. Status bytes are returned unchanged for a separate event parser.

Linux requires a writable usbfs descriptor even for an input control request.
Opening that descriptor may also resume a suspended device. The finite control
timeout bounds the transfer, not kernel mutex waits or system scheduling; a
cancellation request is checked before and after that synchronous operation.
No physical button behaviour has been verified by this transport's fixtures.
"""

from __future__ import annotations

import ctypes
from dataclasses import dataclass
import errno
import fcntl
import hashlib
import math
import os
from pathlib import Path
import stat
import threading
import time
from typing import Any, Callable


DEFAULT_LOCK = "/run/lock/dcp150c-device.lock"
USB_VENDOR = "04f9"
USB_PRODUCT = "01cf"
USB_DEVICE_MAJOR = 189
STATUS_LENGTH = 255
STATUS_QUIET_INTERVAL_SECONDS = 0.1
MAXIMUM_ENUMERATED_DEVICES = 2048


class ScanButtonError(RuntimeError):
    """A stable code suitable for translation by the browser interface."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class _ControlTransfer(ctypes.Structure):
    """Native ``usbdevfs_ctrltransfer`` from linux/usbdevice_fs.h.

    The pointer is deliberately native, including its alignment: 24 bytes on
    64-bit ARM and 16 bytes on 32-bit ARM. Do not pack this structure.
    """

    _fields_ = [
        ("request_type", ctypes.c_uint8),
        ("request", ctypes.c_uint8),
        ("value", ctypes.c_uint16),
        ("index", ctypes.c_uint16),
        ("length", ctypes.c_uint16),
        ("timeout_milliseconds", ctypes.c_uint32),
        ("data", ctypes.c_void_p),
    ]


# Linux's asm-generic/ioctl.h encoding, shared by ARM and AArch64. The size must
# describe this process's ABI, including when a 32-bit process uses a 64-bit OS.
USBDEVFS_CONTROL = (3 << 30) | (ctypes.sizeof(_ControlTransfer) << 16) | (ord("U") << 8)


def _native_control_request(descriptor: int, request: int, transfer: _ControlTransfer) -> int:
    """Call the native ioctl once; ctypes releases the GIL during the syscall."""
    library = ctypes.CDLL(None, use_errno=True)
    operation = library.ioctl
    operation.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_void_p]
    operation.restype = ctypes.c_int
    result = operation(descriptor, request, ctypes.byref(transfer))
    if result < 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))
    return result


def _open_without_symlinks(path: Path, flags: int) -> int:
    """Open an absolute path, rejecting links in every component."""
    directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for component in path.parts[1:-1]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                            dir_fd=directory)
            os.close(directory)
            directory = child
        return os.open(path.name, flags | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
                       dir_fd=directory)
    finally:
        os.close(directory)


def _node_identity(information: os.stat_result) -> tuple[int, int, int, int]:
    # usbfs updates node timestamps after a control ioctl, so timestamps cannot
    # be used to detect replacement of the USB node itself.
    return (information.st_dev, information.st_ino, information.st_rdev,
            stat.S_IFMT(information.st_mode))


def _resolve_registry(path: Path) -> Path:
    try:
        return path.resolve(strict=True)
    except RuntimeError as error:
        raise OSError(errno.ELOOP, "The USB metadata registry contains a symbolic-link loop.") from error


def _read_attribute(directory: int, name: str, *, optional: bool = False) -> str:
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=directory)
    except FileNotFoundError:
        if optional:
            return ""
        raise
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError(errno.EINVAL, "The cached USB attribute is not a regular file.")
        raw = os.read(descriptor, 1025)
        if len(raw) > 1024 or b"\x00" in raw:
            raise OSError(errno.EINVAL, "The cached USB attribute is invalid.")
        try:
            return raw.decode("utf-8", "strict").strip()
        except UnicodeError as error:
            raise OSError(errno.EINVAL, "The cached USB attribute is invalid.") from error
    finally:
        os.close(descriptor)


@dataclass(frozen=True)
class _UsbDevice:
    registry_entry: Path
    sysfs_path: Path
    sysfs_identity: tuple[int, int, int]
    bus_number: int
    device_number: int
    serial_number: str
    node: Path
    node_identity: tuple[int, int, int, int]

    def public_identity(self) -> str:
        components = (str(self.sysfs_path), self.sysfs_identity, self.bus_number,
                      self.device_number, self.serial_number, self.node_identity)
        return hashlib.sha256(repr(components).encode("utf-8")).hexdigest()


class ScanButtonTransport:
    """One bounded input request, only when explicitly called by its owner.

    Test roots and an ioctl fixture require ``allow_test_backend=True`` together.
    That backend only accepts regular files in its isolated device tree and
    cannot fall back to a real USB ioctl.
    """

    def __init__(self, lock_path: str = DEFAULT_LOCK, printer_serial: str = "", *,
                 timeout_milliseconds: int = 500, total_budget_seconds: float = 1.0,
                 sysfs_root: Path | str = "/sys", device_root: Path | str = "/dev",
                 allow_test_backend: bool = False,
                 ioctl_fixture: Callable[[int, int, _ControlTransfer], int] | None = None) -> None:
        if type(timeout_milliseconds) is not int or not 1 <= timeout_milliseconds <= 1000:
            raise ValueError("The scan-button timeout must be between 1 and 1000 milliseconds.")
        if (isinstance(total_budget_seconds, bool) or not isinstance(total_budget_seconds, (int, float))
                or not math.isfinite(total_budget_seconds) or not 0.01 <= total_budget_seconds <= 2):
            raise ValueError("The scan-button operation budget must be between 0.01 and 2 seconds.")
        if not isinstance(printer_serial, str) or len(printer_serial) > 256 or "\x00" in printer_serial:
            raise ValueError("The printer serial number must be a short text value.")
        self.lock_path = Path(lock_path)
        self.sysfs_root, self.device_root = Path(sysfs_root), Path(device_root)
        for path in (self.lock_path, self.sysfs_root, self.device_root):
            if not path.is_absolute() or ".." in path.parts or path == Path("/"):
                raise ValueError("The device transport requires absolute, unambiguous paths.")
        if not allow_test_backend:
            if self.sysfs_root != Path("/sys") or self.device_root != Path("/dev") or ioctl_fixture is not None:
                raise ValueError("Fixture paths and ioctl callbacks require an explicit test backend.")
        else:
            if (self.sysfs_root.is_relative_to("/sys") or self.device_root.is_relative_to("/dev")
                    or self.sysfs_root == self.device_root or not callable(ioctl_fixture)):
                raise ValueError("The test backend requires isolated metadata, regular files and an ioctl fixture.")
        self.printer_serial = printer_serial
        self.timeout_milliseconds = timeout_milliseconds
        self.total_budget_seconds = float(total_budget_seconds)
        self.allow_test_backend = bool(allow_test_backend)
        self._control_request = ioctl_fixture if allow_test_backend else _native_control_request

    @staticmethod
    def _check(cancel_event: threading.Event, deadline: float) -> None:
        if cancel_event.is_set():
            raise ScanButtonError("operation_cancelled", "The scan-button status request was cancelled.")
        if time.monotonic() >= deadline:
            raise ScanButtonError("scan_button_timeout", "The scan-button status request exceeded its time budget.")

    @staticmethod
    def _wait_after_status() -> None:
        """Preserve the protocol's quiet interval while still owning the lock.

        Cancellation and the operation deadline are deliberately checked only
        after this interval. An early unlock would let another printer or
        scanner operation violate the same minimum spacing.
        """
        quiet_deadline = time.monotonic() + STATUS_QUIET_INTERVAL_SECONDS
        while True:
            remaining_seconds = quiet_deadline - time.monotonic()
            if remaining_seconds <= 0:
                return
            try:
                time.sleep(remaining_seconds)
            except InterruptedError:
                continue

    def _lock_device(self) -> int:
        descriptor = None
        acquired = False
        try:
            descriptor = _open_without_symlinks(self.lock_path, os.O_RDWR)
            information = os.fstat(descriptor)
            if not stat.S_ISREG(information.st_mode):
                raise OSError(errno.EINVAL, "The shared printer lock is not a regular file.")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ScanButtonError("device_busy", "The printer is busy with another operation.") from error
            if _node_identity(self.lock_path.lstat()) != _node_identity(information):
                raise OSError(errno.ESTALE, "The shared printer lock was replaced.")
            acquired = True
            return descriptor
        except OSError as error:
            raise ScanButtonError("scan_button_lock_unavailable", "The shared printer lock is unavailable.") from error
        finally:
            if descriptor is not None and not acquired:
                os.close(descriptor)

    def _device_snapshot(self, registry_entry: Path) -> _UsbDevice | None:
        resolved = _resolve_registry(registry_entry)
        if not resolved.is_relative_to(self.sysfs_root / "devices"):
            raise OSError(errno.EPERM, "The USB registry points outside the device metadata tree.")
        descriptor = _open_without_symlinks(resolved, os.O_RDONLY | os.O_DIRECTORY)
        try:
            before = os.fstat(descriptor)
            vendor = _read_attribute(descriptor, "idVendor")
            product = _read_attribute(descriptor, "idProduct")
            if vendor.casefold() != USB_VENDOR or product.casefold() != USB_PRODUCT:
                return None
            serial_number = _read_attribute(descriptor, "serial", optional=True)
            numbers = [_read_attribute(descriptor, name) for name in ("busnum", "devnum", "dev")]
            bus_text, device_text, device_number_text = numbers
            if (not bus_text.isascii() or not bus_text.isdecimal() or not device_text.isascii()
                    or not device_text.isdecimal()):
                raise OSError(errno.EINVAL, "The cached USB bus or device number is invalid.")
            bus_number, device_number = int(bus_text), int(device_text)
            if not 1 <= bus_number <= 999 or not 1 <= device_number <= 127:
                raise OSError(errno.EINVAL, "The cached USB address is out of range.")
            minor_number = (bus_number - 1) * 128 + device_number - 1
            expected_number = f"{USB_DEVICE_MAJOR}:{minor_number}"
            if device_number_text != expected_number:
                raise OSError(errno.EINVAL, "The USB address and device-node number do not match.")
            character_entry = self.sysfs_root / "dev" / "char" / expected_number
            if _resolve_registry(character_entry) != resolved:
                raise OSError(errno.EPERM, "The USB character-device registry does not match the selected model.")
            node = self.device_root / "bus" / "usb" / f"{bus_number:03d}" / f"{device_number:03d}"
            node_descriptor = _open_without_symlinks(node, os.O_PATH)
            try:
                node_information = os.fstat(node_descriptor)
            finally:
                os.close(node_descriptor)
            if self.allow_test_backend:
                valid_type = stat.S_ISREG(node_information.st_mode)
            else:
                valid_type = (stat.S_ISCHR(node_information.st_mode)
                              and node_information.st_rdev == os.makedev(USB_DEVICE_MAJOR, minor_number))
            if not valid_type:
                raise OSError(errno.EPERM, "The USB node is not the selected character device.")
            after = os.fstat(descriptor)
            sysfs_identity = (before.st_dev, before.st_ino, before.st_ctime_ns)
            if sysfs_identity != (after.st_dev, after.st_ino, after.st_ctime_ns):
                raise OSError(errno.ESTALE, "The USB metadata changed during discovery.")
            return _UsbDevice(registry_entry, resolved, sysfs_identity, bus_number, device_number,
                              serial_number, node, _node_identity(node_information))
        finally:
            os.close(descriptor)

    def _discover(self, cancel_event: threading.Event, deadline: float) -> _UsbDevice:
        matches = []
        # sysfs values here are cached by the kernel. No descriptor request is
        # made merely to find a matching model or read its serial number.
        with os.scandir(self.sysfs_root / "bus" / "usb" / "devices") as entries:
            for index, entry in enumerate(entries):
                self._check(cancel_event, deadline)
                if index >= MAXIMUM_ENUMERATED_DEVICES:
                    raise ScanButtonError("scan_button_status_failed", "The USB registry exceeds the discovery limit.")
                try:
                    candidate = self._device_snapshot(Path(entry.path))
                except FileNotFoundError:
                    # Interfaces have no idVendor; devices can also disappear
                    # while sysfs is being enumerated. Neither is a match.
                    continue
                if candidate is not None and (not self.printer_serial or candidate.serial_number == self.printer_serial):
                    matches.append(candidate)
                if len(matches) > 1:
                    raise ScanButtonError("scan_button_selection_required", "Select a single printer serial number before reading its scan button.")
        if not matches:
            raise ScanButtonError("scan_button_not_found", "No matching Brother DCP-150C USB device is available.")
        return matches[0]

    def _validate_current(self, device: _UsbDevice, descriptor: int) -> None:
        try:
            current = self._device_snapshot(device.registry_entry)
            if current != device or _node_identity(os.fstat(descriptor)) != device.node_identity:
                raise OSError(errno.ESTALE, "The selected USB device was replaced.")
        except OSError as error:
            raise ScanButtonError("scan_button_device_changed", "The USB device was removed or changed during the status request.") from error

    @staticmethod
    def _translate_error(error: OSError) -> ScanButtonError:
        if error.errno in (errno.EACCES, errno.EPERM, errno.ELOOP):
            return ScanButtonError("scan_button_access_denied", "The selected USB device or its metadata cannot be accessed safely.")
        if error.errno in (errno.ENOENT, errno.ENODEV, errno.ENXIO, errno.ENOTDIR, errno.ESTALE):
            return ScanButtonError("scan_button_not_found", "No matching Brother DCP-150C USB device is available.")
        if error.errno in (errno.ETIMEDOUT, errno.ETIME):
            return ScanButtonError("scan_button_timeout", "The scan-button status request timed out.")
        if error.errno in (errno.EBUSY, errno.EAGAIN):
            return ScanButtonError("device_busy", "The printer is busy with another operation.")
        return ScanButtonError("scan_button_status_failed", "The scan-button status request could not be completed.")

    def read_status(self, cancel_event: threading.Event | None = None) -> dict[str, Any]:
        """Return the actual response bytes and an opaque connection identity.

        A zero-length or short response is returned without padding. The event
        parser must require its full header; unread bytes cannot become events.
        There are no retries, output requests or implicit configuration changes.
        """
        cancel_event = cancel_event if cancel_event is not None else threading.Event()
        deadline = time.monotonic() + self.total_budget_seconds
        self._check(cancel_event, deadline)
        lock_descriptor = self._lock_device()
        usb_descriptor = None
        try:
            self._check(cancel_event, deadline)
            device = self._discover(cancel_event, deadline)
            self._check(cancel_event, deadline)
            # USBDEVFS_CONTROL requires FMODE_WRITE, even for request type0xc0.
            usb_descriptor = _open_without_symlinks(device.node, os.O_RDWR)
            self._validate_current(device, usb_descriptor)
            self._check(cancel_event, deadline)
            remaining_milliseconds = int((deadline - time.monotonic()) * 1000)
            if remaining_milliseconds < 1:
                raise ScanButtonError("scan_button_timeout", "No time remains for the scan-button status request.")
            response = (ctypes.c_ubyte * STATUS_LENGTH)()
            transfer = _ControlTransfer(0xC0, 3, 0, 0, STATUS_LENGTH,
                                        min(self.timeout_milliseconds, remaining_milliseconds),
                                        ctypes.addressof(response))
            try:
                received_length = self._control_request(usb_descriptor, USBDEVFS_CONTROL, transfer)
            except OSError as error:
                self._check(cancel_event, deadline)
                self._validate_current(device, usb_descriptor)
                raise self._translate_error(error) from error
            self._wait_after_status()
            self._check(cancel_event, deadline)
            self._validate_current(device, usb_descriptor)
            self._check(cancel_event, deadline)
            if type(received_length) is not int or not 0 <= received_length <= STATUS_LENGTH:
                raise ScanButtonError("scan_button_invalid_response", "The scan-button response length is invalid.")
            return {"status": bytes(response[:received_length]), "device_identity": device.public_identity()}
        except OSError as error:
            self._check(cancel_event, deadline)
            raise self._translate_error(error) from error
        finally:
            if usb_descriptor is not None:
                os.close(usb_descriptor)
            os.close(lock_descriptor)

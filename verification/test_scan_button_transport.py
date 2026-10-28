# SPDX-License-Identifier: GPL-2.0-or-later
"""Isolated sysfs, regular-file USB nodes and simulated control ioctl only."""

import ctypes
import errno
import fcntl
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from dcp150c import scan_button_transport as transport_module
from dcp150c.scan_button_transport import (
    ScanButtonError, ScanButtonTransport, STATUS_LENGTH, USBDEVFS_CONTROL, _ControlTransfer,
)


class NativeHeaderLayout(unittest.TestCase):
    @unittest.skipUnless(shutil.which("cc") and Path("/usr/include/linux/usbdevice_fs.h").is_file(),
                         "A C compiler and Linux USB headers are required for the native ABI check")
    def test_native_ctypes_layout_and_ioctl_number_match_actual_kernel_headers(self):
        # The executable only prints sizeof/offsetof/constants. It cannot open a
        # device or issue an ioctl; use the actual host ABI instead of a mock.
        source = r'''
#include <stddef.h>
#include <stdio.h>
#include <sys/ioctl.h>
#include <linux/usbdevice_fs.h>
int main(void) {
    printf("{\"size\":%zu,\"request_type\":%zu,\"request\":%zu,"
           "\"value\":%zu,\"index\":%zu,\"length\":%zu,"
           "\"timeout_milliseconds\":%zu,\"data\":%zu,\"ioctl\":%lu}\n",
           sizeof(struct usbdevfs_ctrltransfer),
           offsetof(struct usbdevfs_ctrltransfer, bRequestType),
           offsetof(struct usbdevfs_ctrltransfer, bRequest),
           offsetof(struct usbdevfs_ctrltransfer, wValue),
           offsetof(struct usbdevfs_ctrltransfer, wIndex),
           offsetof(struct usbdevfs_ctrltransfer, wLength),
           offsetof(struct usbdevfs_ctrltransfer, timeout),
           offsetof(struct usbdevfs_ctrltransfer, data),
           (unsigned long) USBDEVFS_CONTROL);
    return 0;
}
'''
        with tempfile.TemporaryDirectory(prefix="dcp150c-usb-layout-") as directory:
            root = Path(directory)
            source_file, executable = root / "layout.c", root / "layout"
            source_file.write_text(source, encoding="ascii")
            subprocess.run([shutil.which("cc"), "-std=c11", "-Wall", "-Wextra", "-Werror",
                            str(source_file), "-o", str(executable)], check=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
            actual = json.loads(subprocess.check_output([str(executable)], timeout=5))
        expected = {name: getattr(_ControlTransfer, name).offset for name, _ in _ControlTransfer._fields_}
        expected.update(size=ctypes.sizeof(_ControlTransfer), ioctl=USBDEVFS_CONTROL)
        self.assertEqual(actual, expected)
        self.assertEqual(actual["size"], 24 if ctypes.sizeof(ctypes.c_void_p) == 8 else 16)

    def test_native_call_passes_a_structure_pointer_and_preserves_errno(self):
        class NativeFunction:
            def __call__(self, descriptor, request, pointer):
                self.arguments = descriptor, request, pointer
                ctypes.set_errno(errno.ETIMEDOUT)
                return -1

        operation = NativeFunction()
        library = type("Library", (), {"ioctl": operation})()
        transfer = _ControlTransfer(0xC0, 3, 0, 0, 255, 200, None)
        with patch.object(ctypes, "CDLL", return_value=library) as load_library:
            with self.assertRaises(OSError) as failure:
                transport_module._native_control_request(123, USBDEVFS_CONTROL, transfer)
        self.assertEqual(failure.exception.errno, errno.ETIMEDOUT)
        load_library.assert_called_once_with(None, use_errno=True)
        self.assertEqual(operation.argtypes, [ctypes.c_int, ctypes.c_ulong, ctypes.c_void_p])
        self.assertEqual(operation.restype, ctypes.c_int)
        self.assertEqual(operation.arguments[:2], (123, USBDEVFS_CONTROL))
        self.assertEqual(ctypes.cast(operation.arguments[2], ctypes.c_void_p).value, ctypes.addressof(transfer))


class ScanButtonFixtures(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="dcp150c-scan-button-")
        self.root = Path(self.temporary.name)
        self.sysfs = self.root / "sysfs"
        self.devices = self.root / "device-nodes"
        self.registry = self.sysfs / "bus" / "usb" / "devices"
        self.registry.mkdir(parents=True)
        (self.sysfs / "dev" / "char").mkdir(parents=True)
        self.lock = self.root / "shared-device.lock"
        self.lock.touch()
        self.calls = []
        self.response = b"\x00\x10\x03\x20\x05"
        self.node = self.add_device()

    def tearDown(self):
        self.temporary.cleanup()

    def add_device(self, address=7, serial="PRINTER-ONE", vendor="04f9", product="01cf"):
        device = self.sysfs / "devices" / "platform" / "usb1" / f"1-{address}"
        device.mkdir(parents=True)
        for name, value in {"idVendor": vendor, "idProduct": product, "serial": serial,
                            "busnum": "1", "devnum": str(address), "dev": f"189:{address - 1}"}.items():
            (device / name).write_text(value + "\n", encoding="utf-8")
        (self.registry / f"1-{address}").symlink_to(device)
        (self.sysfs / "dev" / "char" / f"189:{address - 1}").symlink_to(device)
        node = self.devices / "bus" / "usb" / "001" / f"{address:03d}"
        node.parent.mkdir(parents=True, exist_ok=True)
        node.write_bytes(b"A regular-file fixture, never a USB device.\n")
        return node

    def fixture_ioctl(self, descriptor, request, transfer):
        self.calls.append((descriptor, request, transfer))
        self.assertEqual(request, USBDEVFS_CONTROL)
        self.assertEqual((transfer.request_type, transfer.request, transfer.value, transfer.index, transfer.length),
                         (0xC0, 3, 0, 0, STATUS_LENGTH))
        self.assertGreaterEqual(transfer.timeout_milliseconds, 1)
        self.assertLessEqual(transfer.timeout_milliseconds, 1000)
        self.assertEqual(ctypes.string_at(transfer.data, STATUS_LENGTH), bytes(STATUS_LENGTH))
        self.assertTrue(stat.S_ISREG(os.fstat(descriptor).st_mode))
        self.assertEqual(fcntl.fcntl(descriptor, fcntl.F_GETFL) & os.O_ACCMODE, os.O_RDWR)
        with self.lock.open("r+") as competing_lock:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(competing_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        ctypes.memmove(transfer.data, self.response, len(self.response))
        return len(self.response)

    def reader(self, **changes):
        settings = {"lock_path": str(self.lock), "sysfs_root": self.sysfs, "device_root": self.devices,
                    "allow_test_backend": True, "ioctl_fixture": self.fixture_ioctl}
        settings.update(changes)
        return ScanButtonTransport(**settings)

    def assert_error(self, code, reader=None, cancel_event=None):
        with self.assertRaises(ScanButtonError) as failure:
            (reader or self.reader()).read_status(cancel_event)
        self.assertEqual(failure.exception.code, code)
        return failure.exception

    def assert_lock_released(self):
        with self.lock.open("r+") as descriptor:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_construction_has_no_device_enumeration_file_open_or_library_loading(self):
        with patch.object(os, "open", side_effect=AssertionError("Unexpected open")), \
                patch.object(os, "scandir", side_effect=AssertionError("Unexpected discovery")), \
                patch.object(Path, "resolve", side_effect=AssertionError("Unexpected metadata read")), \
                patch.object(ctypes, "CDLL", side_effect=AssertionError("Unexpected native loading")):
            ScanButtonTransport()
            self.reader()
        self.assertEqual(self.calls, [])

    def test_exactly_one_input_request_and_original_file_bytes_and_opaque_identity(self):
        original_bytes = self.node.read_bytes()
        reader = self.reader()
        first, second = reader.read_status(), reader.read_status()
        self.assertEqual(first["status"], self.response)
        self.assertEqual(first, second)
        self.assertEqual(len(self.calls), 2)
        self.assertRegex(first["device_identity"], r"^[0-9a-f]{64}$")
        self.assertNotIn(str(self.root), repr(first))
        self.assertEqual(self.node.read_bytes(), original_bytes)
        self.assert_lock_released()
        for descriptor, _, _ in self.calls:
            with self.assertRaises(OSError):
                os.fstat(descriptor)

    def test_empty_short_and_full_responses_are_never_padded_or_parsed_as_events(self):
        reader = self.reader()
        for length in (0, 1, 4, 5, 254, 255):
            with self.subTest(length=length):
                self.response = bytes(range(length))
                self.assertEqual(reader.read_status()["status"], self.response)

    def test_fixtures_require_explicit_isolation_and_callback_without_native_fallback(self):
        for settings in (
            {"sysfs_root": self.sysfs},
            {"device_root": self.devices},
            {"ioctl_fixture": self.fixture_ioctl},
            {"allow_test_backend": True},
            {"allow_test_backend": True, "sysfs_root": self.sysfs, "device_root": self.devices},
            {"allow_test_backend": True, "sysfs_root": "/sys/anything", "device_root": self.devices,
             "ioctl_fixture": self.fixture_ioctl},
            {"allow_test_backend": True, "sysfs_root": self.sysfs, "device_root": "/dev/anything",
             "ioctl_fixture": self.fixture_ioctl},
        ):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                ScanButtonTransport(**settings)

    def test_invalid_timeouts_budgets_serial_numbers_and_ambiguous_paths_are_rejected(self):
        for settings in (
            {"timeout_milliseconds": 0}, {"timeout_milliseconds": 1001},
            {"timeout_milliseconds": True}, {"timeout_milliseconds": 1.5},
            {"total_budget_seconds": float("nan")}, {"total_budget_seconds": float("inf")},
            {"total_budget_seconds": True}, {"total_budget_seconds": 0},
            {"total_budget_seconds": 3}, {"printer_serial": "bad\x00serial"},
            {"printer_serial": 123}, {"lock_path": "relative.lock"},
            {"lock_path": "/tmp/../tmp/lock"},
        ):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                self.reader(**settings)

    def test_exact_model_serial_selection_and_ambiguity(self):
        self.add_device(8, serial="PRINTER-TWO")
        self.assert_error("scan_button_selection_required")
        selected = self.reader(printer_serial="PRINTER-TWO").read_status()
        other = self.reader(printer_serial="PRINTER-ONE").read_status()
        self.assertNotEqual(selected["device_identity"], other["device_identity"])
        self.assert_error("scan_button_not_found", self.reader(printer_serial="UNKNOWN"))
        self.assertEqual(len(self.calls), 2)

    def test_wrong_vendor_or_product_is_never_queried(self):
        for name, value in (("idVendor", "ffff"), ("idProduct", "ffff")):
            attribute = self.sysfs / "devices" / "platform" / "usb1" / "1-7" / name
            previous = attribute.read_text()
            attribute.write_text(value)
            self.assert_error("scan_button_not_found")
            attribute.write_text(previous)
        self.assertEqual(self.calls, [])

    def test_shared_lock_is_nonblocking_and_all_error_paths_release_descriptors(self):
        reader = self.reader()
        before = len(list(Path("/proc/self/fd").iterdir()))
        with self.lock.open("r+") as descriptor, patch.object(reader, "_discover", side_effect=AssertionError("Busy discovery")), \
                patch.object(time, "sleep", side_effect=AssertionError("Blocking lock retry")):
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            for _ in range(10):
                self.assert_error("device_busy", reader)
        self.assertEqual(len(list(Path("/proc/self/fd").iterdir())), before)
        reader.read_status()
        self.assert_lock_released()

    def test_missing_lock_symlink_and_nonregular_lock_are_rejected_without_usb(self):
        self.lock.unlink()
        self.assert_error("scan_button_lock_unavailable")
        target = self.root / "another.lock"
        target.touch()
        self.lock.symlink_to(target)
        self.assert_error("scan_button_lock_unavailable")
        self.lock.unlink()
        os.mkfifo(self.lock)
        self.assert_error("scan_button_lock_unavailable")
        self.assertEqual(self.calls, [])

    def test_cancellation_before_discovery_and_during_request_discards_response(self):
        cancelled = threading.Event()
        cancelled.set()
        reader = self.reader()
        with patch.object(reader, "_discover", side_effect=AssertionError("Cancelled discovery")):
            self.assert_error("operation_cancelled", reader, cancelled)
        cancelled.clear()

        def cancel_during_request(*arguments):
            result = self.fixture_ioctl(*arguments)
            cancelled.set()
            return result

        self.assert_error("operation_cancelled", self.reader(ioctl_fixture=cancel_during_request), cancelled)
        self.assertEqual(len(self.calls), 1)
        self.assert_lock_released()

    def _observe_lock_quiet_interval(self, *, cancel_during_interval=False):
        request_completed = threading.Event()
        cancel_event = threading.Event()
        observations = {}
        errors = []

        def status_response(*arguments):
            result = self.fixture_ioctl(*arguments)
            observations["request_completed_at"] = time.monotonic()
            request_completed.set()
            return result

        reader = self.reader(ioctl_fixture=status_response)

        def read_in_background():
            try:
                observations["result"] = reader.read_status(cancel_event)
            except BaseException as error:
                errors.append(error)

        worker = threading.Thread(target=read_in_background, daemon=True)
        worker.start()
        try:
            self.assertTrue(request_completed.wait(2), "The simulated status request did not complete")
            deadline = time.monotonic() + 2
            with self.lock.open("r+") as competing_operation:
                while True:
                    try:
                        fcntl.flock(competing_operation, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        observations["blocked_attempts"] = observations.get("blocked_attempts", 0) + 1
                        # Request cancellation only after a real competing flock
                        # has observed that the post-request lock is still held.
                        if cancel_during_interval:
                            cancel_event.set()
                        if time.monotonic() >= deadline:
                            self.fail("The status transport did not release its shared lock")
                        time.sleep(0.002)
                    else:
                        observations["competing_operation_started_at"] = time.monotonic()
                        break
        finally:
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertGreater(observations.get("blocked_attempts", 0), 0)
        self.assertGreaterEqual(observations["competing_operation_started_at"] - observations["request_completed_at"], 0.1)
        self.assertEqual(len(self.calls), 1)
        if cancel_during_interval:
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], ScanButtonError)
            self.assertEqual(errors[0].code, "operation_cancelled")
            self.assertNotIn("result", observations)
        else:
            self.assertEqual(errors, [])
            self.assertEqual(observations["result"]["status"], self.response)

    def test_competing_device_operation_waits_for_the_complete_successful_status_quiet_interval(self):
        self._observe_lock_quiet_interval()

    def test_cancellation_during_quiet_interval_does_not_release_the_device_lock_early(self):
        self._observe_lock_quiet_interval(cancel_during_interval=True)

    def test_transfer_timeout_is_finite_and_reduced_to_remaining_total_budget(self):
        reader = self.reader(timeout_milliseconds=1000, total_budget_seconds=0.2)
        reader.read_status()
        self.assertGreater(self.calls[0][2].timeout_milliseconds, 0)
        self.assertLessEqual(self.calls[0][2].timeout_milliseconds, 200)

    def test_elapsed_budget_never_retries_and_never_returns_a_late_response(self):
        reader = self.reader(total_budget_seconds=0.01)

        def late_response(*arguments):
            result = self.fixture_ioctl(*arguments)
            time.sleep(0.03)
            return result

        reader._control_request = late_response
        self.assert_error("scan_button_timeout", reader)
        self.assertEqual(len(self.calls), 1)
        self.assert_lock_released()

    def test_invalid_returned_lengths_are_rejected_instead_of_slicing_arbitrary_data(self):
        for returned_length in (-1, 256, None, True, b"five", 5.0):
            with self.subTest(length=returned_length):
                def invalid_length(*arguments):
                    self.fixture_ioctl(*arguments)
                    return returned_length
                self.assert_error("scan_button_invalid_response", self.reader(ioctl_fixture=invalid_length))
                self.assert_lock_released()

    def test_native_errors_have_stable_codes_and_no_reset_or_retry(self):
        for error_number, expected_code in (
            (errno.ETIMEDOUT, "scan_button_timeout"), (errno.EACCES, "scan_button_access_denied"),
            (errno.EPERM, "scan_button_access_denied"), (errno.EBUSY, "device_busy"),
            (errno.ENOTTY, "scan_button_status_failed"), (errno.EPIPE, "scan_button_status_failed"),
            (errno.EINTR, "scan_button_status_failed"), (errno.ENODEV, "scan_button_not_found"),
        ):
            with self.subTest(error=error_number):
                calls_before = len(self.calls)
                def failed_request(*arguments):
                    self.fixture_ioctl(*arguments)
                    raise OSError(error_number, "Simulated ioctl error")
                with patch.object(transport_module.ScanButtonTransport, "_wait_after_status",
                                  side_effect=AssertionError("A failed ioctl must not invent a quiet interval")):
                    self.assert_error(expected_code, self.reader(ioctl_fixture=failed_request))
                self.assertEqual(len(self.calls), calls_before + 1)
                self.assert_lock_released()

    def test_usb_node_links_and_links_in_parent_directories_are_rejected(self):
        target = self.root / "different-regular-file"
        target.touch()
        self.node.unlink()
        self.node.symlink_to(target)
        self.assert_error("scan_button_access_denied")
        self.node.unlink()
        self.node.touch()
        directory = self.devices / "bus" / "usb"
        saved = directory.with_name("saved-usb")
        directory.rename(saved)
        directory.symlink_to(saved)
        self.assert_error("scan_button_not_found")
        self.assertEqual(self.calls, [])

    def test_sysfs_attribute_links_registry_escapes_and_mismatched_character_registry_are_rejected(self):
        device = self.sysfs / "devices" / "platform" / "usb1" / "1-7"
        attribute = device / "serial"
        attribute.unlink()
        attribute.symlink_to(self.node)
        self.assert_error("scan_button_access_denied")
        attribute.unlink()
        attribute.write_text("PRINTER-ONE")
        character_entry = self.sysfs / "dev" / "char" / "189:6"
        character_entry.unlink()
        character_entry.symlink_to(self.root)
        self.assert_error("scan_button_access_denied")
        character_entry.unlink()
        character_entry.symlink_to(device)
        entry = self.registry / "1-7"
        entry.unlink()
        entry.symlink_to(self.root)
        self.assert_error("scan_button_access_denied")
        self.assertEqual(self.calls, [])

    def test_real_character_device_type_and_address_mapping_are_required_in_production(self):
        reader = self.reader()
        # Exercise the production type check on a safely isolated regular file;
        # the injected fixture remains installed and real devices stay unused.
        reader.allow_test_backend = False
        self.assert_error("scan_button_access_denied", reader)
        self.assertEqual(self.calls, [])
        reader.allow_test_backend = True
        attribute = self.sysfs / "devices" / "platform" / "usb1" / "1-7" / "dev"
        for invalid in ("1:3", "189:99", "not-a-number"):
            attribute.write_text(invalid)
            self.assert_error("scan_button_status_failed", reader)
        self.assertEqual(self.calls, [])

    def test_replacement_between_discovery_and_open_is_rejected_before_ioctl(self):
        original_open = transport_module._open_without_symlinks

        def replace_node_before_open(path, flags):
            if path == self.node and flags == os.O_RDWR:
                replacement = self.node.with_name("replacement")
                replacement.write_bytes(b"Replacement device fixture")
                replacement.replace(self.node)
            return original_open(path, flags)

        with patch.object(transport_module, "_open_without_symlinks", side_effect=replace_node_before_open):
            self.assert_error("scan_button_device_changed")
        self.assertEqual(self.calls, [])
        self.assert_lock_released()

    def test_replacement_or_removal_during_request_discards_all_response_bytes(self):
        def replace_node(*arguments):
            result = self.fixture_ioctl(*arguments)
            replacement = self.node.with_name("replacement")
            replacement.write_bytes(b"Replacement device fixture")
            replacement.replace(self.node)
            return result

        self.assert_error("scan_button_device_changed", self.reader(ioctl_fixture=replace_node))
        self.assert_lock_released()

        def remove_device(*arguments):
            result = self.fixture_ioctl(*arguments)
            (self.registry / "1-7").unlink()
            return result

        self.assert_error("scan_button_device_changed", self.reader(ioctl_fixture=remove_device))
        self.assertEqual(len(self.calls), 2)
        self.assert_lock_released()

    def test_connection_identity_changes_when_the_same_usb_address_is_reused(self):
        reader = self.reader()
        original_identity = reader.read_status()["device_identity"]
        device = self.sysfs / "devices" / "platform" / "usb1" / "1-7"
        device.rename(device.with_name("disconnected-device"))
        (self.registry / "1-7").unlink()
        (self.sysfs / "dev" / "char" / "189:6").unlink()
        self.add_device()
        self.assertNotEqual(reader.read_status()["device_identity"], original_identity)


if __name__ == "__main__":
    unittest.main()

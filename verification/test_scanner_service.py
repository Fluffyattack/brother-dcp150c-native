# SPDX-License-Identifier: GPL-2.0-or-later
"""Exercise real SANE with its isolated virtual backend, never a USB backend."""

import fcntl
import os
from pathlib import Path
import shutil
import tempfile
import threading
import unittest

from dcp150c.scanner_service import ScannerError, ScannerService, parse_scanner_options


class ScannerOptionTests(unittest.TestCase):
    def test_only_advertised_supported_modes_and_resolutions_are_offered(self):
        description = """  --mode Black & White|True Gray|24bit Color [24bit Color]
  --resolution 100|200|300|600|1200dpi [300]
  -x 0..215.9mm [210]
  -y 0..297mm [297]
"""
        result = parse_scanner_options(description)
        self.assertEqual(result["modes"], ["color", "gray", "lineart"])
        self.assertEqual(result["resolutions"], [100, 200, 300, 600])
        self.assertEqual(result["area"], {"width": 215.9, "height": 297})

    def test_missing_options_do_not_imply_scanner_support(self):
        result = parse_scanner_options("No scanner connection is available.")
        self.assertEqual(result["modes"], [])
        self.assertEqual(result["resolutions"], [])
        self.assertEqual(result["area"], {})


def virtual_scanner_available():
    return bool(shutil.which("scanimage") and (list(Path("/usr/lib").glob("*/sane/libsane-test.so.1"))
                                              or Path("/usr/lib/sane/libsane-test.so.1").exists()))


@unittest.skipUnless(virtual_scanner_available(), "Install sane-utils and the SANE test backend for scanner integration checks")
class VirtualScannerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="dcp150c-virtual-scanner-")
        self.directory = Path(self.temporary.name)
        (self.directory / "dll.conf").write_text("test\n", encoding="ascii")
        (self.directory / "test.conf").write_text("number_of_devices 1\n", encoding="ascii")
        self.lock = self.directory / "device.lock"
        self.lock.touch()
        self.previous_configuration = os.environ.get("SANE_CONFIG_DIR")
        os.environ["SANE_CONFIG_DIR"] = str(self.directory)
        self.scanner = ScannerService(lock_path=str(self.lock), device="test:0", allow_test_backend=True)

    def tearDown(self):
        if self.previous_configuration is None:
            os.environ.pop("SANE_CONFIG_DIR", None)
        else:
            os.environ["SANE_CONFIG_DIR"] = self.previous_configuration
        self.temporary.cleanup()

    def test_cached_status_does_not_probe_the_scanner(self):
        self.assertFalse(self.scanner.capabilities()["available"])
        self.assertIsNone(self.scanner.capabilities()["probed_at"])

    def test_real_virtual_scan_produces_a_png_without_hardware_verification(self):
        from PIL import Image
        self.assertTrue(self.scanner.probe()["available"])
        destination = self.directory / "scan.png"
        result = self.scanner.scan(destination, {"mode": "color", "resolution": 100,
                                                "area": {"width": 40, "height": 40}}, threading.Event())
        with Image.open(destination) as image:
            self.assertEqual(image.format, "PNG")
            self.assertGreater(image.width, 100)
            image.verify()
        self.assertFalse(result["hardware_verified"])
        self.assertFalse(self.scanner.capabilities()["hardware_verified"])

    def test_production_configuration_rejects_virtual_or_unrelated_devices(self):
        scanner = ScannerService(lock_path=str(self.lock))
        self.assertFalse(scanner.probe()["available"])

    def test_invalid_geometry_and_resolution_are_rejected_before_scanning(self):
        self.scanner.probe()
        for settings in ({"resolution": 99999}, {"area": {"width": 999}}, {"area": {"width": float("nan")}}):
            destination = self.directory / "rejected.png"
            with self.assertRaises(ScannerError) as raised:
                self.scanner.scan(destination, settings, threading.Event())
            self.assertEqual(raised.exception.code, "invalid_scan_settings")
            self.assertFalse(destination.exists())

    def test_waiting_for_the_shared_printer_lock_can_be_cancelled(self):
        with self.lock.open("r+") as descriptor:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            cancel_event = threading.Event()
            timer = threading.Timer(0.2, cancel_event.set)
            timer.start()
            try:
                with self.assertRaises(ScannerError) as raised:
                    self.scanner._lock_device(cancel_event)
                self.assertEqual(raised.exception.code, "operation_cancelled")
            finally:
                timer.join()


if __name__ == "__main__":
    unittest.main()

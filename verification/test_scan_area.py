# SPDX-License-Identifier: GPL-2.0-or-later
"""Check preview geometry against actual isolated SANE output, without hardware."""

import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from PIL import Image

from dcp150c.control_center import ControlCenter
from dcp150c.document_storage import DocumentStorage
from dcp150c.scanner_service import ScannerService
from verification.test_scanner_service import virtual_scanner_available


class ScanMetadataStorage(unittest.TestCase):
    def test_scan_metadata_survives_restart_and_preserves_older_document_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "image.png"
            Image.new("RGB", (20, 30), "white").save(image)
            storage = DocumentStorage(root / "state")
            metadata = {"kind": "preview", "resolution": 75, "device": "test:0", "width": 20, "height": 30,
                        "area": {"left": 0, "top": 0, "width": 200, "height": 300}, "hardware_verified": False}
            document = storage.add(image, "Saved preview", {"media_type": "image/png", "page_count": 1}, scan_metadata=metadata)
            self.assertEqual(len(storage.connection.execute("PRAGMA table_info(documents)").fetchall()), 10)
            self.assertEqual(len(storage.connection.execute("SELECT * FROM documents").fetchone()), 10)
            storage.close()
            storage = DocumentStorage(root / "state")
            self.assertEqual(storage.get(document["id"])["scan"], metadata)
            self.assertEqual(storage.list()[0]["scan"], metadata)
            storage.delete(document["id"])
            self.assertEqual(storage.connection.execute("SELECT count(*) FROM scan_document_metadata").fetchone()[0], 0)
            storage.close()


@unittest.skipUnless(virtual_scanner_available(), "Install sane-utils and its isolated test backend")
class PreviewAcquisition(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        configuration = self.root / "sane"
        configuration.mkdir()
        (configuration / "dll.conf").write_text("test\n")
        (configuration / "test.conf").write_text("number_of_devices 1\n")
        self.environment = patch.dict("os.environ", {"SANE_CONFIG_DIR": str(configuration)})
        self.environment.start()
        self.center = ControlCenter(self.root / "state", self.root / "password.json")
        lock = self.root / "device.lock"
        lock.touch()
        self.scanner = ScannerService(lock_path=str(lock), device="test:0", allow_test_backend=True)
        self.center.scanner = self.scanner
        self.capabilities = self.scanner.probe()
        self.assertTrue(self.capabilities["available"])
        self.scan_commands = []
        original = self.scanner._run
        def record(arguments, *args, **kwargs):
            if "--format=png" in arguments: self.scan_commands.append(arguments)
            return original(arguments, *args, **kwargs)
        self.scanner._run = record

    def tearDown(self):
        self.center.close()
        self.environment.stop()
        self.temporary.cleanup()

    def wait(self, identifier):
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            job = self.center.jobs.get(identifier)
            if job["state"] not in {"queued", "running"}: return job
            time.sleep(0.05)
        self.fail("The virtual scanner operation exceeded its deadline")

    def test_preview_reads_whole_glass_at_lowest_supported_resolution(self):
        job = self.center.preview_scan({"mode": "color"}, "whole-glass-preview-001")
        result = self.wait(job["id"])
        self.assertEqual(result["state"], "succeeded", result)
        document = self.center.storage.get(result["result_document_id"])
        scan = document["scan"]
        self.assertEqual(scan["kind"], "preview")
        self.assertEqual(scan["resolution"], min(self.capabilities["resolutions"]))
        self.assertEqual(scan["area"], {"left": 0, "top": 0, **self.capabilities["area"]})
        self.assertEqual(scan["device"], "test:0")
        self.assertFalse(scan["hardware_verified"])
        with Image.open(self.center.storage.path(document["id"])) as image:
            self.assertEqual(image.size, (scan["width"], scan["height"]))
        self.assertTrue(document["has_preview"])

    def test_lost_preview_response_reuses_accepted_job_even_after_device_becomes_unavailable(self):
        values = {"mode": "color"}
        job = self.center.preview_scan(values, "repeated-preview-001")
        self.assertEqual(self.wait(job["id"])["state"], "succeeded")
        self.scanner._set_capabilities({"available": False})
        self.assertEqual(self.center.preview_scan(values, "repeated-preview-001")["id"], job["id"])
        self.assertEqual(len(self.scan_commands), 1)

    def test_selected_physical_area_changes_scanner_command_and_real_pixel_dimensions(self):
        area = {"left": 10.5, "top": 20.5, "width": 32, "height": 48}
        job = self.center.scan({"mode": "color", "resolution": 100, "output_format": "png", "area": area}, "physical-region-001")
        result = self.wait(job["id"])
        self.assertEqual(result["state"], "succeeded", result)
        document = self.center.storage.get(result["result_document_id"])
        self.assertEqual(document["scan"]["area"], area)
        command = self.scan_commands[0]
        for option, expected in (("-l", 10.5), ("-t", 20.5), ("-x", 32), ("-y", 48)):
            self.assertEqual(float(command[command.index(option) + 1]), expected)
        with Image.open(self.center.storage.path(document["id"])) as image:
            self.assertLessEqual(abs(image.width - area["width"] * 100 / 25.4), 2)
            self.assertLessEqual(abs(image.height - area["height"] * 100 / 25.4), 2)

    def test_scan_profile_keeps_region_through_export_and_rejects_a_smaller_glass(self):
        area = {"left": 10, "top": 20, "width": 80, "height": 100}
        profile = self.center.save_profile({"name": "Document detail", "kind": "scan", "settings": {
            "mode": "color", "resolution": 100, "output_format": "png", "area": area}})
        exported = self.center.export_settings()
        saved = next(value for value in exported["profiles"] if value["name"] == profile["name"])
        self.assertEqual(saved["settings"]["area"], area)
        self.center.import_settings(exported)
        restored = [value for value in self.center.storage.profiles() if value["name"] == profile["name"]]
        self.assertEqual(len(restored), 2)
        self.assertTrue(all(value["settings"]["area"] == area for value in restored))
        self.scanner._set_capabilities({"area": {"width": 50, "height": 50}})
        job = self.center.scan(saved["settings"], "changed-glass-001")
        result = self.wait(job["id"])
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["message_code"], "invalid_scan_settings")
        self.assertEqual(self.scan_commands, [])

    def test_copy_uses_selected_scan_region_before_the_recording_print_transport(self):
        area = {"left": 5, "top": 10, "width": 40, "height": 60}
        recorded = []
        def record_print(path, settings, cancel_event=None, on_submitted=None):
            self.assertTrue(Path(path).read_bytes().startswith(b"%PDF-"))
            recorded.append(settings)
            result = {"job_id": "DCP150C-Native-900001"}
            if on_submitted: on_submitted(result)
            return result
        self.center.printing.submit = record_print
        self.center.printing.job_status = lambda identifier: {"job_id": identifier, "state": 9, "reason_codes": []}
        job = self.center.copy({"mode": "color", "resolution": 100, "area": area, "copies": 2, "image_fit": "fill"}, "region-copy-001")
        result = self.wait(job["id"])
        self.assertIn(result["state"], {"submitted", "succeeded"}, result)
        self.assertEqual(len(recorded), 1)
        self.assertEqual((recorded[0]["copies"], recorded[0]["fit"]), (2, "fill"))
        document = self.center.storage.get(result["result_document_id"])
        self.assertEqual(document["scan"]["area"], area)


if __name__ == "__main__": unittest.main()

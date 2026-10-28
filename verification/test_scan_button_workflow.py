# SPDX-License-Identifier: GPL-2.0-or-later
"""Constructed button frames drive real HTTP, document and isolated SANE jobs.

Frames are built from the observed parser fields, not recorded device traffic.
There are no physical USB, printer or scanner operations in these checks.
"""

from pathlib import Path
import threading
import unittest
from unittest.mock import patch

from pypdf import PdfReader

from dcp150c.document_storage import StorageError
from dcp150c.scan_button_service import ScanButtonService
from dcp150c.scanner_service import ScannerService
from verification import test_control_center as control_center_checks
from verification.test_scan_sessions import GeneratedScanner
from verification.test_scanner_service import virtual_scanner_available


class ConstructedButtonTransport:
    def __init__(self):
        self.frame = b"\x00\x10\x03\x00\x00"
        self.calls = 0

    def read_status(self, cancel_event):
        self.calls += 1
        return {"status": self.frame, "device_identity": "generated-button-connection"}


class ScanButtonHTTPWorkflow(unittest.TestCase):
    setUpClass = classmethod(control_center_checks.ControlCenterHTTPTests.setUpClass.__func__)
    tearDownClass = classmethod(control_center_checks.ControlCenterHTTPTests.tearDownClass.__func__)
    request = control_center_checks.ControlCenterHTTPTests.request
    login = control_center_checks.ControlCenterHTTPTests.login
    wait_job = control_center_checks.ControlCenterHTTPTests.wait_job

    def setUp(self):
        self.cookie = self.csrf = None
        self.center.scan_button.close()
        self.transport = ConstructedButtonTransport()
        self.center.scan_button = ScanButtonService(self.center.storage, self.center.jobs,
            self.center._resolve_scan_button_binding, self.center._perform_scan_button_action,
            transport=self.transport, start_worker=False)
        state = self.center.scan_button.snapshot()["configuration"]
        self.center.scan_button.configure({"revision": state["revision"], "enabled": False, "bindings": {}})
        self.scanner = GeneratedScanner()
        self.center.scanner = self.scanner
        self.initial_documents = {document["id"] for document in self.center.storage.list()}
        self.scan_profile = self.center.save_profile({"name": "Button document", "kind": "scan",
            "settings": {"mode": "color", "resolution": 100, "output_format": "png"}})
        self.ocr_profile = self.center.save_profile({"name": "Button text", "kind": "ocr", "settings": {"languages": "eng"}})

    def tearDown(self):
        state = self.center.scan_button.snapshot()["configuration"]
        self.center.scan_button.configure({"revision": state["revision"], "enabled": False, "bindings": {}})
        for document in self.center.storage.list():
            if document["id"] not in self.initial_documents:
                self.center.storage.delete(document["id"])
        for profile in (self.scan_profile, self.ocr_profile):
            self.center.storage.delete_profile(profile["id"])

    def save(self, action, *, enabled=True):
        binding = {"scan_profile_id": self.scan_profile["id"]}
        if action == "ocr":
            binding["ocr_profile_id"] = self.ocr_profile["id"]
        values = {"revision": self.center.scan_button.snapshot()["configuration"]["revision"],
                  "enabled": enabled, "bindings": {action: binding}}
        status, result, _ = self.request("POST", "/api/scan-button/settings", values)
        self.assertEqual(status, 200, result)
        return result["scan_button"]

    def baseline(self):
        self.transport.frame = b"\x00\x10\x03\x00\x00"
        self.assertEqual(self.center.scan_button.poll_once()["status_code"], "scan_button_waiting")
        self.assertEqual(self.center.scan_button.poll_once()["status_code"], "scan_button_ready")

    def press(self, action_number):
        self.transport.frame = bytes((0, 0x10, 3, 0x20, action_number))
        return self.center.scan_button.poll_once()

    def test_protected_configuration_and_cached_state_never_read_usb(self):
        path = "/api/scan-button/settings"
        values = {"revision": 0, "enabled": False, "bindings": {}}
        self.assertEqual(self.request("POST", path, values)[0], 401)
        self.login()
        self.assertEqual(self.request("POST", path, values, {"X-CSRF-Token": "wrong"})[0], 403)
        self.assertEqual(self.request("POST", path, values, {"Origin": "https://unrelated.invalid"})[0], 403)
        self.assertEqual(self.request("POST", path, {"enabled": True})[0], 400)
        status, snapshot, _ = self.request("GET", "/api/control-center")
        self.assertEqual(status, 200)
        self.assertFalse(snapshot["scan_button"]["configuration"]["enabled"])
        self.assertFalse(snapshot["scan_button"]["hardware_verified"])
        self.save("file")
        self.assertEqual(self.transport.calls, 0)
        self.assertEqual(self.scanner.calls, 0)

    def test_four_button_actions_keep_originals_and_ocr_produces_searchable_text(self):
        self.login()
        with patch.object(self.center.printing, "submit", side_effect=AssertionError("Printing is forbidden")):
            for number, action in ((3, "image"), (5, "file"), (8, "email"), (2, "ocr")):
                self.save(action)
                self.baseline()
                accepted = self.press(number)
                job = self.wait_job(accepted["last_job_id"])
                self.assertEqual((job["kind"], job["state"]), ("scan_button", "succeeded"), job)
                self.assertIsNone(job["cups_job_id"])
                result = self.center.storage.get(job["result_document_id"])
                self.assertTrue(result["has_preview"])
                self.assertEqual(self.request("GET", f"/api/documents/{result['id']}/file")[0], 200)
                original = self.center.storage.get(job["result"]["scan_document_id"])
                self.assertEqual(original["media_type"], "image/png")
                if action == "ocr":
                    self.assertTrue(result["has_text"])
                    text = self.request("GET", f"/api/documents/{result['id']}/text")[1].decode()
                    self.assertIn("NATIVE PAGE", text.upper())
                    pdf = PdfReader(str(self.center.storage.path(result["id"])))
                    self.assertIn("NATIVE PAGE", pdf.pages[0].extract_text().upper())
        self.assertEqual(self.scanner.calls, 4)

    def test_repeated_event_needs_a_new_baseline_before_another_capture(self):
        self.login()
        self.save("file")
        self.press(5)  # A pre-existing event is not a newly authorised press.
        self.assertEqual(self.scanner.calls, 0)
        self.baseline()
        job = self.wait_job(self.press(5)["last_job_id"])
        self.assertEqual(job["state"], "succeeded")
        for _ in range(4):
            self.press(5)
        self.assertEqual(self.scanner.calls, 1)
        self.baseline()
        second = self.wait_job(self.press(5)["last_job_id"])
        self.assertEqual(second["state"], "succeeded")
        self.assertNotEqual(job["id"], second["id"])
        self.assertEqual(self.scanner.calls, 2)

    def test_removed_profile_refuses_capture_and_can_still_be_disabled(self):
        self.login()
        self.save("file")
        self.center.storage.delete_profile(self.scan_profile["id"])
        self.baseline()
        self.assertEqual(self.press(5)["status_code"], "invalid_profile")
        self.assertEqual(self.scanner.calls, 0)
        state = self.center.scan_button.snapshot()["configuration"]
        status, result, _ = self.request("POST", "/api/scan-button/settings", {**state, "enabled": False})
        self.assertEqual(status, 200, result)
        self.assertFalse(result["scan_button"]["configuration"]["enabled"])

    def test_cancellation_before_save_keeps_no_partial_document(self):
        self.login()
        self.save("file")
        self.baseline()
        original_scan = self.scanner.scan
        def capture_then_cancel(path, settings, event):
            result = original_scan(path, settings, event)
            event.set()
            return result
        with patch.object(self.scanner, "scan", side_effect=capture_then_cancel):
            job = self.wait_job(self.press(5)["last_job_id"])
        self.assertEqual((job["state"], job["message_code"]), ("cancelled", "job_cancelled"))
        self.assertIsNone(job["result_document_id"])
        self.assertEqual({document["id"] for document in self.center.storage.list()}, self.initial_documents)

    def test_later_ocr_cancellation_keeps_original_and_does_not_repeat_capture(self):
        self.login()
        self.save("ocr")
        self.baseline()
        with patch.object(self.center.ocr, "process", side_effect=StorageError("job_cancelled", "Generated cancellation")):
            job = self.wait_job(self.press(2)["last_job_id"])
        self.assertEqual(job["state"], "cancelled", job)
        self.assertIsNotNone(job["result_document_id"])
        self.assertEqual(job["result_document_id"], job["result"]["scan_document_id"])
        self.assertEqual(self.request("GET", f"/api/documents/{job['result_document_id']}/file")[0], 200)
        self.press(2)
        self.assertEqual(self.scanner.calls, 1)

    @unittest.skipUnless(virtual_scanner_available(), "Install the isolated SANE test backend")
    def test_button_request_connects_isolated_sane_then_captures_actual_pixels(self):
        self.login()
        configuration = self.root / "button-sane"
        configuration.mkdir(exist_ok=True)
        (configuration / "dll.conf").write_text("test\n")
        (configuration / "test.conf").write_text("number_of_devices 1\n")
        lock = self.root / "button-sane.lock"
        lock.touch()
        self.center.scanner = ScannerService(lock_path=str(lock), device="test:0", allow_test_backend=True)
        self.center.save_profile({**self.scan_profile, "settings": {**self.scan_profile["settings"],
            "area": {"left": 2, "top": 3, "width": 35, "height": 50}}})
        with patch.dict("os.environ", {"SANE_CONFIG_DIR": str(configuration)}):
            self.save("file")
            self.assertFalse(self.center.scanner.capabilities()["available"])
            self.baseline()
            job = self.wait_job(self.press(5)["last_job_id"])
        self.assertEqual(job["state"], "succeeded", job)
        document = self.center.storage.get(job["result_document_id"])
        self.assertEqual(document["scan"]["kind"], "scan_button")
        self.assertEqual(document["scan"]["area"], {"left": 2, "top": 3, "width": 35, "height": 50})
        self.assertFalse(document["scan"]["hardware_verified"])
        from PIL import Image
        with Image.open(self.center.storage.path(document["id"])) as image:
            self.assertLessEqual(abs(image.width - 35 * 100 / 25.4), 2)
            self.assertLessEqual(abs(image.height - 50 * 100 / 25.4), 2)


if __name__ == "__main__":
    unittest.main()

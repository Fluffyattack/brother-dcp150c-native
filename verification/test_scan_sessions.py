# SPDX-License-Identifier: GPL-2.0-or-later
"""Prove durable flatbed workflows with generated images and isolated SANE test data."""

import hashlib
import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw, ImageFont
from pypdf import PdfReader

from dcp150c.access_control import save_password
from dcp150c.command_runner import CommandError
from dcp150c.control_center import ControlCenter
from dcp150c.document_storage import StorageError
from dcp150c.scanner_service import ScannerService
from dcp150c.server import DashboardServer, unavailable_snapshot
from verification.test_scanner_service import virtual_scanner_available


class GeneratedScanner:
    """Explicit generated originals, with no USB or CUPS operations available."""
    def __init__(self):
        self.calls = 0
        self.gate = None
        self.started = threading.Event()

    def capabilities(self):
        return {"available": True, "configured": True, "modes": ["color", "gray"],
                "resolutions": [100, 300], "area": {"width": 210, "height": 297},
                "probed_at": None, "hardware_verified": False}

    def scan(self, output, settings, cancel_event):
        self.calls += 1
        self.started.set()
        while self.gate and not self.gate.wait(0.02):
            if cancel_event.is_set():
                raise CommandError("job_cancelled", "Cancelled")
        image = Image.new("RGB", (1000, 1400), ("#df342f", "#2958df", "#26a560")[(self.calls - 1) % 3])
        draw = ImageDraw.Draw(image)
        draw.rectangle((40, 100, 960, 420), fill="white")
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 58)
        draw.text((70, 170), "NATIVE PAGE " + str(self.calls), font=font, fill="black")
        image.save(output, "PNG")
        return {"hardware_verified": False}


class ScanSessionWorkflows(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.center = ControlCenter(self.root / "state", self.root / "password.json")
        self.scanner = GeneratedScanner()
        self.center.scanner = self.scanner

    def tearDown(self):
        self.center.close()
        self.temporary.cleanup()

    def create(self, key=None):
        return self.center.scan_sessions.create({"name": "Offline multipage example", "settings": {"mode": "color", "resolution": 300}}, key)

    def wait_job(self, identifier):
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            job = self.center.jobs.get(identifier)
            if job["state"] not in {"queued", "running"}:
                return job
            time.sleep(0.05)
        self.fail("Background workflow exceeded the test deadline")

    def page(self, session, key=None):
        job = self.center.scan_sessions.capture(session["id"], {"revision": session["revision"]}, key)
        completed = self.wait_job(job["id"])
        self.assertEqual(completed["state"], "succeeded", completed)
        return self.center.scan_sessions.get(session["id"]), completed

    def test_next_page_reorder_crop_and_real_pdf_preserve_originals(self):
        session, _ = self.page(self.create())
        session, _ = self.page(session)
        original_hashes = {page["document_id"]: hashlib.sha256(self.center.storage.path(page["document_id"]).read_bytes()).hexdigest() for page in session["pages"]}
        first, second = session["pages"]
        session = self.center.scan_sessions.update(session["id"], {"revision": session["revision"], "pages": [
            {**second, "rotation": 90, "crop": {"left": 0.1, "top": 0.1, "right": 0.9, "bottom": 0.9}}, first]})
        self.assertAlmostEqual(session["pages"][0]["crop"]["width"], 0.8)
        job = self.center.scan_sessions.finish(session["id"], {"revision": session["revision"]}, "finish-example-001")
        completed = self.wait_job(job["id"])
        self.assertEqual(completed["state"], "succeeded", completed)
        result = self.center.storage.path(completed["result_document_id"])
        self.assertEqual(len(PdfReader(str(result)).pages), 2)
        for index, expected in ((1, "blue"), (2, "red")):
            preview = self.root / ("preview-" + str(index) + ".png")
            self.center.processor.preview(result, preview, page=index)
            with Image.open(preview) as image:
                red, green, blue = image.convert("RGB").getpixel((image.width // 2, image.height * 2 // 3))
                self.assertTrue(blue > red + 50 if expected == "blue" else red > blue + 50)
        for identifier, digest in original_hashes.items():
            self.assertEqual(hashlib.sha256(self.center.storage.path(identifier).read_bytes()).hexdigest(), digest)
        self.assertEqual(self.center.scan_sessions.get(session["id"])["state"], "completed")

    def test_accepted_page_and_finish_requests_are_not_repeated(self):
        session = self.create("create-example-001")
        self.assertEqual(self.create("create-example-001")["id"], session["id"])
        revision = session["revision"]
        session, completed = self.page(session, "capture-example-001")
        repeated = self.center.scan_sessions.capture(session["id"], {"revision": revision}, "capture-example-001")
        self.assertEqual(repeated["id"], completed["id"])
        self.assertEqual(self.scanner.calls, 1)
        values = {"revision": session["revision"]}
        job = self.center.scan_sessions.finish(session["id"], values, "finish-example-002")
        self.assertEqual(self.wait_job(job["id"])["state"], "succeeded")
        self.assertEqual(self.center.scan_sessions.finish(session["id"], values, "finish-example-002")["id"], job["id"])

    def test_empty_or_stale_edits_leave_no_reserved_work(self):
        session = self.create()
        before = len(self.center.jobs.list())
        with self.assertRaisesRegex(StorageError, "at least one"):
            self.center.scan_sessions.finish(session["id"], {"revision": 0}, "empty-finish-001")
        self.assertEqual(len(self.center.jobs.list()), before)
        self.assertIsNone(self.center.scan_sessions.get(session["id"])["active_job_id"])
        updated = self.center.scan_sessions.update(session["id"], {"revision": 0, "name": "Updated title"})
        with self.assertRaisesRegex(StorageError, "another window"):
            self.center.scan_sessions.capture(session["id"], {"revision": 0}, "stale-page-001")
        self.assertEqual(len(self.center.jobs.list()), before)
        self.assertEqual(self.scanner.calls, 0)
        self.assertEqual(self.center.scan_sessions.get(session["id"])["revision"], updated["revision"])

    def test_two_windows_cannot_start_two_scans_for_the_same_revision(self):
        session = self.create()
        self.scanner.gate = threading.Event()
        barrier, accepted, rejected = threading.Barrier(2), [], []
        def start(index):
            barrier.wait()
            try:
                accepted.append(self.center.scan_sessions.capture(session["id"], {"revision": 0}, "simultaneous-page-00" + str(index)))
            except StorageError as error:
                rejected.append(error.code)
        workers = [threading.Thread(target=start, args=(index,)) for index in range(2)]
        for worker in workers: worker.start()
        for worker in workers: worker.join(5)
        self.assertEqual((len(accepted), len(rejected)), (1, 1))
        self.scanner.gate.set()
        self.assertEqual(self.wait_job(accepted[0]["id"])["state"], "succeeded")
        self.assertEqual(self.scanner.calls, 1)

    def test_originals_survive_expiry_until_the_session_releases_them(self):
        session, _ = self.page(self.create())
        identifier = session["pages"][0]["document_id"]
        for job in self.center.jobs.list():
            if job["document_id"] == identifier and job["kind"] == "preview":
                self.wait_job(job["id"])
        with self.center.storage.guard, self.center.storage.connection:
            self.center.storage.connection.execute("UPDATE documents SET expires_at=0 WHERE id=?", (identifier,))
        self.center.storage.prune()
        self.assertTrue(self.center.storage.path(identifier).exists())
        with self.assertRaisesRegex(StorageError, "scanning document"):
            self.center.storage.delete(identifier)
        self.center.scan_sessions.delete(session["id"])
        self.center.storage.prune()
        with self.assertRaises(StorageError): self.center.storage.get(identifier)

    def test_page_limit_rejects_the_next_capture_before_device_work(self):
        import uuid
        session = self.create()
        source = self.root / "tiny.png"
        Image.new("RGB", (5, 5), "white").save(source)
        pages = []
        for index in range(25):
            document = self.center.storage.add(source, str(index + 1), {"media_type": "image/png", "page_count": 1})
            pages.append({"id": uuid.uuid4().hex, "document_id": document["id"], "rotation": 0, "crop": None})
        with self.center.storage.guard, self.center.storage.connection:
            self.center.storage.connection.execute("UPDATE scan_sessions SET pages=? WHERE id=?", (json.dumps(pages), session["id"]))
        with self.assertRaisesRegex(StorageError, "25 pages"):
            self.center.scan_sessions.capture(session["id"], {"revision": 0}, "too-many-pages-001")
        self.assertEqual(self.scanner.calls, 0)
        self.assertEqual(self.center.scan_sessions.get(session["id"])["page_count"], 25)
        self.assertEqual(self.center.jobs.list(), [])

    def test_removing_a_page_does_not_delete_the_original(self):
        session, _ = self.page(self.create())
        original = session["pages"][0]["document_id"]
        session = self.center.scan_sessions.update(session["id"], {"revision": session["revision"], "pages": []})
        self.assertEqual(session["page_count"], 0)
        self.assertTrue(self.center.storage.path(original).is_file())

    def test_queued_cancel_releases_session_without_starting_a_scan(self):
        entered, release = threading.Event(), threading.Event()
        def block(event, identifier):
            entered.set()
            while not release.wait(0.02):
                if event.is_set(): raise CommandError("job_cancelled", "Cancelled")
        blocking = self.center.jobs.submit("offline-test", {}, block)
        self.assertTrue(entered.wait(3))
        session = self.create()
        job = self.center.scan_sessions.capture(session["id"], {"revision": 0}, "queued-cancel-001")
        self.assertEqual(self.center.cancel(job["id"])["state"], "cancelled")
        self.assertIsNone(self.center.scan_sessions.get(session["id"])["active_job_id"])
        release.set()
        self.wait_job(blocking["id"])
        self.assertEqual(self.scanner.calls, 0)

    def test_running_capture_cancel_keeps_the_document_editable(self):
        session = self.create()
        self.scanner.gate = threading.Event()
        job = self.center.scan_sessions.capture(session["id"], {"revision": 0}, "running-cancel-001")
        self.assertTrue(self.scanner.started.wait(3))
        self.center.cancel(job["id"])
        self.assertEqual(self.wait_job(job["id"])["state"], "cancelled")
        current = self.center.scan_sessions.get(session["id"])
        self.assertEqual((current["state"], current["page_count"], current["active_job_id"]), ("open", 0, None))

    def test_restart_recovers_interrupted_work_without_replaying_it(self):
        session = self.create()
        with self.center.storage.guard, self.center.storage.connection:
            now = time.time()
            self.center.storage.connection.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?)", (
                "a" * 32, "scan_page", "running", now, now, json.dumps({"scan_session_id": session["id"]}), "{}", "job_running", "interrupted-page-001", "example-hash"))
            self.center.storage.connection.execute("UPDATE scan_sessions SET active_job_id=?,state='finishing' WHERE id=?", ("a" * 32, session["id"]))
        self.center.close()
        self.center = ControlCenter(self.root / "state", self.root / "password.json")
        current = self.center.scan_sessions.get(session["id"])
        self.assertEqual((current["state"], current["active_job_id"]), ("open", None))
        self.assertEqual(self.center.jobs.get("a" * 32)["state"], "interrupted")
        self.assertEqual(self.scanner.calls, 0)

    def test_real_optional_ocr_finishes_all_pages_in_order(self):
        if not self.center.ocr.capabilities()["available"]: self.skipTest("Install the documented OCR dependencies")
        session, _ = self.page(self.create())
        session, _ = self.page(session)
        session = self.center.scan_sessions.update(session["id"], {"revision": session["revision"], "pages": list(reversed(session["pages"]))})
        job = self.center.scan_sessions.finish(session["id"], {"revision": session["revision"], "ocr": True, "languages": "eng"}, "searchable-finish-001")
        result = self.wait_job(job["id"])
        self.assertEqual(result["state"], "succeeded", result)
        identifier = result["result_document_id"]
        text = self.center.storage.path(identifier, "text").read_text()
        self.assertLess(text.index("NATIVE PAGE 2"), text.index("NATIVE PAGE 1"))
        pages = PdfReader(str(self.center.storage.path(identifier))).pages
        self.assertEqual(len(pages), 2)
        self.assertIn("NATIVE", pages[0].extract_text())

    @unittest.skipUnless(virtual_scanner_available(), "Install sane-utils and its virtual test backend")
    def test_two_actual_virtual_sane_acquisitions_become_one_pdf(self):
        configuration = self.root / "sane-test"
        configuration.mkdir()
        (configuration / "dll.conf").write_text("test\n")
        (configuration / "test.conf").write_text("number_of_devices 1\n")
        lock = self.root / "device.lock"
        lock.touch()
        with patch.dict("os.environ", {"SANE_CONFIG_DIR": str(configuration)}):
            self.center.scanner = ScannerService(lock_path=str(lock), device="test:0", allow_test_backend=True)
            self.assertTrue(self.center.scanner.probe()["available"])
            session = self.center.scan_sessions.create({"name": "Virtual flatbed", "settings": {
                "resolution": 100, "mode": "color", "area": {"width": 40, "height": 40}}})
            session, _ = self.page(session)
            session, _ = self.page(session)
            job = self.center.scan_sessions.finish(session["id"], {"revision": session["revision"]}, "virtual-finish-001")
            result = self.wait_job(job["id"])
            self.assertEqual(result["state"], "succeeded", result)
            self.assertEqual(len(PdfReader(str(self.center.storage.path(result["result_document_id"]))).pages), 2)
            self.assertFalse(self.center.scanner.capabilities()["hardware_verified"])


class ScanSessionHTTPProtection(unittest.TestCase):
    def test_protected_session_creation_and_server_side_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            password = "offline-session-http-password"
            save_password(root / "password.json", password)
            center = ControlCenter(root / "state", root / "password.json")
            center.scanner = GeneratedScanner()
            class OfflineMonitor:
                def snapshot(self, force=False): return unavailable_snapshot()
            server = DashboardServer(("127.0.0.1", 0), OfflineMonitor(), start_polling=False, control_center=center)
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            cookie, csrf = None, None
            def request(method, path, body=None):
                nonlocal cookie, csrf
                connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=20)
                headers = {"Content-Type": "application/json"}
                if cookie: headers["Cookie"] = cookie
                if csrf: headers["X-CSRF-Token"] = csrf
                connection.request(method, path, json.dumps(body) if body is not None else None, headers)
                response = connection.getresponse()
                data = json.loads(response.read())
                if response.getheader("Set-Cookie"): cookie = response.getheader("Set-Cookie").split(";", 1)[0]
                connection.close()
                return response.status, data
            try:
                self.assertEqual(request("POST", "/api/scan-sessions", {"name": "Private original"})[0], 401)
                _, anonymous = request("GET", "/api/session")
                csrf = anonymous["csrf_token"]
                _, authenticated = request("POST", "/api/login", {"password": password})
                csrf = authenticated["csrf_token"]
                status, created = request("POST", "/api/scan-sessions", {"name": "Private original"})
                self.assertEqual(status, 200, created)
                identifier = created["scan_session"]["id"]
                status, recovered = request("GET", "/api/scan-sessions/" + identifier)
                self.assertEqual(status, 200)
                self.assertEqual(recovered["scan_session"]["name"], "Private original")
                self.assertEqual(request("GET", "/api/control-center")[1]["scan_sessions"][0]["id"], identifier)
                old_token, csrf = csrf, "incorrect"
                self.assertEqual(request("POST", f"/api/scan-sessions/{identifier}/pages", {"revision": 0})[0], 403)
                csrf = old_token
                self.assertEqual(request("DELETE", f"/api/scan-sessions/{identifier}")[0], 200)
                self.assertEqual(center.scanner.calls, 0)
            finally:
                server.shutdown()
                thread.join()
                server.server_close()


if __name__ == "__main__":
    unittest.main()

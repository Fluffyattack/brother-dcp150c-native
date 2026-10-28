# SPDX-License-Identifier: GPL-2.0-or-later
"""Exercise real HTTP access, private files, processing jobs and restart safety.

No test enumerates a USB device or submits a job to a real CUPS service.
"""
from __future__ import annotations

from io import BytesIO
import http.client
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from PIL import Image, ImageDraw, ImageFont

from dcp150c.access_control import AccessControl, save_password
from dcp150c.command_runner import CommandError, run_command
from dcp150c.control_center import ControlCenter
from dcp150c.document_storage import DocumentStorage, StorageError
from dcp150c.job_manager import JobManager
from dcp150c.server import DashboardServer, unavailable_snapshot


class OfflineMonitor:
    def snapshot(self, force=False):
        return unavailable_snapshot("Offline verification", "printer_offline")


class ControlCenterHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name)
        cls.password_file = cls.root / "password.json"
        cls.password = "offline-control-center-check"
        save_password(cls.password_file, cls.password)
        cls.center = ControlCenter(cls.root / "state", cls.password_file)
        # Prevent accidental physical operations even if a test regresses.
        cls.center.scanner.probe = lambda *args: (_ for _ in ()).throw(AssertionError("Hardware discovery is forbidden in this test"))
        cls.center.scanner.scan = lambda *args: (_ for _ in ()).throw(AssertionError("Hardware scanning is forbidden in this test"))
        cls.server = DashboardServer(("127.0.0.1", 0), OfflineMonitor(), control_center=cls.center, start_polling=False)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.thread.join()
        cls.server.server_close()
        cls.temporary.cleanup()

    def setUp(self):
        self.cookie = None
        self.csrf = None

    def request(self, method, path, body=None, headers=None):
        request_headers = {"Host": f"127.0.0.1:{self.port}"}
        if self.cookie:
            request_headers["Cookie"] = self.cookie
        if method not in {"GET", "HEAD"} and self.csrf:
            request_headers["X-CSRF-Token"] = self.csrf
        if isinstance(body, dict):
            body = json.dumps(body).encode()
            request_headers["Content-Type"] = "application/json"
        request_headers.update(headers or {})
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        connection.request(method, path, body, request_headers)
        response = connection.getresponse()
        payload = response.read()
        if response.getheader("Set-Cookie"):
            self.cookie = response.getheader("Set-Cookie").split(";", 1)[0]
        status, response_headers = response.status, dict(response.getheaders())
        connection.close()
        if response_headers.get("Content-Type", "").startswith("application/json"):
            payload = json.loads(payload)
        return status, payload, response_headers

    def login(self):
        status, session, _ = self.request("GET", "/api/session")
        self.assertEqual(status, 200)
        self.csrf = session["csrf_token"]
        status, session, _ = self.request("POST", "/api/login", {"password": self.password})
        self.assertEqual(status, 200)
        self.csrf = session["csrf_token"]

    def upload(self, text="Control Center offline document"):
        image = Image.new("RGB", (1400, 450), "white")
        draw = ImageDraw.Draw(image)
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 48)
        draw.text((50, 100), text, font=font, fill="black")
        data = BytesIO()
        image.save(data, "PNG")
        status, result, _ = self.request("POST", "/api/documents", data.getvalue(),
                                        {"Content-Type": "image/png", "X-Filename": "Offline%20example.png"})
        self.assertEqual(status, 201, result)
        return result["document"], data.getvalue()

    def wait_job(self, identifier):
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            job = self.center.jobs.get(identifier)
            if job["state"] not in {"queued", "running"}:
                return job
            time.sleep(0.1)
        self.fail("The offline operation did not finish")

    def test_private_endpoints_require_login_and_status_stays_available(self):
        for path in ("/api/control-center", "/api/settings/export", "/api/documents/" + "a" * 32 + "/file"):
            self.assertEqual(self.request("GET", path)[0], 401)
        self.assertEqual(self.request("GET", "/api/ink")[0], 200)
        self.assertEqual(self.request("POST", "/api/scan", {})[0], 401)

    def test_csrf_cookie_rotation_and_logout(self):
        self.login()
        authenticated_cookie = self.cookie
        status, _, _ = self.request("POST", "/api/settings", {"retention_hours": 24},
                                    {"Origin": "https://unrelated.invalid"})
        self.assertEqual(status, 403)
        self.assertEqual(self.request("POST", "/api/settings", {"retention_hours": 24}, {"X-CSRF-Token": "wrong"})[0], 403)
        self.assertEqual(self.request("POST", "/api/logout", {})[0], 200)
        self.cookie = authenticated_cookie
        self.assertEqual(self.request("GET", "/api/control-center")[0], 401)

    def test_upload_preview_rename_retention_and_private_download(self):
        self.login()
        document, original = self.upload()
        identifier = document["id"]
        for job in self.center.jobs.list():
            if job["document_id"] == identifier:
                self.assertEqual(self.wait_job(job["id"])["state"], "succeeded")
        status, downloaded, headers = self.request("GET", f"/api/documents/{identifier}/file")
        self.assertEqual((status, downloaded), (200, original))
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertEqual(self.request("GET", f"/api/documents/{identifier}/preview")[0], 200)
        status, changed, _ = self.request("POST", f"/api/documents/{identifier}", {"name": "Readable document", "keep": True})
        self.assertEqual(status, 200)
        self.assertTrue(changed["document"]["keep"])
        self.assertIsNone(changed["document"]["expires_at"])
        self.assertEqual(self.request("DELETE", f"/api/documents/{identifier}")[0], 200)
        self.assertEqual(self.request("GET", f"/api/documents/{identifier}/file")[0], 404)

    def test_real_ocr_through_http_and_reload_does_not_repeat_action(self):
        self.login()
        document, _ = self.upload("NATIVE CONTROL CENTER 2026")
        status, result, _ = self.request("POST", f"/api/documents/{document['id']}/ocr", {"languages": "deu+eng"},
                                       {"Idempotency-Key": "offline-ocr-action-001"})
        self.assertEqual(status, 202, result)
        first_id = result["job"]["id"]
        status, repeated, _ = self.request("POST", f"/api/documents/{document['id']}/ocr", {"languages": "deu+eng"},
                                         {"Idempotency-Key": "offline-ocr-action-001"})
        self.assertEqual(repeated["job"]["id"], first_id)
        completed = self.wait_job(first_id)
        self.assertEqual(completed["state"], "succeeded", completed)
        result_id = completed["result_document_id"]
        status, text, _ = self.request("GET", f"/api/documents/{result_id}/text")
        self.assertEqual(status, 200)
        self.assertIn(b"NATIVE CONTROL CENTER", text)
        before = len(self.center.jobs.list())
        for _ in range(3):
            self.assertEqual(self.request("GET", "/api/control-center")[0], 200)
        self.assertEqual(len(self.center.jobs.list()), before)

    def test_profiles_export_has_no_passwords_or_document_content(self):
        self.login()
        status, result, _ = self.request("POST", "/api/profiles", {"name": "My document", "kind": "scan",
                                        "settings": {"mode": "color", "resolution": 300, "output_format": "pdf"}})
        self.assertEqual(status, 200, result)
        status, export, _ = self.request("GET", "/api/settings/export")
        self.assertEqual(status, 200)
        self.assertEqual(set(export), {"format", "settings", "profiles"})
        self.assertNotIn(self.password, json.dumps(export))
        self.assertEqual(self.request("POST", "/api/settings", {"password": "not-a-supported-setting"})[0], 400)
        self.assertEqual(self.request("POST", "/api/settings/import", export)[0], 200)

    def test_merge_and_page_editing_work_through_background_http_jobs(self):
        self.login()
        first, _ = self.upload("FIRST PAGE")
        second, _ = self.upload("SECOND PAGE")
        status, result, _ = self.request("POST", "/api/documents/merge", {"document_ids": [first["id"], second["id"]]})
        self.assertEqual(status, 202, result)
        merged = self.wait_job(result["job"]["id"])
        self.assertEqual(merged["state"], "succeeded", merged)
        identifier = merged["result_document_id"]
        self.assertEqual(self.center.storage.get(identifier)["pages"], 2)
        from pypdf import PdfReader
        with self.center.storage.lease(identifier) as path:
            original_height = float(PdfReader(str(path)).pages[1].mediabox.height)
        status, result, _ = self.request("POST", f"/api/documents/{identifier}/transform", {
            "pages": [2, 1], "rotation": 90, "crop": {"left": 0.1, "top": 0.1, "width": 0.8, "height": 0.8}})
        self.assertEqual(status, 202, result)
        edited = self.wait_job(result["job"]["id"])
        self.assertEqual(edited["state"], "succeeded", edited)
        with self.center.storage.lease(edited["result_document_id"]) as path:
            pages = PdfReader(str(path)).pages
            self.assertEqual(len(pages), 2)
            self.assertAlmostEqual(float(pages[0].mediabox.width), original_height * 0.8, places=1)

    def test_failed_scan_cannot_start_copy_printing(self):
        self.login()
        called = []
        original = self.center.printing.submit
        self.center.printing.submit = lambda *args, **kwargs: called.append(True)
        try:
            status, result, _ = self.request("POST", "/api/copy", {"copies": 1, "resolution": 300, "mode": "color"})
            self.assertEqual(status, 202, result)
            self.assertEqual(self.wait_job(result["job"]["id"])["state"], "failed")
            self.assertEqual(called, [])
        finally:
            self.center.printing.submit = original

    def test_scan_cropping_changes_png_and_jpeg_output(self):
        original = self.center.scanner.scan
        def acquire_virtual_image(path, settings, cancel_event):
            Image.new("RGB", (120, 80), "green").save(path, "PNG")
            return {"hardware_verified": False}
        self.center.scanner.scan = acquire_virtual_image
        try:
            for output_format in ("png", "jpeg"):
                document = self.center._scan_to_document({"output_format": output_format,
                    "crop": {"left": 0.25, "top": 0.25, "width": 0.5, "height": 0.5}}, threading.Event())
                with self.center.storage.lease(document["id"]) as path, Image.open(path) as image:
                    self.assertEqual(image.size, (60, 40))
                    self.assertEqual(image.format, "PNG" if output_format == "png" else "JPEG")
        finally:
            self.center.scanner.scan = original

    def test_wrong_file_type_and_malformed_json_are_rejected(self):
        self.login()
        self.assertEqual(self.request("POST", "/api/documents", b"not a PDF", {
            "Content-Type": "application/pdf", "X-Filename": "fake.pdf"})[0], 400)
        status, _, _ = self.request("POST", "/api/settings", b'{"retention_hours":NaN}', {"Content-Type": "application/json"})
        self.assertEqual(status, 400)
        status, _, _ = self.request("POST", "/api/settings", b'{"retention_hours":24,"retention_hours":48}', {"Content-Type": "application/json"})
        self.assertEqual(status, 400)


class DurableOperationTests(unittest.TestCase):
    def test_queued_merge_inputs_survive_retention_and_reject_deletion(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = DocumentStorage(directory)
            source = Path(directory) / "source.png"
            Image.new("RGB", (5, 5), "white").save(source)
            documents = [storage.add(source, str(index), {"media_type": "image/png", "page_count": 1}) for index in range(2)]
            jobs = JobManager(storage)
            started = threading.Event()
            def blocking(event, _identifier):
                started.set()
                event.wait(5)
                raise CommandError("job_cancelled", "Cancelled")
            jobs.submit("offline", {}, blocking)
            self.assertTrue(started.wait(3))
            jobs.submit("merge", {"document_ids": [document["id"] for document in documents]}, lambda event, identifier: {})
            with storage.guard, storage.connection:
                storage.connection.execute("UPDATE documents SET expires_at=0")
            storage.prune()
            self.assertEqual(len(storage.list()), 2)
            for document in documents:
                with self.assertRaises(StorageError):
                    storage.delete(document["id"])
            jobs.close()
            storage.prune()
            self.assertEqual(storage.list(), [])
            storage.close()

    def test_orphan_recovery_and_missing_document_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = DocumentStorage(directory)
            source = Path(directory) / "source.png"
            Image.new("RGB", (5, 5), "white").save(source)
            document = storage.add(source, "recoverable", {"media_type": "image/png", "page_count": 1})
            import shutil
            shutil.rmtree(storage.documents_directory / document["id"])
            storage.delete(document["id"])
            orphan = storage.documents_directory / ("a" * 32)
            orphan.mkdir()
            (orphan / "document.png").write_bytes(b"interrupted import")
            storage.prune()
            self.assertFalse(orphan.exists())
            with self.assertRaisesRegex(StorageError, "already open"):
                DocumentStorage(directory)
            storage.close()

    def test_idempotency_conflict_and_interrupted_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = DocumentStorage(directory)
            jobs = JobManager(storage)
            started = threading.Event()
            def action(event, _identifier):
                started.set()
                event.wait(5)
                raise CommandError("job_cancelled", "Cancelled")
            first = jobs.submit("offline", {"document_id": "example"}, action, "durable-action-001")
            self.assertTrue(started.wait(3))
            self.assertEqual(jobs.submit("offline", {"document_id": "example"}, action, "durable-action-001")["id"], first["id"])
            with self.assertRaises(StorageError):
                jobs.submit("offline", {"document_id": "different"}, action, "durable-action-001")
            jobs.close()
            self.assertEqual(jobs.get(first["id"])["state"], "cancelled")
            with storage.connection:
                storage.connection.execute("UPDATE jobs SET state='running',result=? WHERE id=?", (json.dumps({"cups_job_id": "DCP150C-Native-999"}), first["id"]))
            restarted = JobManager(storage)
            self.assertEqual(restarted.get(first["id"])["state"], "interrupted")
            self.assertEqual(restarted.get(first["id"])["cups_job_id"], "DCP150C-Native-999")
            restarted.close()
            storage.close()

    def test_expired_documents_are_removed_but_leased_or_kept_documents_survive(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = DocumentStorage(directory)
            source = Path(directory) / "source.png"
            Image.new("RGB", (5, 5), "white").save(source)
            metadata = {"media_type": "image/png", "page_count": 1}
            document = storage.add(source, "temporary.png", metadata)
            with storage.guard, storage.connection:
                storage.connection.execute("UPDATE documents SET expires_at=0")
            with storage.lease(document["id"]):
                storage.prune()
                self.assertEqual(len(storage.list()), 1)
                with self.assertRaises(StorageError):
                    storage.delete(document["id"])
            storage.prune()
            self.assertEqual(storage.list(), [])
            storage.close()

    def test_runner_cancellation_reaps_descendants(self):
        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory) / "child.pid"
            script = "import subprocess,time,pathlib; child=subprocess.Popen(['sleep','60']); pathlib.Path(" + repr(str(pid_file)) + ").write_text(str(child.pid)); time.sleep(60)"
            event, errors = threading.Event(), []
            def action():
                try:
                    run_command([sys.executable, "-c", script], cancel_event=event, timeout=10)
                except CommandError as error:
                    errors.append(error.code)
            worker = threading.Thread(target=action)
            worker.start()
            deadline = time.monotonic() + 5
            while not pid_file.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(pid_file.exists())
            event.set()
            worker.join(5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, ["job_cancelled"])
            child_status = Path("/proc") / pid_file.read_text() / "stat"
            self.assertTrue(not child_status.exists() or child_status.read_text().split()[2] == "Z")

    def test_password_hash_and_rate_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            password_file = Path(directory) / "password.json"
            save_password(password_file, "another-offline-password")
            self.assertNotIn("another-offline-password", password_file.read_text())
            access = AccessControl(password_file)
            session = access.session(None, create=True)
            for _ in range(5):
                with self.assertRaisesRegex(ValueError, "login_failed"):
                    access.login(session.token, "incorrect", "offline-client")
            with self.assertRaisesRegex(ValueError, "login_rate_limited"):
                access.login(session.token, "another-offline-password", "offline-client")


if __name__ == "__main__":
    unittest.main()

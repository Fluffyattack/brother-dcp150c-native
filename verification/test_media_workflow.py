# SPDX-License-Identifier: GPL-2.0-or-later
"""Import real FAT-image documents through protected HTTP, without hardware."""

import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from PIL import Image

from dcp150c.document_storage import StorageError
from dcp150c.media_reader import MediaReaderService
from verification import test_control_center as control_center_checks
from verification.test_media_reader import FAT_TOOLS_AVAILABLE, create_fat_image, image_copy, image_directory, image_digest


@unittest.skipUnless(FAT_TOOLS_AVAILABLE, "Install dosfstools and mtools for isolated regular-file fixtures")
class MemoryCardHTTPWorkflow(unittest.TestCase):
    setUpClass = classmethod(control_center_checks.ControlCenterHTTPTests.setUpClass.__func__)
    tearDownClass = classmethod(control_center_checks.ControlCenterHTTPTests.tearDownClass.__func__)
    request = control_center_checks.ControlCenterHTTPTests.request
    login = control_center_checks.ControlCenterHTTPTests.login
    wait_job = control_center_checks.ControlCenterHTTPTests.wait_job

    def setUp(self):
        self.cookie = self.csrf = None
        self.fixture = tempfile.TemporaryDirectory(dir=self.root, prefix="memory-card-")
        self.directory = Path(self.fixture.name)
        self.image = create_fat_image(self.directory)
        image_directory(self.image, "/Documents")
        self.originals = {}
        for name, format_name in (("Foto Grüße.png", "PNG"), ("Photo.jpg", "JPEG"), ("Document.pdf", "PDF")):
            source = self.directory / name
            Image.new("RGB", (160, 240), "#28a5bb").save(source, format_name)
            self.originals[name] = source.read_bytes()
            image_copy(self.image, source, "/Documents/" + name)
        invalid = self.directory / "invalid.jpg"
        invalid.write_text("This file is not an image.")
        image_copy(self.image, invalid, "/Invalid.jpg")
        unsupported = self.directory / "Notes.txt"
        unsupported.write_text("An unsupported file is visible but cannot be imported.")
        image_copy(self.image, unsupported, "/Notes.txt")
        self.lock = self.directory / "shared-device.lock"
        self.lock.touch()
        self.center.media = MediaReaderService(str(self.lock), allow_test_backend=True, test_image_path=self.image)
        self.center._remember_media_listing(None)
        self.initial_documents = {document["id"] for document in self.center.storage.list()}
        self.initial_digest = image_digest(self.image)

    def tearDown(self):
        for document in self.center.storage.list():
            if document["id"] not in self.initial_documents:
                self.center.storage.delete(document["id"])
        if self.image.exists():
            self.assertEqual(image_digest(self.image), self.initial_digest, "The source card must remain unchanged")
        self.fixture.cleanup()

    def start(self, path, values=None, key=None):
        headers = {"Idempotency-Key": key or "card-request-" + uuid.uuid4().hex}
        status, response, _ = self.request("POST", path, {} if values is None else values, headers)
        self.assertEqual(status, 202, response)
        return self.wait_job(response["job"]["id"])

    def probe(self):
        job = self.start("/api/media/probe")
        self.assertEqual(job["state"], "succeeded", job)
        return self.center.snapshot()["media_listing"]

    def folder(self):
        root = self.probe()
        entry = next(entry for entry in root["entries"] if entry["name"] == "Documents")
        job = self.start("/api/media/directories/" + entry["id"])
        self.assertEqual(job["state"], "succeeded", job)
        return self.center.snapshot()["media_listing"]

    def test_cached_snapshots_and_protected_routes_do_not_read_card(self):
        path = "/api/media/probe"
        for endpoint in (path, "/api/media/directories/" + "a" * 32, "/api/media/files/" + "a" * 32 + "/import"):
            self.assertEqual(self.request("POST", endpoint, {})[0], 401)
        self.login()
        with patch.object(self.center.media, "_discover", side_effect=AssertionError("Implicit card discovery")):
            status, snapshot, _ = self.request("GET", "/api/control-center")
            self.assertEqual(status, 200)
            self.assertEqual(snapshot["capabilities"]["media"]["reason_code"], "media_not_checked")
            self.assertIsNone(snapshot["media_listing"])
            self.assertEqual(self.request("POST", path, {}, {"X-CSRF-Token": "wrong"})[0], 403)
            self.assertEqual(self.request("POST", path, {}, {"Origin": "https://unrelated.invalid"})[0], 403)
        status, source, headers = self.request("GET", "/assets/media.js")
        self.assertEqual(status, 200)
        self.assertIn(b"renderMediaReader", source)
        self.assertIn("javascript", headers["Content-Type"])

    def test_three_real_formats_import_preview_and_download_original_bytes(self):
        self.login()
        listing = self.folder()
        self.assertIsNotNone(listing["parent_directory_id"])
        for entry in listing["entries"]:
            job = self.start("/api/media/files/" + entry["id"] + "/import")
            self.assertEqual(job["state"], "succeeded", job)
            document = self.center.storage.get(job["result_document_id"])
            self.assertEqual(document["name"], entry["name"])
            self.assertTrue(document["has_preview"])
            status, original, _ = self.request("GET", f"/api/documents/{document['id']}/file")
            self.assertEqual(status, 200)
            self.assertEqual(original, self.originals[entry["name"]])
            self.assertEqual(hashlib.sha256(original).hexdigest(), hashlib.sha256(self.originals[entry["name"]]).hexdigest())
            self.assertEqual(self.request("GET", f"/api/documents/{document['id']}/preview")[0], 200)
        root = self.start("/api/media/directories/" + listing["parent_directory_id"], {"offset": 1, "limit": 1})
        self.assertEqual(root["state"], "succeeded", root)
        page = self.center.snapshot()["media_listing"]
        self.assertEqual((page["offset"], len(page["entries"]), page["entry_count"]), (1, 1, 3))

    def test_lost_response_retry_survives_card_removal_and_missing_dependencies(self):
        self.login()
        entry = self.folder()["entries"][0]
        path, key = "/api/media/files/" + entry["id"] + "/import", "one-card-import-only"
        first = self.start(path, key=key)
        self.assertEqual(first["state"], "succeeded", first)
        self.image.unlink()
        with patch.dict(self.center._capabilities["documents"], available=False), patch.object(
                self.center.media, "copy_file", side_effect=AssertionError("A retry must not reread a removed card")):
            repeated = self.start(path, key=key)
        self.assertEqual((repeated["id"], repeated["result_document_id"]), (first["id"], first["result_document_id"]))
        self.assertEqual(len(self.center.storage.list()), len(self.initial_documents) + 1)
        failed = self.start(path)
        self.assertEqual((failed["state"], failed["message_code"]), ("failed", "media_changed"))
        self.assertIsNone(self.center.snapshot()["media_listing"])

    def test_cancel_after_copy_before_commit_saves_no_document(self):
        self.login()
        entry = self.folder()["entries"][0]
        original_copy = self.center.media.copy_file
        def copy_then_cancel(identifier, destination, event, **settings):
            result = original_copy(identifier, destination, event, **settings)
            event.set()
            return result
        with patch.object(self.center.media, "copy_file", side_effect=copy_then_cancel):
            job = self.start("/api/media/files/" + entry["id"] + "/import")
        self.assertEqual((job["state"], job["message_code"]), ("cancelled", "job_cancelled"))
        self.assertIsNone(job["result_document_id"])
        self.assertEqual({document["id"] for document in self.center.storage.list()}, self.initial_documents)
        self.assertEqual(list(self.center.storage.work_directory.iterdir()), [])

    def test_preview_cancellation_preserves_saved_original_and_durable_result(self):
        self.login()
        entry = self.folder()["entries"][0]
        path, key = "/api/media/files/" + entry["id"] + "/import", "card-preview-interrupted"
        with patch.object(self.center.processor, "preview", side_effect=StorageError("job_cancelled", "Preview cancelled")):
            job = self.start(path, key=key)
        self.assertEqual(job["state"], "cancelled", job)
        self.assertIsNotNone(job["result_document_id"])
        document = self.center.storage.get(job["result_document_id"])
        self.assertEqual(self.request("GET", f"/api/documents/{document['id']}/file")[1], self.originals[entry["name"]])
        repeated = self.start(path, key=key)
        self.assertEqual((repeated["id"], repeated["result_document_id"]), (job["id"], document["id"]))

    def test_invalid_content_selection_and_request_parameters_leave_no_documents(self):
        self.login()
        listing = self.probe()
        for entry in listing["entries"]:
            if not entry["is_directory"]:
                job = self.start("/api/media/files/" + entry["id"] + "/import")
                self.assertEqual(job["state"], "failed", job)
                self.assertIsNone(job["result_document_id"])
        root_path = "/api/media/directories/" + listing["directory_id"]
        for values in ({"offset": -1}, {"limit": 201}, {"offset": True}, {"path": "/etc"}):
            self.assertEqual(self.request("POST", root_path, values)[0], 400)
        self.assertEqual(self.request("POST", "/api/media/probe", {"automatic": True})[0], 400)
        self.assertEqual({document["id"] for document in self.center.storage.list()}, self.initial_documents)
        self.assertEqual(list(self.center.storage.work_directory.iterdir()), [])


if __name__ == "__main__":
    unittest.main()

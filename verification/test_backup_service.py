# SPDX-License-Identifier: GPL-2.0-or-later
"""Round-trip real documents and reject incomplete or unsafe archives offline."""

from io import BytesIO
import hashlib
import json
from pathlib import Path
import shutil
import stat
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import zipfile

from PIL import Image
from pypdf import PdfWriter

from dcp150c.control_center import ControlCenter
from dcp150c.document_storage import StorageError
from verification import test_control_center as control_center_checks


class BackupRoundTrip(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.center = ControlCenter(self.root / "state", self.root / "password.json")
        self.center.scanner.probe = self.center.scanner.scan = lambda *args: self.fail("A backup must never touch hardware")
        self.center.printing.submit = lambda *args, **kwargs: self.fail("A backup must never print")
        self.image = self.root / "source.png"
        Image.new("RGB", (75, 105), "red").save(self.image)
        self.text = self.root / "source.txt"
        self.text.write_text("Ursprünglicher OCR-Text\nOriginal recognised text\n", encoding="utf-8")
        self.scan_metadata = {"kind": "scan", "width": 75, "height": 105, "resolution": 100,
            "mode": "color", "area": {"left": 5, "top": 6, "width": 19, "height": 26.7},
            "device": "test:0", "content_type": "image/png", "hardware_verified": False}
        self.document = self.center.storage.add(self.image, "Original ä.png", self.center.processor.inspect(self.image), text=self.text, scan_metadata=self.scan_metadata)
        self.center.storage.update(self.document["id"], {"keep": True})
        self.pdf = self.root / "two-pages.pdf"
        writer = PdfWriter()
        writer.add_blank_page(width=200, height=300)
        writer.add_blank_page(width=300, height=200)
        writer.write(self.pdf)
        self.second = self.center.storage.add(self.pdf, "Two original pages", self.center.processor.inspect(self.pdf))
        self.unselected = self.center.storage.add(self.image, "Excluded original", self.center.processor.inspect(self.image))

    def tearDown(self):
        self.center.close()
        self.temporary.cleanup()

    def wait(self, job):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            result = self.center.jobs.get(job["id"])
            if result["state"] not in {"queued", "running"}:
                return result
            time.sleep(0.03)
        self.fail("The offline backup operation exceeded its deadline")

    def export(self, identifiers=None, include_settings=True, key=None):
        identifiers = identifiers if identifiers is not None else [self.document["id"], self.second["id"]]
        result = self.wait(self.center.create_backup({"document_ids": identifiers, "include_settings": include_settings}, key))
        self.assertEqual(result["state"], "succeeded", result)
        with self.center.backups.download(result["result"]["backup_id"]) as handle:
            return handle.read(), result

    def restore(self, data, restore_settings=False, key=None):
        with self.center.backups.receive(len(data)) as (identifier, path):
            path.write_bytes(data)
            job = self.center.restore_backup(identifier, hashlib.sha256(data).hexdigest(), restore_settings, key)
        return self.wait(job)

    @staticmethod
    def rewrite(data, change_manifest=None, extra=None, corrupt=None):
        result = BytesIO()
        with zipfile.ZipFile(BytesIO(data)) as original, zipfile.ZipFile(result, "w", zipfile.ZIP_DEFLATED) as rewritten:
            for entry in original.infolist():
                content = original.read(entry)
                if entry.filename == "manifest.json" and change_manifest:
                    manifest = json.loads(content)
                    change_manifest(manifest)
                    content = json.dumps(manifest).encode()
                if entry.filename == corrupt:
                    content += b"changed bytes"
                rewritten.writestr(entry.filename, content)
            if extra:
                rewritten.writestr(*extra)
        return result.getvalue()

    def test_round_trip_selected_originals_dates_text_profiles_and_scanner_metadata(self):
        created_at = time.time() - 48 * 3600
        with self.center.storage.guard, self.center.storage.connection:
            self.center.storage.connection.execute("UPDATE documents SET created_at=? WHERE id=?", (created_at, self.second["id"]))
        self.center.save_profile({"name": "Saved area", "kind": "scan", "settings": {"resolution": 100, "area": self.scan_metadata["area"]}})
        data, _ = self.export()
        with zipfile.ZipFile(BytesIO(data)) as package:
            manifest = json.loads(package.read("manifest.json"))
            self.assertEqual(len(manifest["documents"]), 2)
            self.assertNotIn(self.unselected["name"], json.dumps(manifest))
            self.assertFalse(any("password" in member or "sqlite" in member or "preview" in member for member in package.namelist()))
        # Preserve the original while checking a deliberately historical date.
        self.center.storage.update(self.second["id"], {"keep": True})
        self.center.storage.update_settings({"retention_hours": 6})
        result = self.restore(data, True)
        self.assertEqual(result["state"], "succeeded", result)
        identifiers = result["result"]["restored_document_ids"]
        self.assertEqual(len(identifiers), 2)
        self.assertNotIn(self.document["id"], identifiers)
        restored = self.center.storage.get(identifiers[0])
        self.assertEqual(restored["name"], self.document["name"])
        self.assertEqual(restored["scan"], self.scan_metadata)
        self.assertTrue(restored["keep"])
        self.assertEqual(self.center.storage.path(identifiers[0]).read_bytes(), self.image.read_bytes())
        self.assertEqual(self.center.storage.path(identifiers[0], "text").read_bytes(), self.text.read_bytes())
        self.assertEqual(self.center.storage.path(identifiers[1]).read_bytes(), self.pdf.read_bytes())
        self.assertEqual(self.center.storage.get(identifiers[1])["pages"], 2)
        self.assertEqual(self.center.storage.get(identifiers[1])["created_at"], manifest["documents"][1]["created_at"])
        self.assertEqual(self.center.storage.settings()["retention_hours"], 24)
        self.assertEqual(len([profile for profile in self.center.storage.profiles() if profile["name"] == "Saved area"]), 2)
        self.center.storage.update_settings({"retention_hours": 7})
        self.center.storage.prune()
        self.assertFalse(self.center.storage.get(identifiers[1])["keep"])
        self.assertTrue(self.center.storage.get(identifiers[0])["has_preview"])
        self.assertTrue(self.center.storage.get(self.unselected["id"]))
        self.assertEqual(self.center.storage.path(self.document["id"]).read_bytes(), self.image.read_bytes())

    def test_document_only_restore_preserves_current_preferences(self):
        data, _ = self.export()
        self.center.storage.update_settings({"retention_hours": 3})
        profiles = self.center.storage.profiles()
        result = self.restore(data)
        self.assertEqual(result["state"], "succeeded", result)
        self.assertEqual(self.center.storage.settings()["retention_hours"], 3)
        self.assertEqual(self.center.storage.profiles(), profiles)

    def test_corrupt_late_file_rolls_back_all_documents_and_preferences(self):
        data, _ = self.export()
        data = self.rewrite(data, corrupt="documents/0002/document.pdf")
        self.center.storage.update_settings({"retention_hours": 4})
        before = self.center.storage.list()
        profiles = self.center.storage.profiles()
        result = self.restore(data, True)
        self.assertEqual((result["state"], result["message_code"]), ("failed", "invalid_backup"))
        self.assertEqual(self.center.storage.list(), before)
        self.assertEqual(self.center.storage.settings()["retention_hours"], 4)
        self.assertEqual(self.center.storage.profiles(), profiles)
        self.assertFalse(list(self.center.backups.directory.glob("*.incoming")))

    def test_refuse_extra_paths_symlinks_duplicates_and_excessive_directory(self):
        data, _ = self.export()
        malicious = [self.rewrite(data, extra=("../outside.txt", b"outside"))]
        link = zipfile.ZipInfo("outside-link")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        malicious.append(self.rewrite(data, extra=(link, b"/etc/passwd")))
        malicious.append(self.rewrite(data, extra=("manifest.json", b"{}")))
        bad_directory = bytearray(data)
        end = bad_directory.rfind(b"PK\x05\x06")
        bad_directory[end + 8:end + 12] = b"\xfe\xff\xfe\xff"
        malicious.append(bytes(bad_directory))
        before = len(self.center.storage.list())
        for archive in malicious:
            with self.subTest(size=len(archive)):
                result = self.restore(archive)
                self.assertEqual((result["state"], result["message_code"]), ("failed", "invalid_backup"), result)
        self.assertEqual(len(self.center.storage.list()), before)
        self.assertFalse((self.root / "outside.txt").exists())

    def test_invalid_scan_metadata_or_declared_sizes_never_reach_the_library(self):
        data, _ = self.export()
        changes = [lambda value: value["documents"][0]["scan"].update(area={"left": -1}),
                   lambda value: value["documents"][0]["file"].update(size=10000000000),
                   lambda value: value["documents"][0].update(created_at=[]),
                   lambda value: value["documents"][0].update(media_type=[])]
        for change in changes:
            result = self.restore(self.rewrite(data, change_manifest=change))
            self.assertEqual(result["message_code"], "invalid_backup", result)
        self.assertEqual(len(self.center.storage.list()), 3)

    def test_lost_responses_reuse_export_and_restore_even_after_source_deletion(self):
        selected = [self.document["id"]]
        data, exported = self.export(selected, key="lost-backup-export-001")
        self.center.storage.delete(self.document["id"])
        repeated = self.center.create_backup({"document_ids": selected, "include_settings": True}, "lost-backup-export-001")
        self.assertEqual(repeated["id"], exported["id"])
        first = self.restore(data, key="lost-backup-restore-001")
        repeated = self.restore(data, key="lost-backup-restore-001")
        self.assertEqual(first["id"], repeated["id"])
        self.assertEqual(first["result"]["document_count"], 1)
        self.assertEqual(len(self.center.storage.list()), 3)
        self.assertFalse(list(self.center.backups.directory.glob("*.incoming")))
        changed = self.rewrite(data, change_manifest=lambda manifest: manifest.update(created_at="2000-01-01T00:00:00Z"))
        with self.assertRaisesRegex(StorageError, "different operation"):
            self.restore(changed, key="lost-backup-restore-001")

    def test_queued_cancel_and_recovery_remove_uploads_without_replaying_restore(self):
        data, _ = self.export()
        release = threading.Event()
        self.center.jobs.submit("offline_wait", {}, lambda event, identifier: (release.wait(5), {})[1])
        with self.center.backups.receive(len(data)) as (identifier, path):
            path.write_bytes(data)
            self.center.backups.prune()
            self.assertTrue(path.exists(), "An active upload must survive retention maintenance")
            queued = self.center.restore_backup(identifier, hashlib.sha256(data).hexdigest(), False, "queued-restore-cancel")
        self.center.backups.prune()
        self.assertTrue(path.exists())
        self.center.jobs.cancel(queued["id"])
        self.center.backups.prune()
        self.assertFalse(path.exists())
        release.set()
        self.assertEqual(self.wait(queued)["state"], "cancelled")
        self.assertEqual(len(self.center.storage.list()), 3)
        orphan = self.center.backups.directory / ("a" * 32 + ".incoming")
        orphan.write_bytes(data)
        self.center.backups.prune()
        self.assertFalse(orphan.exists())

    def test_transaction_failure_after_first_file_preserves_documents_and_settings(self):
        data, _ = self.export()
        original_replace = Path.replace
        def fail_second_file(source, target):
            if source.name == "1.document":
                raise OSError("Simulated disk failure")
            return original_replace(source, target)
        self.center.storage.update_settings({"retention_hours": 5})
        before = self.center.storage.list()
        profiles = self.center.storage.profiles()
        with patch.object(Path, "replace", fail_second_file):
            result = self.restore(data, True)
        self.assertEqual(result["state"], "failed", result)
        self.assertEqual(self.center.storage.list(), before)
        self.assertEqual(self.center.storage.settings()["retention_hours"], 5)
        self.assertEqual(self.center.storage.profiles(), profiles)
        self.assertEqual({entry.name for entry in self.center.storage.documents_directory.iterdir()}, {item["id"] for item in before})

    def test_cancel_during_commit_rolls_back_already_moved_files_and_preferences(self):
        data, _ = self.export()
        original_restore = self.center.storage.restore_batch
        original_replace = Path.replace
        def cancel_during_commit(documents, preferences, event):
            def replace(source, target):
                result = original_replace(source, target)
                if source.name == "0.document":
                    event.set()
                return result
            with patch.object(Path, "replace", replace):
                return original_restore(documents, preferences, event)
        before = self.center.storage.list()
        profiles = self.center.storage.profiles()
        with patch.object(self.center.storage, "restore_batch", cancel_during_commit):
            result = self.restore(data, True)
        self.assertEqual((result["state"], result["message_code"]), ("cancelled", "job_cancelled"), result)
        self.assertEqual(self.center.storage.list(), before)
        self.assertEqual(self.center.storage.profiles(), profiles)
        self.assertEqual({entry.name for entry in self.center.storage.documents_directory.iterdir()}, {item["id"] for item in before})

    def test_backup_restores_on_a_fresh_installation_without_original_identifiers(self):
        data, _ = self.export()
        original_identifiers = {document["id"] for document in self.center.storage.list()}
        self.center.close()
        self.center = ControlCenter(self.root / "new-installation", self.root / "other-password.json")
        result = self.restore(data, True)
        self.assertEqual(result["state"], "succeeded", result)
        documents = self.center.storage.list()
        self.assertEqual(len(documents), 2)
        self.assertTrue(original_identifiers.isdisjoint(document["id"] for document in documents))
        self.assertEqual({document["name"] for document in documents}, {self.document["name"], self.second["name"]})
        self.assertFalse(self.center.access.configured)

    def test_prepared_backup_expiry_restart_download_and_deletion_keep_originals(self):
        data, exported = self.export()
        identifier = exported["result"]["backup_id"]
        self.center.close()
        self.center = ControlCenter(self.root / "state", self.root / "password.json")
        with self.center.backups.download(identifier) as handle:
            self.assertEqual(handle.read(), data)
            self.center.backups.delete(identifier)
            handle.seek(0)
            self.assertEqual(handle.read(), data)
        self.assertEqual(self.center.backups.list(), [])
        self.assertEqual(self.center.storage.path(self.document["id"]).read_bytes(), self.image.read_bytes())
        _, second = self.export()
        with self.center.storage.guard, self.center.storage.connection:
            self.center.storage.connection.execute("UPDATE backups SET expires_at=?", (time.time() - 1,))
        self.center.backups.prune()
        with self.assertRaises(StorageError):
            with self.center.backups.download(second["result"]["backup_id"]):
                self.fail("An expired archive must not be downloadable")

    def test_prepared_backup_limit_and_restore_quota_fail_without_replacing_data(self):
        data, _ = self.export()
        self.export()
        self.export()
        result = self.wait(self.center.create_backup({"document_ids": [], "include_settings": True}, None))
        self.assertEqual(result["message_code"], "backup_limit")
        with patch.object(self.center.storage, "check_space", side_effect=StorageError("storage_full", "Full")):
            restored = self.restore(data, True)
        self.assertEqual(restored["message_code"], "storage_full")
        self.assertEqual(len(self.center.storage.list()), 3)


class BackupHTTPTests(unittest.TestCase):
    setUpClass = classmethod(control_center_checks.ControlCenterHTTPTests.setUpClass.__func__)
    tearDownClass = classmethod(control_center_checks.ControlCenterHTTPTests.tearDownClass.__func__)
    setUp = control_center_checks.ControlCenterHTTPTests.setUp
    request = control_center_checks.ControlCenterHTTPTests.request
    login = control_center_checks.ControlCenterHTTPTests.login
    upload = control_center_checks.ControlCenterHTTPTests.upload
    wait_job = control_center_checks.ControlCenterHTTPTests.wait_job

    def test_backup_endpoints_require_login_and_anti_forgery(self):
        self.assertEqual(self.request("POST", "/api/backups", {"document_ids": []})[0], 401)
        self.assertEqual(self.request("POST", "/api/backups/restore", b"archive", {"Content-Type": "application/zip"})[0], 401)
        self.assertEqual(self.request("GET", "/api/backups/" + "a" * 32 + "/file")[0], 401)
        self.login()
        self.assertEqual(self.request("POST", "/api/backups", {"document_ids": []}, {"X-CSRF-Token": "wrong"})[0], 403)

    def test_authenticated_streamed_zip_round_trip_and_repeated_response(self):
        self.login()
        original, original_bytes = self.upload()
        status, result, _ = self.request("POST", "/api/backups", {"document_ids": [original["id"]], "include_settings": False}, {"Idempotency-Key": "http-export-roundtrip"})
        self.assertEqual(status, 202, result)
        exported = self.wait_job(result["job"]["id"])
        self.assertEqual(exported["state"], "succeeded", exported)
        identifier = exported["result"]["backup_id"]
        url = f"/api/backups/{identifier}/file"
        status, data, headers = self.request("GET", url)
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/zip")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertEqual(self.request("HEAD", url)[1], b"")
        upload_headers = {"Content-Type": "application/zip", "X-Restore-Settings": "false", "Idempotency-Key": "http-restore-roundtrip"}
        status, result, _ = self.request("POST", "/api/backups/restore", data, upload_headers)
        self.assertEqual(status, 202, result)
        restored = self.wait_job(result["job"]["id"])
        self.assertEqual(restored["state"], "succeeded", restored)
        new_identifier = restored["result"]["restored_document_ids"][0]
        self.assertNotEqual(new_identifier, original["id"])
        self.assertEqual(self.request("GET", f"/api/documents/{new_identifier}/file")[1], original_bytes)
        repeated = self.request("POST", "/api/backups/restore", data, upload_headers)
        self.assertEqual(repeated[1]["job"]["id"], restored["id"])
        self.assertEqual(self.request("DELETE", f"/api/backups/{identifier}")[0], 200)
        self.assertEqual(self.request("GET", url)[0], 404)
        self.assertEqual(self.request("GET", f"/api/documents/{original['id']}/file")[1], original_bytes)

    def test_malformed_archive_is_an_inspectable_failed_job(self):
        self.login()
        status, result, _ = self.request("POST", "/api/backups/restore", b"This is not a ZIP archive", {"Content-Type": "application/zip"})
        self.assertEqual(status, 202, result)
        job = self.wait_job(result["job"]["id"])
        self.assertEqual((job["state"], job["message_code"]), ("failed", "invalid_backup"))
        self.assertEqual(self.request("POST", "/api/backups", {"document_ids": [], "include_settings": False})[0], 400)
        status, result, _ = self.request("POST", "/api/backups/restore", b"x", {
            "Content-Type": "application/zip", "Content-Length": str(self.center.backups.maximum_upload_bytes + 1)})
        self.assertEqual((status, result["error"]), (413, "backup_too_large"))


if __name__ == "__main__":
    unittest.main()

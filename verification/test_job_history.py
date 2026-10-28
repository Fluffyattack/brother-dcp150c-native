# SPDX-License-Identifier: GPL-2.0-or-later
"""Keep unresolved work visible and durable retries safe beyond recent history.

All print references here belong to a recording fixture. No system CUPS or
physical device is contacted.
"""
import sqlite3
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import uuid

from dcp150c.control_center import ControlCenter
from dcp150c.document_storage import DocumentStorage, StorageError
from dcp150c.job_manager import JobManager
from verification import test_control_center as control_center_checks


def wait_job(jobs, identifier):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        job = jobs.get(identifier)
        if job["state"] not in {"queued", "running"}:
            return job
        time.sleep(0.01)
    raise AssertionError("The isolated metadata operation exceeded its deadline")


def seed_finished_history(storage):
    """Arrange metadata without repeating worker processing or SD-card writes."""
    now = time.time()
    request_hash = hashlib.sha256(json.dumps(["preview", {}], separators=(",", ":")).encode()).hexdigest()
    rows = [(uuid.uuid4().hex, "preview", "succeeded", now + index / 1000,
             now + index / 1000, "{}", "{}", "job_succeeded", None, request_hash) for index in range(60)]
    with storage.guard, storage.connection:
        storage.connection.executemany("INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?)", rows)


class DurableJobHistory(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.storage = DocumentStorage(self.root / "state")
        self.jobs = JobManager(self.storage)

    def tearDown(self):
        self.jobs.close()
        self.storage.close()
        self.temporary.cleanup()

    def fill_recent_history(self):
        seed_finished_history(self.storage)

    def test_older_submitted_and_uncertain_prints_remain_visible_after_recent_jobs(self):
        submitted = self.jobs.submit("print", {}, lambda event, identifier: {"cups_job_id": "DCP150C-Native-900001"})
        wait_job(self.jobs, submitted["id"])
        def uncertain(event, identifier):
            self.jobs.remember_result(identifier, {"cups_job_id": "DCP150C-Native-900002"})
            raise StorageError("processing_failed", "The recording transport lost its response")
        failed = self.jobs.submit("print", {}, uncertain)
        self.assertEqual(wait_job(self.jobs, failed["id"])["state"], "failed")
        self.fill_recent_history()
        records = self.jobs.list()
        self.assertEqual(len(records), 52)
        by_identifier = {record["id"]: record for record in records}
        self.assertTrue(by_identifier[submitted["id"]]["needs_attention"])
        self.assertTrue(by_identifier[failed["id"]]["needs_attention"])
        self.assertTrue(by_identifier[submitted["id"]]["created_at"].endswith("Z"))
        self.assertEqual({record["id"] for record in self.jobs.unfinished_print_jobs()}, {submitted["id"], failed["id"]})
        self.jobs._update(failed["id"], "failed", "print_aborted", {"cups_state": 8})
        self.assertNotIn(failed["id"], {record["id"] for record in self.jobs.list()})
        self.assertEqual([record["id"] for record in self.jobs.unfinished_print_jobs()], [submitted["id"]])

    def test_hidden_completed_request_still_reuses_its_identity_after_restart(self):
        calls = []
        def original(event, identifier):
            calls.append(identifier)
            return {}
        old = self.jobs.submit("ocr", {"document_id": "a" * 32}, original, "durable-old-result-001")
        wait_job(self.jobs, old["id"])
        self.fill_recent_history()
        self.assertNotIn(old["id"], {record["id"] for record in self.jobs.list()})
        self.jobs.close()
        self.storage.close()
        self.storage = DocumentStorage(self.root / "state")
        self.jobs = JobManager(self.storage)
        def forbidden(event, identifier):
            self.fail("An old accepted request must never run again")
        repeated = self.jobs.submit("ocr", {"document_id": "a" * 32}, forbidden, "durable-old-result-001")
        self.assertEqual(repeated["id"], old["id"])
        self.assertEqual(calls, [old["id"]])
        self.assertEqual(self.jobs.history_policy()["request_records_retention"], "while_private_state_exists")
        self.assertFalse(self.jobs.history_policy()["automatic_replay"])

    def test_queued_and_running_work_is_never_cut_off_by_the_history_window(self):
        self.fill_recent_history()
        entered, release = threading.Event(), threading.Event()
        def blocked(event, identifier):
            entered.set()
            while not release.wait(0.01):
                if event.is_set():
                    break
            return {}
        running = self.jobs.submit("ocr", {}, blocked)
        self.assertTrue(entered.wait(5))
        queued = self.jobs.submit("preview", {}, lambda event, identifier: {})
        try:
            with self.storage.guard, self.storage.connection:
                self.storage.connection.execute("UPDATE jobs SET created_at=? WHERE id IN (?,?)", (time.time() - 86400, running["id"], queued["id"]))
            records = {record["id"]: record for record in self.jobs.list()}
            self.assertEqual((records[running["id"]]["state"], records[queued["id"]]["state"]), ("running", "queued"))
            self.assertTrue(records[running["id"]]["needs_attention"])
            self.assertTrue(records[queued["id"]]["needs_attention"])
        finally:
            release.set()

    def test_state_and_result_roll_back_together_if_the_state_update_is_rejected(self):
        job = self.jobs.submit("preview", {}, lambda event, identifier: {"document_id": "a" * 32})
        before = wait_job(self.jobs, job["id"])
        with self.storage.guard, self.storage.connection:
            self.storage.connection.execute("""CREATE TRIGGER reject_failed_update BEFORE UPDATE ON jobs
                WHEN NEW.state='failed' BEGIN SELECT RAISE(ABORT,'simulated database failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.jobs._update(job["id"], "failed", "processing_failed", {"document_id": "b" * 32})
        self.assertEqual(self.jobs.get(job["id"]), before)


class ReadOnlyPrintReconciliation(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.center = ControlCenter(self.root / "state", self.root / "password.json")
        def forbidden(*args, **kwargs):
            self.fail("History refresh must not start, release or cancel a physical job")
        self.center.printing.submit = self.center.printing.cancel = forbidden
        self.center.printing.job_status = lambda identifier: {"state": 4}
        self.center.scanner.probe = self.center.scanner.scan = forbidden
        def failed_reference(event, identifier):
            self.center.jobs.remember_result(identifier, {"cups_job_id": "DCP150C-Native-900003"})
            raise StorageError("processing_failed", "Recording transport lost its response")
        self.job = self.center.jobs.submit("print", {}, failed_reference)
        self.assertEqual(wait_job(self.center.jobs, self.job["id"])["state"], "failed")

    def tearDown(self):
        self.center.close()
        self.temporary.cleanup()

    def test_failed_reference_stays_held_until_the_queue_reports_a_final_state(self):
        requested = []
        current_state = 4
        def read_status(identifier):
            requested.append(identifier)
            return {"state": current_state}
        self.center.printing.job_status = read_status
        self.center.refresh_print_job_states()
        held = self.center.jobs.get(self.job["id"])
        self.assertEqual((held["state"], held["result"]["cups_state"]), ("failed", 4))
        self.assertTrue(held["needs_attention"])
        current_state = 9
        self.center.refresh_print_job_states()
        finished = self.center.jobs.get(self.job["id"])
        self.assertEqual((finished["state"], finished["message_code"]), ("succeeded", "print_completed"))
        self.assertFalse(finished["needs_attention"])
        self.assertGreaterEqual(len(requested), 2)
        self.assertTrue(all(identifier == "DCP150C-Native-900003" for identifier in requested))

    def test_unreachable_queue_keeps_the_original_failed_reference_without_actions(self):
        def unreachable(identifier):
            raise RuntimeError("Recording scheduler is unavailable")
        self.center.printing.job_status = unreachable
        self.center.refresh_print_job_states()
        current = self.center.jobs.get(self.job["id"])
        self.assertEqual(current["state"], "failed")
        self.assertTrue(current["needs_attention"])
        self.assertEqual(current["cups_job_id"], "DCP150C-Native-900003")

    def test_cancellation_during_a_queue_read_is_not_replaced_by_stale_status(self):
        def cancelled_during_read(identifier):
            self.center.jobs.mark_cancelled(self.job["id"])
            return {"state": 4}
        self.center.printing.job_status = cancelled_during_read
        self.center.refresh_print_job_states()
        current = self.center.jobs.get(self.job["id"])
        self.assertEqual((current["state"], current["message_code"]), ("cancelled", "job_cancelled"))
        self.assertFalse(current["needs_attention"])


class ExpiredInputRequestRecovery(unittest.TestCase):
    setUpClass = classmethod(control_center_checks.ControlCenterHTTPTests.setUpClass.__func__)
    tearDownClass = classmethod(control_center_checks.ControlCenterHTTPTests.tearDownClass.__func__)
    setUp = control_center_checks.ControlCenterHTTPTests.setUp
    request = control_center_checks.ControlCenterHTTPTests.request
    login = control_center_checks.ControlCenterHTTPTests.login
    upload = control_center_checks.ControlCenterHTTPTests.upload
    wait_job = control_center_checks.ControlCenterHTTPTests.wait_job

    def test_http_retries_resolve_before_expired_inputs_and_unavailable_dependencies(self):
        self.login()
        printed = []
        def record_print(source, settings, cancel_event=None, on_submitted=None):
            printed.append(source.read_bytes())
            result = {"job_id": "DCP150C-Native-900004"}
            if on_submitted:
                on_submitted(result)
            return result
        self.center.printing.submit = record_print
        self.center.printing.job_status = lambda identifier: {"state": 9}
        accepted = []
        for kind in ("print", "transform", "ocr", "merge"):
            source, _ = self.upload()
            identifiers = [source["id"]]
            if kind == "print":
                path, values = "/api/print", {"document_id": source["id"], "copies": 1}
            elif kind == "merge":
                second, _ = self.upload()
                identifiers.append(second["id"])
                path, values = "/api/documents/merge", {"document_ids": identifiers}
            else:
                path = f"/api/documents/{source['id']}/{kind}"
                values = {"languages": "eng"} if kind == "ocr" else {"rotation": 90}
            headers = {"Idempotency-Key": "expired-input-" + kind}
            status, result, _ = self.request("POST", path, values, headers)
            self.assertEqual(status, 202, result)
            job = self.wait_job(result["job"]["id"])
            self.assertIn(job["state"], {"submitted", "succeeded"}, job)
            accepted.append((path, values, headers, job["id"]))
            for identifier in identifiers:
                self.assertEqual(self.request("DELETE", f"/api/documents/{identifier}")[0], 200)
        self.center.refresh_print_job_states()
        seed_finished_history(self.center.storage)
        self.assertTrue({item[3] for item in accepted}.isdisjoint(job["id"] for job in self.center.jobs.list()))
        self.center._capabilities["documents"]["available"] = False
        for path, values, headers, identifier in accepted:
            status, result, _ = self.request("POST", path, values, headers)
            self.assertEqual((status, result.get("job", {}).get("id")), (202, identifier), result)
        self.assertEqual(len(printed), 1)
        print_path, print_values, headers, _ = accepted[0]
        self.assertEqual(self.request("POST", print_path, {**print_values, "copies": 2}, headers)[0], 409)
        self.center._capabilities["documents"]["available"] = True
        with self.center.storage.guard:
            before = self.center.storage.connection.execute("SELECT count(*) FROM jobs").fetchone()[0]
        self.assertEqual(self.request("POST", print_path, print_values, {"Idempotency-Key": "new-request-expired-input"})[0], 400)
        with self.center.storage.guard:
            self.assertEqual(self.center.storage.connection.execute("SELECT count(*) FROM jobs").fetchone()[0], before)


if __name__ == "__main__":
    unittest.main()

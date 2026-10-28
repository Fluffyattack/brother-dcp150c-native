# SPDX-License-Identifier: GPL-2.0-or-later
"""Offline scan-button policy checks with real durable storage and job workers.

All response bytes are constructed fixtures, not physical device captures.
The injected status receiver and recorded actions never access USB, scan or print.
"""
from __future__ import annotations

from collections import deque
import copy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from dcp150c.document_storage import DocumentStorage, StorageError
from dcp150c.job_manager import JobManager
from dcp150c.scan_button_service import ScanButtonService, decode_scan_button_status


# Only the event header and four action codes are established statically. These
# inactive responses deliberately exercise a native policy, not device semantics.
QUIET_RESPONSE = bytes((0, 0x10, 3, 0, 0))
EVENT_CODES = {"image": 3, "ocr": 2, "email": 8, "file": 5}


def event_response(action):
    return bytes((0, 0x10, 3, 0x20, EVENT_CODES[action]))


def reading(status=QUIET_RESPONSE, identity="isolated-device-a"):
    return {"status": status, "device_identity": identity}


class ScriptedStatusTransport:
    def __init__(self, *responses):
        self.responses = deque(responses)
        self.calls = []
        self.guard = threading.Lock()

    def append(self, *responses):
        with self.guard:
            self.responses.extend(responses)

    def read_status(self, cancellation):
        with self.guard:
            self.calls.append(cancellation)
            if not self.responses:
                raise AssertionError("Unexpected status access: no fixture response remains")
            response = self.responses.popleft()
        if isinstance(response, BaseException):
            raise response
        return response(cancellation) if callable(response) else copy.deepcopy(response)


class ScanButtonFrames(unittest.TestCase):
    def test_four_observed_event_codes_and_framed_non_events(self):
        for action in EVENT_CODES:
            with self.subTest(action=action):
                self.assertEqual(decode_scan_button_status(event_response(action)), action)
                # Unspecified response bytes must not acquire invented meanings.
                extended = bytes((0xAB, 0x10, 3, 0x20, EVENT_CODES[action])) + bytes(250)
                self.assertEqual(decode_scan_button_status(extended), action)
        for state in (0, 0x10, 0x11, 0x12, 0x13, 0xFF):
            with self.subTest(non_event_state=state):
                self.assertIsNone(decode_scan_button_status(bytes((0, 0x10, 3, state, 0xFE))))

    def test_short_oversized_wrongly_framed_and_unassigned_responses(self):
        invalid = [None, "status", bytearray(QUIET_RESPONSE), bytes(4), bytes(256),
                   bytes((0, 0x11, 3, 0, 0)), bytes((0, 0x10, 4, 0, 0))]
        for response in invalid:
            with self.subTest(response=repr(response)):
                with self.assertRaises(StorageError) as failure:
                    decode_scan_button_status(response)
                self.assertEqual(failure.exception.code, "scan_button_status_invalid")
        with self.assertRaises(StorageError) as failure:
            decode_scan_button_status(bytes((0, 0x10, 3, 0x20, 0xFE)))
        self.assertEqual(failure.exception.code, "scan_button_unassigned")


class ScanButtonWorkflows(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="dcp150c-button-check-")
        self.state_directory = Path(self.temporary.name) / "private-state"
        self.storage = DocumentStorage(self.state_directory)
        self.jobs = JobManager(self.storage)
        self.scan_profile = self.storage.save_profile("Document", "scan", {
            "resolution": 300, "mode": "color", "output_format": "pdf"})
        self.ocr_profile = self.storage.save_profile("Searchable document", "ocr", {"languages": "deu+eng"})
        self.services = []
        self.release_events = []
        self.actions = []
        self.action_guard = threading.Lock()

    def tearDown(self):
        for release in self.release_events:
            release.set()
        for service in self.services:
            service.close()
        self.jobs.close()
        self.storage.close()
        self.temporary.cleanup()

    def binding(self, action="file"):
        result = {"scan_profile_id": self.scan_profile["id"]}
        if action == "ocr":
            result["ocr_profile_id"] = self.ocr_profile["id"]
        return result

    def resolve_binding(self, action, binding):
        profiles = {profile["id"]: profile for profile in self.storage.profiles()}
        try:
            scan = profiles[binding["scan_profile_id"]]
            if scan["kind"] != "scan":
                raise KeyError("scan")
            result = {"scan_profile_id": scan["id"], "scan_settings": scan["settings"]}
            if action == "ocr":
                ocr = profiles[binding["ocr_profile_id"]]
                if ocr["kind"] != "ocr":
                    raise KeyError("ocr")
                result.update(ocr_profile_id=ocr["id"], ocr_settings=ocr["settings"])
            return result
        except KeyError as error:
            raise StorageError("invalid_profile", "The selected profile is unavailable.") from error

    def perform_action(self, cancellation, identifier, parameters):
        with self.action_guard:
            self.actions.append({"id": identifier, "parameters": copy.deepcopy(parameters)})
        return {"recorded_action": parameters["button_action"]}

    def make_service(self, transport=None, *, resolve_binding=None, start_worker=False):
        transport = transport if transport is not None else ScriptedStatusTransport()
        service = ScanButtonService(self.storage, self.jobs, resolve_binding or self.resolve_binding,
                                    self.perform_action, transport=transport,
                                    start_worker=start_worker, poll_interval=0.05)
        self.services.append(service)
        return service

    def enable(self, service, actions=("file",)):
        return service.configure({"revision": service.snapshot()["configuration"]["revision"],
                                  "enabled": True,
                                  "bindings": {action: self.binding(action) for action in actions}})

    def poll(self, service, *responses):
        service.transport.append(*responses)
        return [service.poll_once() for _ in responses]

    def arm(self, service, identity="isolated-device-a"):
        first, second = self.poll(service, reading(identity=identity), reading(identity=identity))
        self.assertEqual(first["status_code"], "scan_button_waiting")
        self.assertEqual(second["status_code"], "scan_button_ready")

    def scan_jobs(self):
        return [job for job in self.jobs.list() if job["kind"] == "scan_button"]

    def wait_for_job(self, identifier, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = self.jobs.get(identifier)
            if result["state"] not in {"queued", "running"}:
                return result
            time.sleep(0.01)
        self.fail("The isolated document job did not finish")

    def block_job_worker(self):
        entered, release = threading.Event(), threading.Event()
        self.release_events.append(release)
        def hold(cancellation, _identifier):
            entered.set()
            while not release.wait(0.01):
                if cancellation.is_set():
                    return {}
            return {}
        blocker = self.jobs.submit("verification_blocker", {}, hold)
        self.assertTrue(entered.wait(2), "The real job worker did not start")
        return release, blocker

    def test_disabled_construction_polling_snapshots_and_restart_perform_no_status_io(self):
        with patch("dcp150c.scan_button_transport.ScanButtonTransport.read_status",
                   side_effect=AssertionError("Disabled receiver performed device access")) as status_read:
            service = ScanButtonService(self.storage, self.jobs, self.resolve_binding, self.perform_action,
                                        start_worker=True, poll_interval=0.05)
            self.services.append(service)
            for _ in range(10):
                self.assertEqual(service.poll_once()["status_code"], "scan_button_disabled")
                self.assertFalse(service.snapshot()["hardware_verified"])
            time.sleep(0.08)
            service.close()
            self.assertFalse(service._worker.is_alive())
            status_read.assert_not_called()
        self.assertEqual(self.scan_jobs(), [])

    def test_each_observed_action_enqueues_only_a_document_job_after_two_inactive_readings(self):
        service = self.make_service()
        self.enable(service, tuple(EVENT_CODES))
        for action in EVENT_CODES:
            with self.subTest(action=action):
                self.arm(service)
                result = self.poll(service, reading(event_response(action)))[0]
                job = self.wait_for_job(result["last_job_id"])
                self.assertEqual(job["kind"], "scan_button")
                self.assertEqual(job["state"], "succeeded")
                self.assertEqual(job["result"]["recorded_action"], action)
        self.assertEqual({record["parameters"]["button_action"] for record in self.actions}, set(EVENT_CODES))
        self.assertFalse(any(job["kind"] in {"print", "copy"} for job in self.jobs.list()))

    def test_stale_events_and_a_single_inactive_reading_do_not_arm_the_receiver(self):
        service = self.make_service()
        self.enable(service)
        for response in (event_response("file"), QUIET_RESPONSE, event_response("file"),
                         event_response("file")):
            result = self.poll(service, reading(response))[0]
            self.assertEqual(result["status_code"], "scan_button_waiting")
        self.assertEqual(self.scan_jobs(), [])
        self.arm(service)
        accepted = self.poll(service, reading(event_response("file")))[0]
        self.wait_for_job(accepted["last_job_id"])
        for _ in range(5):
            self.poll(service, reading(event_response("file")))
        self.assertEqual(len(self.scan_jobs()), 1)
        self.arm(service)
        accepted = self.poll(service, reading(event_response("file")))[0]
        self.wait_for_job(accepted["last_job_id"])
        self.assertEqual(len(self.scan_jobs()), 2)

    def test_device_change_error_and_invalid_response_each_require_a_new_baseline(self):
        service = self.make_service()
        self.enable(service)
        self.arm(service)
        changed = self.poll(service, reading(event_response("file"), "isolated-device-b"))[0]
        self.assertEqual(changed["status_code"], "scan_button_waiting")
        self.assertEqual(self.scan_jobs(), [])
        self.arm(service, "isolated-device-b")
        offline = StorageError("scan_button_offline", "The isolated receiver is unavailable.")
        self.poll(service, offline, reading(event_response("file"), "isolated-device-b"))
        self.assertEqual(self.scan_jobs(), [])
        self.arm(service, "isolated-device-b")
        invalid = self.poll(service, reading(b"bad", "isolated-device-b"))[0]
        self.assertEqual(invalid["status_code"], "scan_button_status_invalid")
        self.poll(service, reading(event_response("file"), "isolated-device-b"))
        self.assertEqual(self.scan_jobs(), [])
        self.arm(service, "isolated-device-b")
        accepted = self.poll(service, reading(event_response("file"), "isolated-device-b"))[0]
        self.assertEqual(self.wait_for_job(accepted["last_job_id"])["state"], "succeeded")

    def test_busy_discards_an_incomplete_baseline_but_preserves_an_established_one(self):
        service = self.make_service()
        self.enable(service)
        busy = StorageError("device_busy", "The isolated printer lock is busy.")
        self.poll(service, reading(), busy, reading(), reading(event_response("file")))
        self.assertEqual(self.scan_jobs(), [])
        self.arm(service)
        result = self.poll(service, busy)[0]
        self.assertEqual(result["status_code"], "scan_button_busy")
        result = self.poll(service, reading(event_response("file")))[0]
        self.assertEqual(self.wait_for_job(result["last_job_id"])["state"], "succeeded")

    def test_a_running_button_job_suppresses_reads_and_requires_a_fresh_baseline(self):
        release, _ = self.block_job_worker()
        service = self.make_service()
        self.enable(service)
        self.arm(service)
        accepted = self.poll(service, reading(event_response("file")))[0]
        previous_reads = len(service.transport.calls)
        for _ in range(5):
            self.assertEqual(service.poll_once()["status_code"], "scan_button_running")
        self.assertEqual(len(service.transport.calls), previous_reads)
        release.set()
        self.wait_for_job(accepted["last_job_id"])
        self.poll(service, reading(event_response("file")))
        self.assertEqual(len(self.scan_jobs()), 1)

    def test_reconfiguration_discards_an_inflight_read_and_parallel_poll_calls_share_it(self):
        entered, release = threading.Event(), threading.Event()
        self.release_events.append(release)
        cancellations = []
        def delayed(cancellation):
            cancellations.append(cancellation)
            entered.set()
            release.wait(2)
            return reading(event_response("file"))
        service = self.make_service()
        self.enable(service)
        self.arm(service)
        service.transport.append(delayed)
        failures = []
        def poll_once():
            try:
                service.poll_once()
            except Exception as error:
                failures.append(error)
        receiver = threading.Thread(target=poll_once)
        receiver.start()
        self.assertTrue(entered.wait(2))
        for _ in range(10):
            service.snapshot()
            service.poll_once()
        self.assertEqual(len(service.transport.calls), 3)
        configured = service.configure({"revision": 1, "enabled": True,
                                        "bindings": {"image": self.binding("image")}})
        self.assertEqual(configured["configuration"]["revision"], 2)
        self.assertTrue(cancellations[0].is_set())
        release.set()
        receiver.join(2)
        self.assertFalse(receiver.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(self.scan_jobs(), [])
        self.assertEqual(service.snapshot()["status_code"], "scan_button_waiting")

    def test_parallel_browser_revisions_conflict_but_repeated_identical_save_does_not_rearm(self):
        service = self.make_service()
        barrier = threading.Barrier(3)
        results, errors = [], []
        def save(action):
            barrier.wait()
            try:
                results.append(service.configure({"revision": 0, "enabled": True,
                                                  "bindings": {action: self.binding(action)}}))
            except StorageError as error:
                errors.append(error.code)
        browsers = [threading.Thread(target=save, args=(action,)) for action in ("image", "file")]
        for browser in browsers:
            browser.start()
        barrier.wait()
        for browser in browsers:
            browser.join(2)
            self.assertFalse(browser.is_alive())
        self.assertEqual(len(results), 1)
        self.assertEqual(errors, ["request_conflict"])
        self.arm(service)
        saved = copy.deepcopy(results[0]["configuration"])
        saved["revision"] = 0  # Retry after a hypothetical lost successful response.
        repeated = service.configure(saved)
        self.assertEqual(repeated["status_code"], "scan_button_ready")
        self.assertEqual(repeated["configuration"]["revision"], 1)
        action = next(iter(saved["bindings"]))
        accepted = self.poll(service, reading(event_response(action)))[0]
        self.assertEqual(self.wait_for_job(accepted["last_job_id"])["state"], "succeeded")

    def test_profile_deletion_does_not_prevent_disabling(self):
        service = self.make_service()
        self.enable(service)
        self.storage.delete_profile(self.scan_profile["id"])
        result = service.configure({"revision": 1, "enabled": False,
                                    "bindings": {"file": self.binding()}})
        self.assertEqual(result["status_code"], "scan_button_disabled")
        self.assertFalse(result["configuration"]["enabled"])
        service.poll_once()
        self.assertEqual(service.transport.calls, [])

    def test_deleted_or_unassigned_profile_produces_no_job(self):
        service = self.make_service()
        self.enable(service)
        self.arm(service)
        result = self.poll(service, reading(event_response("image")))[0]
        self.assertEqual(result["status_code"], "scan_button_unassigned")
        self.arm(service)
        self.storage.delete_profile(self.scan_profile["id"])
        result = self.poll(service, reading(event_response("file")))[0]
        self.assertEqual(result["status_code"], "invalid_profile")
        self.assertIsNone(result["last_job_id"])
        self.assertEqual(self.scan_jobs(), [])

    def test_queue_full_and_failed_database_reservation_cannot_dispatch_button_actions(self):
        release, _ = self.block_job_worker()
        for index in range(20):
            self.jobs.submit("verification_filler", {"index": index}, lambda *_args: {})
        service = self.make_service()
        self.enable(service)
        self.arm(service)
        result = self.poll(service, reading(event_response("file")))[0]
        self.assertEqual(result["status_code"], "job_queue_full")
        self.assertIsNone(result["last_job_id"])
        self.assertEqual(self.scan_jobs(), [])
        release.set()
        deadline = time.monotonic() + 3
        while self.jobs._queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.jobs._queue.unfinished_tasks, 0)
        with self.storage.guard, self.storage.connection:
            self.storage.connection.executescript("""
                CREATE TRIGGER reject_button_reservation BEFORE UPDATE ON scan_button_configuration
                WHEN NEW.last_job_id IS NOT NULL BEGIN SELECT RAISE(ABORT,'isolated reservation failure'); END;
            """)
        self.arm(service)
        result = self.poll(service, reading(event_response("file")))[0]
        self.assertEqual(result["status_code"], "scan_button_status_failed")
        self.assertIsNone(result["last_job_id"])
        self.assertEqual(self.scan_jobs(), [])
        self.assertEqual(self.actions, [])
        with self.storage.guard:
            persisted = self.storage.connection.execute(
                "SELECT last_job_id,last_action FROM scan_button_configuration").fetchone()
        self.assertEqual(tuple(persisted), (None, None))
        self.assertTrue(self.jobs._worker.is_alive())

    def test_accepted_parameters_are_frozen_against_profile_and_resolver_cache_changes(self):
        release, _ = self.block_job_worker()
        cached = self.resolve_binding("file", self.binding())
        service = self.make_service(resolve_binding=lambda *_args: cached)
        self.enable(service)
        self.arm(service)
        result = self.poll(service, reading(event_response("file")))[0]
        cached["scan_settings"]["resolution"] = 600
        self.storage.save_profile("Document changed", "scan", {"resolution": 600, "mode": "gray"},
                                  identifier=self.scan_profile["id"])
        with self.storage.guard:
            persisted = self.storage.connection.execute("SELECT parameters FROM jobs WHERE id=?",
                                                        (result["last_job_id"],)).fetchone()[0]
        self.assertEqual(json.loads(persisted)["scan_settings"]["resolution"], 300)
        release.set()
        self.wait_for_job(result["last_job_id"])
        self.assertEqual(self.actions[0]["parameters"]["scan_settings"]["resolution"], 300)

    def test_restart_keeps_last_job_without_replaying_an_interrupted_event(self):
        service = self.make_service()
        self.enable(service)
        self.arm(service)
        accepted = self.poll(service, reading(event_response("file")))[0]
        identifier = accepted["last_job_id"]
        self.wait_for_job(identifier)
        # Persist an interruption boundary without running the action again.
        self.jobs._update(identifier, "running", "job_running")
        service.close()
        self.jobs.close()
        self.storage.close()
        self.storage = DocumentStorage(self.state_directory)
        self.jobs = JobManager(self.storage)
        restarted = self.make_service()
        self.assertEqual(self.jobs.get(identifier)["state"], "interrupted")
        self.assertEqual(restarted.snapshot()["last_job_id"], identifier)
        self.assertEqual(restarted.snapshot()["last_action"], "file")
        self.poll(restarted, reading(event_response("file")), reading(event_response("file")))
        self.assertEqual(len(self.scan_jobs()), 1)
        self.assertEqual(len(self.actions), 1)
        self.arm(restarted)
        new_event = self.poll(restarted, reading(event_response("file")))[0]
        self.assertNotEqual(new_event["last_job_id"], identifier)
        self.wait_for_job(new_event["last_job_id"])
        self.assertEqual(len(self.actions), 2)

    def test_real_receiver_thread_stops_and_cancels_its_current_transport_read(self):
        entered, cancelled = threading.Event(), threading.Event()
        def wait_for_cancellation(cancellation):
            entered.set()
            if cancellation.wait(2):
                cancelled.set()
            raise StorageError("job_cancelled", "The isolated receive operation stopped.")
        transport = ScriptedStatusTransport(wait_for_cancellation)
        service = self.make_service(transport, start_worker=True)
        self.enable(service)
        self.assertTrue(entered.wait(2))
        started = time.monotonic()
        service.close()
        self.assertLess(time.monotonic() - started, 1)
        self.assertTrue(cancelled.is_set())
        self.assertFalse(service._worker.is_alive())
        service.poll_once()
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(self.scan_jobs(), [])

    def test_stop_cannot_cancel_an_old_token_and_lose_the_new_poll_token(self):
        service = self.make_service()
        self.enable(service)
        checked, release_check = threading.Event(), threading.Event()
        self.release_events.append(release_check)
        cancellation_seen = []
        original_stop = service._stop
        class ScheduledStop:
            def is_set(self):
                value = original_stop.is_set()
                if threading.current_thread().name == "isolated-button-poll" and not value:
                    checked.set()
                    release_check.wait(2)  # Controlled context switch after a false stop check.
                return value
            def set(self):
                original_stop.set()
        service._stop = ScheduledStop()
        def wait_for_cancel(cancellation):
            cancellation_seen.append(cancellation.wait(0.75))
            return reading()
        service.transport.append(wait_for_cancel)
        receiver = threading.Thread(target=service.poll_once, name="isolated-button-poll")
        service._worker = receiver
        receiver.start()
        self.assertTrue(checked.wait(2))
        closing = threading.Thread(target=service.close)
        closing.start()
        # close() may wait for the guard, but must cancel the token published
        # after the controlled context switch rather than a superseded token.
        release_check.set()
        closing.join(2)
        receiver.join(2)
        self.assertFalse(closing.is_alive())
        self.assertFalse(receiver.is_alive())
        self.assertTrue(all(cancellation_seen))
        self.assertEqual(self.scan_jobs(), [])


if __name__ == "__main__":
    unittest.main()

# SPDX-License-Identifier: GPL-2.0-or-later
"""Supervise real isolated child processes and locks; never enumerate hardware."""

from __future__ import annotations

import fcntl
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from dcp150c.document_storage import DocumentStorage, StorageError
from dcp150c.network_scanner_service import NetworkScannerService, SOURCE_REVISION, local_network_interfaces
from verification import test_control_center as control_center_checks


CHILD_PROCESS = '''
import fcntl,json,os,signal,sys,time
from pathlib import Path
directory=Path(os.environ["DCP150C_NETWORK_SCANNER_STATE_DIRECTORY"])
mode=(directory/"fixture-mode").read_text()
if mode=="crash": sys.exit(7)
if mode in ("session","metadata","invalid"):
    descriptor=os.open(os.environ["DCP150C_LOCK_PATH"],os.O_RDWR)
    fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
    signal.signal(signal.SIGTERM,signal.SIG_IGN)
    payload={"process_id":os.getpid(),"acquired_monotonic":time.monotonic(),"kind":mode}
    (directory/"lease.json").write_text(json.dumps([] if mode=="invalid" else payload))
print("listening on virtual fixture",file=sys.stderr,flush=True)
while True: time.sleep(.05)
'''


class NetworkScannerSupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.storage = DocumentStorage(self.root / "state")
        self.lock_path = self.root / "device.lock"
        self.lock_path.touch(mode=0o600)
        self.executable = self.root / "isolated-process"
        self.executable.write_text("#!" + sys.executable + "\n" + CHILD_PROCESS)
        self.executable.chmod(0o755)
        self.information = self.root / "build-information.json"
        self.information.write_text(json.dumps({"format": 1, "protocol": "eSCL", "shared_device_access": True,
            "source": {"revision": SOURCE_REVISION}, "binary_sha256": hashlib.sha256(self.executable.read_bytes()).hexdigest()}))
        self.information.chmod(0o600)
        self.sane = self.root / "sane"
        self.sane.mkdir(mode=0o700)
        (self.sane / "dll.conf").write_text("test\n")
        (self.sane / "dll.conf").chmod(0o600)
        self.interfaces = [{"name": "lo", "addresses": ["127.0.0.1"]}]
        self.services = []

    def service(self, *, worker=True, mode="ready", **settings):
        result = NetworkScannerService(self.storage, lock_path=self.lock_path,
            executable=self.executable, information_file=self.information, sane_configuration=self.sane,
            allow_test_backend=True, interfaces_provider=lambda: self.interfaces,
            start_worker=worker, metadata_timeout=.5, session_timeout=.7, stop_grace=.15, poll_interval=.02, **settings)
        (result.directory / "fixture-mode").write_text(mode)
        self.services.append(result)
        return result

    def enable(self, service):
        return service.configure({"revision": service.snapshot()["configuration"]["revision"],
                                  "enabled": True, "interface": "lo", "port": 18090})

    def wait_for(self, service, status):
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            snapshot = service.snapshot()
            if snapshot["status_code"] == status:
                return snapshot
            time.sleep(.02)
        self.fail("Network scanner did not reach " + status + ": " + str(service.snapshot()))

    def tearDown(self):
        for service in self.services:
            service.close()
        self.storage.close()
        self.temporary.cleanup()

    def test_disabled_state_and_browser_reads_never_launch_or_lock(self):
        with patch("dcp150c.network_scanner_service.subprocess.Popen") as launch:
            service = self.service()
            for _ in range(10):
                self.assertEqual(service.snapshot()["status_code"], "network_scanner_disabled")
            service.close()
            launch.assert_not_called()
        with self.lock_path.open("rb") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_explicit_listener_idles_without_device_lock_and_stops_on_disable(self):
        service = self.service()
        self.enable(service)
        self.wait_for(service, "network_scanner_ready")
        child = service._process
        self.assertIsNone(service.snapshot()["active_operation"])
        with self.lock_path.open("rb") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        arguments = child.args
        self.assertIn("--hotplug=false", arguments)
        self.assertIn("--network-hotplug=false", arguments)
        self.assertIn("--mdns-announce=false", arguments)
        self.assertEqual((service.directory / "access.conf").read_text(), "allow local on lo\n")
        configuration = service.snapshot()["configuration"]
        service.configure({**configuration, "enabled": False})
        self.wait_for(service, "network_scanner_disabled")
        self.assertIsNotNone(child.poll())

    def test_disabled_share_refreshes_local_addresses_without_starting_a_listener(self):
        self.interfaces = []
        service = self.service(worker=False)
        self.interfaces = [{"name": "new0", "addresses": ["192.168.2.3"]}]
        service._next_interfaces_check = 0
        with patch.object(service, "_start_process") as launch:
            service._iteration()
            self.assertEqual(service.snapshot()["interfaces"], self.interfaces)
            self.assertFalse(service.snapshot()["configuration"]["enabled"])
            launch.assert_not_called()

    def test_timed_out_acquisition_is_killed_before_lock_is_free_and_not_replayed(self):
        service = self.service(mode="session")
        self.enable(service)
        self.wait_for(service, "network_scanner_busy")
        child = service._process
        with self.lock_path.open("rb") as lock:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.wait_for(service, "network_scanner_timed_out")
        self.assertEqual(child.poll(), -9)  # The fixture deliberately ignores TERM.
        with self.lock_path.open("rb") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        time.sleep(.15)
        self.assertIsNone(service._process)
        self.assertEqual(service.snapshot()["status_code"], "network_scanner_timed_out")
        configuration = service.snapshot()["configuration"]
        service.configure(configuration)  # Lost save response must not rearm it.
        self.assertIsNone(service._process)
        (service.directory / "fixture-mode").write_text("ready")
        service.restart({"revision": configuration["revision"]})
        self.wait_for(service, "network_scanner_ready")
        self.assertNotEqual(service._process.pid, child.pid)

    def test_metadata_has_a_separate_deadline(self):
        service = self.service(mode="metadata")
        self.enable(service)
        self.wait_for(service, "network_scanner_timed_out")
        self.assertEqual(service.snapshot()["last_exit_code"], -9)

    def test_unexpected_child_exit_is_latched_until_explicit_restart(self):
        service = self.service(mode="crash")
        self.enable(service)
        self.wait_for(service, "network_scanner_failed")
        self.assertEqual(service.snapshot()["last_exit_code"], 7)
        time.sleep(.1)
        self.assertIsNone(service._process)

    def test_malformed_lease_fails_closed(self):
        service = self.service(mode="invalid")
        self.enable(service)
        self.wait_for(service, "network_scanner_failed")
        self.assertEqual(service.snapshot()["last_exit_code"], -9)

    def test_network_address_change_stops_listener_and_requires_restart(self):
        service = self.service()
        self.enable(service)
        self.wait_for(service, "network_scanner_ready")
        child = service._process
        self.interfaces = [{"name": "lo", "addresses": ["127.0.0.2"]}]
        service._next_interfaces_check = 0
        self.wait_for(service, "network_scanner_network_changed")
        self.assertIsNotNone(child.poll())
        self.assertIsNone(service._process)

    def test_revision_conflicts_invalid_fields_and_missing_installation(self):
        service = self.service(worker=False)
        current = service.snapshot()["configuration"]
        for changes in ({"port": True}, {"port": 80}, {"port": 65536}, {"interface": "lo\nallow all"},
                        {"enabled": 1}, {"revision": True}, {"extra": "argument"}):
            with self.subTest(changes=changes), self.assertRaises(StorageError):
                service.configure({**current, **changes})
        with self.assertRaises(StorageError) as conflict:
            service.configure({**current, "enabled": True, "interface": "lo", "revision": 2})
        self.assertEqual(conflict.exception.code, "request_conflict")
        self.information.write_text("{}")
        with self.assertRaises(StorageError) as missing:
            self.enable(service)
        self.assertEqual(missing.exception.code, "network_scanner_missing")
        self.assertFalse(service.snapshot()["configuration"]["enabled"])

    def test_unavailable_interface_can_always_be_disabled_and_persists(self):
        service = self.service(worker=False)
        self.enable(service)
        self.interfaces = []
        self.executable.unlink()
        current = service.snapshot()["configuration"]
        service.configure({**current, "enabled": False})
        service.close()
        reopened = self.service(worker=False)
        self.assertFalse(reopened.snapshot()["configuration"]["enabled"])
        self.assertEqual(reopened.snapshot()["configuration"]["interface"], "lo")

    def test_native_backend_isolation_rejects_other_backends_and_dll_directory(self):
        service = self.service(worker=False)
        self.assertTrue(service.snapshot()["available"])
        (self.sane / "dll.conf").write_text("test\nbrother2\n")
        self.assertFalse(service._installation_available())
        (self.sane / "dll.conf").write_text("test\n")
        (self.sane / "dll.d").mkdir()
        self.assertFalse(service._installation_available())

    def test_local_interface_inspection_filters_loopback_and_handles_failure(self):
        records = [{"ifname": "lo", "flags": ["LOOPBACK"], "addr_info": [{"local": "127.0.0.1"}]},
                   {"ifname": "eth0", "flags": ["UP"], "addr_info": [{"local": "192.168.1.2"}, {"local": "invalid"}]}]
        with patch("dcp150c.network_scanner_service.subprocess.run", return_value=subprocess.CompletedProcess([], 0, json.dumps(records).encode())):
            self.assertEqual(local_network_interfaces(), [{"name": "eth0", "addresses": ["192.168.1.2"]}])
        with patch("dcp150c.network_scanner_service.subprocess.run", side_effect=subprocess.TimeoutExpired("ip", 3)):
            self.assertEqual(local_network_interfaces(), [])


class NetworkScannerHTTPWorkflow(unittest.TestCase):
    setUpClass = classmethod(control_center_checks.ControlCenterHTTPTests.setUpClass.__func__)
    tearDownClass = classmethod(control_center_checks.ControlCenterHTTPTests.tearDownClass.__func__)
    request = control_center_checks.ControlCenterHTTPTests.request
    login = control_center_checks.ControlCenterHTTPTests.login

    def setUp(self):
        self.cookie = self.csrf = None

    def test_protected_routes_and_cached_default_do_not_launch_a_listener(self):
        values = {"revision": 0, "enabled": False, "interface": "", "port": 8090}
        with patch.object(self.center.network_scanner, "_start_process") as launch:
            self.assertEqual(self.request("POST", "/api/network-scanner/settings", values)[0], 401)
            self.login()
            self.assertEqual(self.request("POST", "/api/network-scanner/settings", values,
                                          {"X-CSRF-Token": "invalid"})[0], 403)
            status, payload, _ = self.request("POST", "/api/network-scanner/settings", values)
            self.assertEqual(status, 200, payload)
            self.assertFalse(payload["network_scanner"]["configuration"]["enabled"])
            status, payload, _ = self.request("GET", "/api/control-center")
            self.assertEqual(status, 200, payload)
            self.assertFalse(payload["network_scanner"]["hardware_verified"])
            self.assertEqual(self.request("POST", "/api/network-scanner/restart", {"revision": 0})[0], 400)
            launch.assert_not_called()


if __name__ == "__main__":
    unittest.main()

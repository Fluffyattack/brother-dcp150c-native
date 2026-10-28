# SPDX-License-Identifier: GPL-2.0-or-later
"""Full eSCL server and sane-airscan client against an isolated SANE test device.

Set DCP150C_AIRSANE_TEST_BINARY to the binary produced by the source builder.
No system SANE configuration, multicast discovery, USB, printer or scanner is
used. Successful virtual acquisitions do not establish physical device support.
"""

from __future__ import annotations

import fcntl
import hashlib
import http.client
from io import BytesIO
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit
from xml.etree import ElementTree

from PIL import Image
from pypdf import PdfReader

from dcp150c.document_storage import DocumentStorage
from dcp150c.network_scanner_service import NetworkScannerService, SOURCE_REVISION


BINARY = os.environ.get("DCP150C_AIRSANE_TEST_BINARY", "")


@unittest.skipUnless(BINARY and Path(BINARY).is_file(), "Build the optional network scanner and set DCP150C_AIRSANE_TEST_BINARY")
class NativeNetworkScannerWorkflow(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.storage = DocumentStorage(self.root / "state")
        self.lock = self.root / "device.lock"
        self.lock.touch(mode=0o600)
        self.binary = Path(BINARY).resolve()
        self.information = self.root / "build-information.json"
        self.information.write_text(json.dumps({"format": 1, "protocol": "eSCL", "shared_device_access": True,
            "source": {"revision": SOURCE_REVISION}, "binary_sha256": hashlib.sha256(self.binary.read_bytes()).hexdigest()}))
        self.information.chmod(0o600)
        self.sane = self.root / "sane"
        self.sane.mkdir(mode=0o700)
        (self.sane / "dll.conf").write_text("test\n")
        (self.sane / "dll.conf").chmod(0o600)
        with socket.socket() as address:
            address.bind(("127.0.0.1", 0))
            self.port = address.getsockname()[1]
        self.service = None

    def start(self, *, slow=False):
        (self.sane / "test.conf").write_text(
            'number_of_devices 1\nmode Color\ndepth 8\nresolution_min 50\nresolution_max 1200\n'
            'resolution_quant 1\nresolution 75\ntest-picture "Color pattern"\n'
            'geometry_min 0\ngeometry_max 220\ngeometry_quant 0.001\ntl_x 0\ntl_y 0\nbr_x 80\nbr_y 100\n'
            + ('read-limit true\nread-limit-size 2048\nread-delay true\nread-delay-duration 100000\n' if slow else ''))
        (self.sane / "test.conf").chmod(0o600)
        self.service = NetworkScannerService(self.storage, lock_path=self.lock, executable=self.binary,
            information_file=self.information, sane_configuration=self.sane, allow_test_backend=True,
            interfaces_provider=lambda: [{"name": "lo", "addresses": ["127.0.0.1"]}], poll_interval=.02)
        self.service.configure({"revision": 0, "enabled": True, "interface": "lo", "port": self.port})
        self.wait(lambda: self.service.snapshot()["status_code"] == "network_scanner_ready")

    def wait(self, condition, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(.02)
        self.fail("Virtual network scanner did not reach its expected state: " + str(self.service.snapshot()))

    def request(self, method, path, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        headers = {"Content-Type": "text/xml"} if body is not None else {}
        try:
            connection.request(method, path, body, headers)
            response = connection.getresponse()
            return response.status, {key.lower(): value for key, value in response.getheaders()}, response.read()
        finally:
            connection.close()

    @staticmethod
    def ticket(media_type="image/png", resolution=75, width=300, height=360):
        return (f'<?xml version="1.0"?><scan:ScanSettings xmlns:scan="http://schemas.hp.com/imaging/escl/2011/05/03" '
                f'xmlns:pwg="http://www.pwg.org/schemas/2010/12/sm"><pwg:Version>2.63</pwg:Version>'
                f'<scan:Intent>Document</scan:Intent><scan:ScanRegions><scan:ScanRegion>'
                f'<pwg:ContentRegionUnits>escl:ThreeHundredthsOfInches</pwg:ContentRegionUnits>'
                f'<pwg:XOffset>0</pwg:XOffset><pwg:YOffset>0</pwg:YOffset><pwg:Width>{width}</pwg:Width>'
                f'<pwg:Height>{height}</pwg:Height></scan:ScanRegion></scan:ScanRegions>'
                f'<pwg:InputSource>Platen</pwg:InputSource><scan:ColorMode>RGB24</scan:ColorMode>'
                f'<scan:XResolution>{resolution}</scan:XResolution><scan:YResolution>{resolution}</scan:YResolution>'
                f'<pwg:DocumentFormat>{media_type}</pwg:DocumentFormat></scan:ScanSettings>').encode()

    def create_job(self, **options):
        status, headers, body = self.request("POST", "/eSCL/ScanJobs", self.ticket(**options))
        self.assertEqual(status, 201, (headers, body))
        return urlsplit(headers["location"]).path

    def assert_lock_free(self):
        with self.lock.open("rb") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def direct_virtual_size(self, width=300, height=360):
        """Use the backend's actual fixed-point/quantized dimensions as reference.

        SANE's 16.16 millimetres cannot represent every requested coordinate
        exactly. Comparing with a direct acquisition prevents inventing an
        extra row or column merely to match idealized arithmetic.
        """
        environment = {**os.environ, "SANE_CONFIG_DIR": str(self.sane)}
        with self.lock.open("rb") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = subprocess.run(["scanimage", "--device-name=test:0", "--format=pnm", "--resolution=75",
                                     "--mode=Color", "-x", str(width * 25.4 / 300), "-y", str(height * 25.4 / 300)],
                                    env=environment, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        with Image.open(BytesIO(result.stdout)) as image:
            image.load()
            return image.size

    def tearDown(self):
        if self.service:
            self.service.close()
        self.storage.close()
        self.temporary.cleanup()

    def test_capabilities_status_and_idle_use_no_device_lock(self):
        self.start()
        status, headers, data = self.request("GET", "/eSCL/ScannerCapabilities")
        self.assertEqual(status, 200)
        self.assertIn(b"ScannerCapabilities", data)
        self.assertIn(b"600", data)
        self.assertNotIn(b">1200<", data)
        self.assertEqual(self.request("GET", "/eSCL/ScannerStatus")[0], 200)
        self.assert_lock_free()
        self.assertFalse(self.service.snapshot()["hardware_verified"])

    def test_three_formats_are_real_complete_virtual_images(self):
        self.start()
        expected_size = self.direct_virtual_size()
        for media_type in ("image/png", "image/jpeg", "application/pdf"):
            with self.subTest(media_type=media_type):
                job = self.create_job(media_type=media_type)
                status, headers, content = self.request("GET", job + "/NextDocument")
                self.assertEqual(status, 200, (headers, content[:100]))
                self.assertEqual(headers["content-type"], media_type)
                if media_type == "application/pdf":
                    self.assertEqual(len(PdfReader(BytesIO(content)).pages), 1)
                else:
                    with Image.open(BytesIO(content)) as image:
                        image.load()
                        self.assertEqual(image.size, expected_size)
                        self.assertEqual(image.mode, "RGB")
                self.wait(lambda: not (self.service.directory / "lease.json").exists())
                self.assert_lock_free()

    def test_shared_lock_busy_and_oversized_tickets_do_not_start_acquisition(self):
        self.start()
        job = self.create_job()
        with self.lock.open("rb") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.request("GET", job + "/NextDocument")[0], 503)
            self.assertFalse((self.service.directory / "lease.json").exists())
        # Upstream represents an invalid ticket as an immediately aborted job
        # resource. Prove its terminal state and refusal to acquire an image.
        invalid_job = self.create_job(resolution=1200)
        status, _, state = self.request("GET", "/eSCL/ScannerStatus")
        self.assertEqual(status, 200)
        jobs = [entry for entry in ElementTree.fromstring(state).iter() if entry.tag.endswith("}JobInfo")]
        states = {entry.find("{http://www.pwg.org/schemas/2010/12/sm}JobUri").text:
                  entry.find("{http://www.pwg.org/schemas/2010/12/sm}JobState").text for entry in jobs}
        self.assertEqual(states[invalid_job], "Aborted")
        self.assertEqual(self.request("GET", invalid_job + "/NextDocument")[0], 404)
        self.assertFalse((self.service.directory / "lease.json").exists())
        self.assert_lock_free()

    def test_connection_limit_recovers_after_slow_clients_disconnect(self):
        self.start()
        sockets = []
        try:
            for _ in range(8):
                connection = socket.create_connection(("127.0.0.1", self.port), timeout=3)
                connection.sendall(b"GET /eSCL/ScannerStatus HTTP/1.1\r\n")
                sockets.append(connection)
            time.sleep(.2)
            extra = socket.create_connection(("127.0.0.1", self.port), timeout=3)
            try:
                extra.settimeout(3)
                try:
                    self.assertEqual(extra.recv(1), b"")
                except ConnectionResetError:
                    pass
            finally:
                extra.close()
        finally:
            for connection in sockets:
                connection.close()
        time.sleep(.1)
        self.assertEqual(self.request("GET", "/eSCL/ScannerCapabilities")[0], 200)
        self.assert_lock_free()

    def test_pending_job_limit_bounds_worker_threads_without_acquiring_images(self):
        self.start()
        status_path = Path("/proc") / str(self.service._process.pid) / "status"
        def thread_count():
            return int(next(line.split()[1] for line in status_path.read_text().splitlines()
                            if line.startswith("Threads:")))
        baseline = thread_count()
        accepted = [self.create_job() for _ in range(16)]
        self.assertEqual(len(set(accepted)), 16)
        self.assertEqual(self.request("POST", "/eSCL/ScanJobs", self.ticket())[0], 503)
        self.assertLessEqual(thread_count(), baseline + 16 + 3)
        self.assertFalse((self.service.directory / "lease.json").exists())
        self.assert_lock_free()

    def test_cancel_releases_native_handle_and_allows_a_subsequent_scan(self):
        self.start(slow=True)
        job = self.create_job(media_type="image/jpeg", width=900, height=900)
        received = []
        def download():
            try:
                received.append(self.request("GET", job + "/NextDocument"))
            except (OSError, http.client.HTTPException) as error:
                received.append(type(error).__name__)
        worker = threading.Thread(target=download)
        worker.start()
        try:
            self.wait(lambda: self.service.snapshot()["active_operation"] == "session")
            self.assertEqual(self.request("DELETE", job)[0], 200)
            worker.join(10)
            self.assertFalse(worker.is_alive())
            self.wait(lambda: not (self.service.directory / "lease.json").exists())
            self.assert_lock_free()
            expected_size = self.direct_virtual_size(width=120, height=120)
            next_job = self.create_job(media_type="image/png", width=120, height=120)
            status, _, content = self.request("GET", next_job + "/NextDocument")
            self.assertEqual(status, 200)
            with Image.open(BytesIO(content)) as image:
                image.load()
                self.assertEqual(image.size, expected_size)
        finally:
            if worker.is_alive():
                self.service.close()
                worker.join(10)

    @unittest.skipUnless(shutil.which("scanimage"), "sane-utils is needed for the actual eSCL client")
    def test_sane_airscan_client_receives_native_test_backend_pixels(self):
        self.start()
        client = self.root / "client-sane"
        client.mkdir()
        (client / "dll.conf").write_text("airscan\n")
        (client / "airscan.conf").write_text(
            f'[devices]\n"Virtual Brother" = http://127.0.0.1:{self.port}/eSCL/, eSCL\n'
            '[options]\ndiscovery = disable\nws-discovery = off\n')
        environment = {**os.environ, "SANE_CONFIG_DIR": str(client)}
        result = subprocess.run(["scanimage", "--device-name=airscan:e0:Virtual Brother", "--format=pnm",
                                 "--resolution=75", "--mode=Color", "-x", "25.4", "-y", "30.48"],
                                env=environment, capture_output=True, timeout=45)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        with Image.open(BytesIO(result.stdout)) as image:
            image.load()
            self.assertEqual(image.mode, "RGB")
            self.assertEqual(image.size, (75, 90))
            self.assertGreater(len(set(image.getdata())), 2)
        self.wait(lambda: not (self.service.directory / "lease.json").exists())
        self.assert_lock_free()


if __name__ == "__main__":
    unittest.main()

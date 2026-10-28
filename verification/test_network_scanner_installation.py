# SPDX-License-Identifier: GPL-2.0-or-later
"""Exercise optional root installer boundaries using ordinary temporary files."""

import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import runpy
import shutil
import tarfile
import tempfile
import unittest


PROJECT = Path(__file__).resolve().parents[1]
BUILDER = runpy.run_path(str(PROJECT / "scripts/build-network-scanner.py"))
INSTALLER = runpy.run_path(str(PROJECT / "scripts/install-network-scanner.py"))


class NetworkScannerInstallationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.build = self.root / "build"
        self.destination = self.root / "installation"
        (self.build / "bin").mkdir(parents=True)
        # The installer only reads these native ELF bytes; this executable is
        # never run and does not pretend to be a working scanner server.
        shutil.copyfile("/proc/self/exe", self.build / "bin/airsaned")
        sources = self.build / "sources/AirSane"
        (sources / "network_scanner").mkdir(parents=True)
        (sources / "LICENSE").write_text("Temporary license fixture\n")
        for name in ("device_access.h", "device_access.cpp"):
            (sources / "network_scanner" / name).write_text("// Temporary source fixture\n")
        (self.build / "shared-device-access.patch").write_text("Temporary patch fixture\n")
        checksum = lambda file: hashlib.sha256(file.read_bytes()).hexdigest()
        self.manifest = {"format": 1, "model": "Brother DCP-150C", "protocol": "eSCL",
            "source": {"revision": BUILDER["SOURCE"]["revision"]}, "shared_device_access": True,
            "binary_sha256": checksum(self.build / "bin/airsaned"),
            "patch_sha256": checksum(self.build / "shared-device-access.patch"),
            "helper_sha256": {name: checksum(sources / "network_scanner" / name)
                              for name in ("device_access.h", "device_access.cpp")}}
        self.save_manifest()

    def save_manifest(self):
        (self.build / "build-information.json").write_text(json.dumps(self.manifest))

    def install(self):
        INSTALLER["install_network_scanner"](self.build, self.destination)

    def tearDown(self):
        self.temporary.cleanup()

    def test_staged_install_keeps_sources_and_does_not_enable_system_services(self):
        self.install()
        binary = self.destination / "usr/lib/dcp150c-native/network-scanner/airsaned"
        self.assertTrue(binary.is_file())
        self.assertEqual(binary.stat().st_mode & 0o777, 0o755)
        data = self.destination / "usr/share/dcp150c-native/network-scanner"
        self.assertTrue((data / "sources/LICENSE").is_file())
        self.assertTrue((data / "sources/network_scanner/device_access.cpp").is_file())
        self.assertEqual((self.destination / "etc/dcp150c-native/network-scanner/sane/dll.conf").read_text(), "brother2\n")
        self.assertFalse((self.destination / "etc/sane.d").exists())
        self.assertFalse((self.destination / "etc/systemd").exists())

    def test_changed_binary_and_source_are_rejected_before_copying(self):
        for key in ("binary_sha256", "patch_sha256"):
            with self.subTest(key=key):
                expected = self.manifest[key]
                self.manifest[key] = "0" * 64
                self.save_manifest()
                with self.assertRaises(ValueError):
                    self.install()
                self.assertFalse(self.destination.exists())
                self.manifest[key] = expected
        self.save_manifest()
        (self.build / "sources/AirSane/network_scanner/device_access.h").write_text("changed")
        with self.assertRaises(ValueError):
            self.install()
        self.assertFalse(self.destination.exists())

    def test_private_installer_umask_does_not_hide_public_runtime_directories(self):
        previous = os.umask(0o077)
        try:
            self.install()
        finally:
            os.umask(previous)
        for relative in ("usr/lib/dcp150c-native/network-scanner", "usr/share/dcp150c-native/network-scanner/sources/network_scanner",
                         "etc/dcp150c-native/network-scanner/sane"):
            current = self.destination / relative
            while current != self.destination:
                self.assertEqual(current.stat().st_mode & 0o777, 0o755, str(current))
                current = current.parent

    def test_another_backend_configuration_is_preserved(self):
        configuration = self.destination / "etc/dcp150c-native/network-scanner/sane/dll.conf"
        configuration.parent.mkdir(parents=True)
        configuration.write_text("another-backend\n")
        with self.assertRaises(ValueError):
            self.install()
        self.assertEqual(configuration.read_text(), "another-backend\n")
        self.assertFalse((self.destination / "usr").exists())

    def test_symbolic_link_destination_cannot_escape_staging(self):
        outside = self.root / "outside"
        outside.mkdir()
        (self.destination / "usr").mkdir(parents=True)
        (self.destination / "usr/lib").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.install()
        self.assertEqual(list(outside.iterdir()), [])

    def test_archive_links_and_parent_paths_are_not_extracted(self):
        for name, is_link in (("source/../../outside", False), ("source/link", True)):
            with self.subTest(name=name):
                archive = self.root / "source.tar.gz"
                with tarfile.open(archive, "w:gz") as output:
                    entry = tarfile.TarInfo(name)
                    if is_link:
                        entry.type = tarfile.SYMTYPE
                        entry.linkname = "/tmp"
                        output.addfile(entry)
                    else:
                        entry.size = 1
                        output.addfile(entry, BytesIO(b"x"))
                with self.assertRaises(ValueError):
                    BUILDER["extract_sources"](archive, self.root / "extracted")
                self.assertFalse((self.root / "extracted").exists())


if __name__ == "__main__":
    unittest.main()

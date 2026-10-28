# SPDX-License-Identifier: GPL-2.0-or-later
"""Read real temporary FAT images; never open a system disk, mount or USB device."""
from contextlib import contextmanager
import fcntl
import hashlib
import os
from pathlib import Path
import shutil
import stat
import struct
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid

from dcp150c.media_reader import MediaError, MediaReaderService, _Candidate, _FatVolume


FORMAT_PROGRAM = shutil.which("mkfs.fat") or ("/usr/sbin/mkfs.fat" if Path("/usr/sbin/mkfs.fat").is_file() else None)
FAT_TOOLS_AVAILABLE = bool(FORMAT_PROGRAM and shutil.which("mcopy") and shutil.which("mmd"))


def create_fat_image(directory: Path, fat_bits: int = 16) -> Path:
    """Create a unique regular-file fixture, never a pre-existing device/path."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    image = directory / ("card-" + uuid.uuid4().hex + ".img")
    assert not image.exists() and not str(image).startswith("/dev/")
    sizes = {12: 2048, 16: 8192, 32: 65536}
    subprocess.run([FORMAT_PROGRAM, "-C", "-F", str(fat_bits), "-s", "1", "--invariant", "-n", "TESTCARD", str(image), str(sizes[fat_bits])],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=30)
    assert stat.S_ISREG(image.stat().st_mode)
    return image


def image_directory(image: Path, destination: str) -> None:
    assert stat.S_ISREG(image.lstat().st_mode) and not str(image).startswith("/dev/")
    subprocess.run(["mmd", "-i", str(image), "::" + destination], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=30,
                   env={**os.environ, "LC_ALL": "C.UTF-8"})


def image_copy(image: Path, source: Path, destination: str) -> None:
    assert stat.S_ISREG(image.lstat().st_mode) and not str(image).startswith("/dev/")
    subprocess.run(["mcopy", "-i", str(image), str(source), "::" + destination], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=30,
                   env={**os.environ, "LC_ALL": "C.UTF-8"})


def image_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


@unittest.skipUnless(FAT_TOOLS_AVAILABLE, "Install dosfstools and mtools for isolated regular-file fixtures")
class CardFilesystems(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="dcp150c-card-check-")
        self.root = Path(self.temporary.name)
        self.lock = self.root / "device.lock"
        self.lock.touch()

    def tearDown(self):
        self.temporary.cleanup()

    def reader(self, image):
        return MediaReaderService(str(self.lock), allow_test_backend=True, test_image_path=image)

    def populated(self, fat_bits=16):
        image = create_fat_image(self.root, fat_bits)
        image_directory(image, "/DCIM")
        image_directory(image, "/DCIM/100CAMERA")
        source = self.root / "source.bin"
        content = b"Generated card document\n" + bytes(range(256)) * 1200
        source.write_bytes(content)
        image_copy(image, source, "/DCIM/100CAMERA/Foto Grüße 2026.jpg")
        image_copy(image, source, "/Original scan.png")
        image_copy(image, source, "/Notes.txt")
        return image, content

    def find(self, reader, name, directory_id=None):
        listing = reader.list_directory(directory_id)
        return next(entry for entry in listing["entries"] if entry["name"] == name)

    def test_all_three_fat_types_real_unicode_folders_and_original_bytes(self):
        for fat_bits in (12, 16, 32):
            with self.subTest(filesystem=fat_bits):
                image, content = self.populated(fat_bits)
                before = image_digest(image)
                reader = self.reader(image)
                capabilities = reader.probe()
                self.assertTrue(capabilities["available"], capabilities)
                self.assertEqual(capabilities["filesystem"], f"FAT{fat_bits}")
                self.assertTrue(capabilities["read_only"])
                self.assertFalse(capabilities["hardware_verified"])
                root = reader.list_directory()
                self.assertEqual(root["directory_id"], capabilities["root_directory_id"])
                self.assertIsNone(root["parent_directory_id"])
                dcim = self.find(reader, "DCIM")
                folder = reader.list_directory(dcim["id"])
                self.assertEqual(folder["parent_directory_id"], root["directory_id"])
                camera = self.find(reader, "100CAMERA", dcim["id"])
                photo = self.find(reader, "Foto Grüße 2026.jpg", camera["id"])
                output = self.root / f"copied-{fat_bits}.jpg"
                result = reader.copy_file(photo["id"], output)
                self.assertEqual(output.read_bytes(), content)
                self.assertEqual(result["sha256"], hashlib.sha256(content).hexdigest())
                self.assertEqual(result["name"], "Foto Grüße 2026.jpg")
                self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
                self.assertEqual(image_digest(image), before, "Source card bytes must never change")
                self.assertNotIn(str(image), str(capabilities) + str(root) + str(folder) + str(result))

    def test_cached_capabilities_do_not_enumerate_or_read_media(self):
        reader = self.reader(self.root / "absent.img")
        with patch.object(reader, "_discover", side_effect=AssertionError("Implicit device discovery")):
            self.assertEqual(reader.capabilities()["reason_code"], "media_not_checked")
        self.assertEqual(reader.probe()["reason_code"], "media_not_found")

    def test_pages_keep_opaque_ids_and_unsupported_files_are_not_importable(self):
        image, _ = self.populated()
        reader = self.reader(image)
        reader.probe()
        first = reader.list_directory(offset=0, limit=1)
        second = reader.list_directory(offset=1, limit=1)
        self.assertEqual(first["entry_count"], 3)
        self.assertEqual(first["next_offset"], 1)
        self.assertNotEqual(first["entries"][0]["id"], second["entries"][0]["id"])
        self.assertEqual(first["entries"], reader.list_directory(offset=0, limit=1)["entries"])
        notes = self.find(reader, "Notes.txt")
        self.assertFalse(notes["supported"])
        with self.assertRaises(MediaError) as failure:
            reader.copy_file(notes["id"], self.root / "unsupported.txt")
        self.assertEqual(failure.exception.code, "unsupported_document")
        self.assertFalse((self.root / "unsupported.txt").exists())
        with self.assertRaises(MediaError):
            reader.list_directory("../../etc")

    def test_replacement_or_removal_invalidates_old_volume_and_entry_identifiers(self):
        image, _ = self.populated()
        reader = self.reader(image)
        old_volume = reader.probe()["volume_id"]
        old_file = self.find(reader, "Original scan.png")
        replacement, _ = self.populated()
        replacement.replace(image)
        with self.assertRaises(MediaError) as failure:
            reader.copy_file(old_file["id"], self.root / "stale.png")
        self.assertEqual(failure.exception.code, "media_changed")
        self.assertFalse(reader.capabilities()["available"])
        self.assertNotEqual(reader.probe()["volume_id"], old_volume)
        image.unlink()
        with self.assertRaises(MediaError) as failure:
            reader.list_directory()
        self.assertEqual(failure.exception.code, "media_changed")

    def test_copy_uses_read_only_source_and_never_overwrites_existing_destinations(self):
        image, _ = self.populated()
        reader = self.reader(image)
        opened = []
        original_open = os.open
        def record_open(path, flags, *args, **kwargs):
            if Path(path) == image:
                opened.append(flags)
                self.assertEqual(flags & os.O_ACCMODE, os.O_RDONLY)
            return original_open(path, flags, *args, **kwargs)
        with patch("dcp150c.media_reader.os.open", record_open):
            reader.probe()
            entry = self.find(reader, "Original scan.png")
            reader.copy_file(entry["id"], self.root / "new.png")
            existing = self.root / "existing.png"
            existing.write_bytes(b"keep existing bytes")
            with self.assertRaises(MediaError) as failure:
                reader.copy_file(entry["id"], existing)
            self.assertEqual(failure.exception.code, "media_destination_exists")
            self.assertEqual(existing.read_bytes(), b"keep existing bytes")
            link = self.root / "linked.png"
            link.symlink_to(existing)
            with self.assertRaises(MediaError):
                reader.copy_file(entry["id"], link)
            self.assertEqual(existing.read_bytes(), b"keep existing bytes")
        self.assertTrue(opened)

    def test_file_size_and_cancelled_lock_wait_leave_no_partial_copy(self):
        image, content = self.populated()
        reader = self.reader(image)
        reader.probe()
        entry = self.find(reader, "Original scan.png")
        with self.assertRaises(MediaError) as failure:
            reader.copy_file(entry["id"], self.root / "too-large.png", maximum_bytes=len(content) - 1)
        self.assertEqual(failure.exception.code, "media_file_too_large")
        descriptor = os.open(self.lock, os.O_RDWR)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        cancelled = threading.Event()
        timer = threading.Timer(0.15, cancelled.set)
        timer.start()
        try:
            with self.assertRaises(MediaError) as failure:
                reader.copy_file(entry["id"], self.root / "cancelled.png", cancelled)
            self.assertEqual(failure.exception.code, "job_cancelled")
        finally:
            os.close(descriptor)
            timer.join()
        self.assertFalse((self.root / "cancelled.png").exists())
        self.assertFalse((self.root / "too-large.png").exists())

    def test_cancel_during_streaming_removes_partial_destination(self):
        image, _ = self.populated()
        reader = self.reader(image)
        reader.probe()
        entry = self.find(reader, "Original scan.png")
        event = threading.Event()
        destination = self.root / "partial.png"
        original_read = _FatVolume.read_at
        streamed = []
        def cancel_after_chunk(volume, offset, length):
            data = original_read(volume, offset, length)
            if destination.exists():
                streamed.append(length)
                event.set()
            return data
        with patch.object(_FatVolume, "read_at", cancel_after_chunk):
            with self.assertRaises(MediaError) as failure:
                reader.copy_file(entry["id"], destination, event)
        self.assertEqual(failure.exception.code, "job_cancelled")
        self.assertTrue(streamed)
        self.assertFalse(destination.exists())

    def test_single_primary_mbr_partition_is_read_without_loop_or_mount(self):
        filesystem, content = self.populated(12)
        disk = self.root / "whole-disk.img"
        offset = 1024 * 1024
        with disk.open("xb") as output:
            output.truncate(offset + filesystem.stat().st_size)
            boot = bytearray(512)
            boot[446 + 4] = 0x01
            struct.pack_into("<II", boot, 446 + 8, offset // 512, filesystem.stat().st_size // 512)
            boot[510:512] = b"\x55\xaa"
            output.seek(0)
            output.write(boot)
            output.seek(offset)
            with filesystem.open("rb") as source:
                shutil.copyfileobj(source, output)
        reader = self.reader(disk)
        self.assertTrue(reader.probe()["available"])
        entry = self.find(reader, "Original scan.png")
        reader.copy_file(entry["id"], self.root / "partition-copy.png")
        self.assertEqual((self.root / "partition-copy.png").read_bytes(), content)

    def test_replacement_during_copy_discards_the_completed_private_output(self):
        image, _ = self.populated()
        replacement, _ = self.populated()
        reader = self.reader(image)
        reader.probe()
        entry = self.find(reader, "Original scan.png")
        destination = self.root / "changed-mid-copy.png"
        original_read = _FatVolume.read_at
        replaced = []
        def replace_during_stream(volume, offset, length):
            result = original_read(volume, offset, length)
            if destination.exists() and not replaced:
                replacement.replace(image)
                replaced.append(True)
            return result
        with patch.object(_FatVolume, "read_at", replace_during_stream):
            with self.assertRaises(MediaError) as failure:
                reader.copy_file(entry["id"], destination)
        self.assertEqual(failure.exception.code, "media_changed")
        self.assertTrue(replaced)
        self.assertFalse(destination.exists())
        self.assertFalse(reader.capabilities()["available"])

    def test_cancelled_operation_wait_does_not_wait_for_another_reader(self):
        image, _ = self.populated()
        reader = self.reader(image)
        reader.probe()
        event, finished = threading.Event(), threading.Event()
        errors = []
        def waiting_read():
            try:
                reader.list_directory(cancel_event=event)
            except MediaError as error:
                errors.append(error.code)
            finally:
                finished.set()
        reader._operations.acquire()
        worker = threading.Thread(target=waiting_read)
        try:
            worker.start()
            event.set()
            self.assertTrue(finished.wait(2))
            self.assertEqual(errors, ["job_cancelled"])
        finally:
            reader._operations.release()
            worker.join(5)

    def test_corrupt_geometry_truncated_volume_and_unknown_filesystem_are_unavailable(self):
        image = create_fat_image(self.root, 12)
        with image.open("r+b") as handle:
            handle.seek(19)
            handle.write(struct.pack("<H", 65535))
        self.assertEqual(self.reader(image).probe()["reason_code"], "media_corrupt")
        image = create_fat_image(self.root, 16)
        with image.open("r+b") as handle:
            handle.truncate(1000)
        self.assertEqual(self.reader(image).probe()["reason_code"], "media_corrupt")
        unknown = self.root / "unknown.img"
        unknown.write_bytes(b"Not a FAT volume" + bytes(1024))
        self.assertEqual(self.reader(unknown).probe()["reason_code"], "media_unsupported")

    def test_corrupt_file_chain_and_circular_directory_are_rejected(self):
        image, _ = self.populated(16)
        reader = self.reader(image)
        reader.probe()
        photo = self.find(reader, "Original scan.png")
        cluster = reader._records[photo["id"]].entry.first_cluster
        with image.open("r+b") as handle:
            boot = handle.read(512)
            fat_start = struct.unpack_from("<H", boot, 14)[0] * 512
            handle.seek(fat_start + cluster * 2)
            handle.write(struct.pack("<H", cluster))
        reader.probe()
        photo = self.find(reader, "Original scan.png")
        with self.assertRaises(MediaError) as failure:
            reader.copy_file(photo["id"], self.root / "loop.png")
        self.assertEqual(failure.exception.code, "media_corrupt")
        self.assertFalse((self.root / "loop.png").exists())
        image, _ = self.populated(16)
        reader = self.reader(image)
        reader.probe()
        folder = self.find(reader, "DCIM")
        first_cluster = reader._records[folder["id"]].first_cluster
        descriptor = os.open(image, os.O_RDONLY)
        try:
            volume = _FatVolume(descriptor, image.stat().st_size, threading.Event(), time.monotonic() + 30)
            data_offset, cluster_bytes = volume._cluster_offset(first_cluster), volume.cluster_bytes
        finally:
            os.close(descriptor)
        with image.open("r+b") as handle:
            boot = handle.read(512)
            fat_start = struct.unpack_from("<H", boot, 14)[0] * 512
            handle.seek(fat_start + first_cluster * 2)
            handle.write(struct.pack("<H", first_cluster))
            handle.seek(data_offset)
            handle.write((b"\xe5" + bytes(31)) * (cluster_bytes // 32))
        reader.probe()
        folder = self.find(reader, "DCIM")
        with self.assertRaises(MediaError) as failure:
            reader.list_directory(folder["id"])
        self.assertEqual(failure.exception.code, "media_corrupt")

    def test_fat32_uses_the_explicitly_active_allocation_table(self):
        image, content = self.populated(32)
        with image.open("r+b") as handle:
            boot = handle.read(512)
            fat_start = struct.unpack_from("<H", boot, 14)[0] * 512
            fat_bytes = struct.unpack_from("<I", boot, 36)[0] * 512
            handle.seek(40)
            handle.write(struct.pack("<H", 0x81))
            handle.seek(fat_start)
            handle.write(bytes(fat_bytes))
        reader = self.reader(image)
        self.assertTrue(reader.probe()["available"])
        entry = self.find(reader, "Original scan.png")
        reader.copy_file(entry["id"], self.root / "active-fat.png")
        self.assertEqual((self.root / "active-fat.png").read_bytes(), content)

    def test_invalid_long_name_checksum_falls_back_to_the_real_short_name(self):
        image, content = self.populated(16)
        reader = self.reader(image)
        reader.probe()
        entry = self.find(reader, "Original scan.png")
        checks = reader._records[entry["id"]].entry.checks
        self.assertGreater(len(checks), 1)
        with image.open("r+b") as handle:
            handle.seek(checks[0][0] + 13)
            handle.write(bytes([checks[0][1][13] ^ 0xff]))
        reader.probe()
        root = reader.list_directory()
        fallback = next(item for item in root["entries"] if item["name"].lower().endswith(".png"))
        self.assertNotEqual(fallback["name"], "Original scan.png")
        reader.copy_file(fallback["id"], self.root / "fallback.png")
        self.assertEqual((self.root / "fallback.png").read_bytes(), content)


class ProductionSourceBoundaries(unittest.TestCase):
    def test_mount_and_superblock_options_must_both_be_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mountinfo = root / "mountinfo"
            reader = MediaReaderService(test_image_path=root / "unused.img",
                mountinfo_path=mountinfo, allow_test_backend=True)
            candidate = _Candidate(root / "unused.img", 1024, (), "fixture", "8:1", True)
            unrelated = "1 0 0:1 / / rw - tmpfs - rw\n"
            for mount_options, superblock_options, writable in (
                    ("rw,nosuid", "rw", True), ("ro,nosuid", "rw,errors=remount-ro", True),
                    ("rw", "ro", True), ("ro,nosuid", "ro,errors=remount-ro", False)):
                with self.subTest(mount_options=mount_options, superblock_options=superblock_options):
                    mountinfo.write_text(unrelated + f"11 1 8:1 / /card {mount_options} shared:3 master:2 - vfat /dev/sdx1 {superblock_options}\n")
                    self.assertEqual(reader._mounted_writable(candidate), writable)

    def test_invalid_or_unreadable_mount_information_never_grants_access(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mountinfo = root / "mountinfo"
            reader = MediaReaderService(test_image_path=root / "unused.img",
                mountinfo_path=mountinfo, allow_test_backend=True)
            candidate = _Candidate(root / "unused.img", 1024, (), "fixture", "8:1", True)
            readonly = "11 1 8:1 / /card ro - vfat /dev/sdx1 ro\n"
            malformed = ("", "11 1 8:1 / /card ro vfat /dev/sdx1 ro\n",
                "11 1 8:1 / /card ro - vfat /dev/sdx1\n",
                "11 1 broken / /card ro - vfat /dev/sdx1 ro\n",
                "11 1 8:1 / /card ro,rw - vfat /dev/sdx1 ro\n",
                "11 1 8:1 / /card ro - vfat /dev/sdx1 errors=remount-ro\n",
                readonly + "unparseable record\n")
            for content in malformed:
                with self.subTest(content=content):
                    mountinfo.write_text(content)
                    with self.assertRaises(MediaError) as failure:
                        reader._mounted_writable(candidate)
                    self.assertEqual(failure.exception.code, "media_access_denied")
            mountinfo.write_bytes(b"\xff")
            with self.assertRaises(MediaError):
                reader._mounted_writable(candidate)
            mountinfo.unlink()
            with self.assertRaises(MediaError):
                reader._mounted_writable(candidate)

    def test_test_roots_and_regular_sources_require_explicit_test_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(ValueError):
                MediaReaderService(test_image_path=root / "card.img")
            with self.assertRaises(ValueError):
                MediaReaderService(sysfs_root=root / "sys", device_root=root / "dev")
            with self.assertRaises(ValueError):
                MediaReaderService(allow_test_backend=True)
            image = root / "card.img"
            image.write_bytes(bytes(1024))
            candidate = _Candidate(image, 1024, (), "fixture", None, True)
            reader = MediaReaderService()
            with self.assertRaises(MediaError) as failure:
                with reader._open(candidate):
                    self.fail("A production reader must refuse a regular file")
            self.assertEqual(failure.exception.code, "media_access_denied")

    @unittest.skipUnless(FAT_TOOLS_AVAILABLE, "Install dosfstools and mtools")
    def test_exact_usb_parent_partition_nodes_and_writable_mount_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = create_fat_image(root, 12)
            sysfs, devices = root / "sys", root / "dev"
            block = sysfs / "devices" / "usb1" / "1-2" / "1-2:1.3" / "block" / "sdx"
            partition = block / "sdx1"
            partition.mkdir(parents=True)
            devices.mkdir()
            usb = sysfs / "devices" / "usb1" / "1-2"
            (usb / "idVendor").write_text("04f9\n")
            (usb / "idProduct").write_text("01cf\n")
            (usb / "serial").write_text("ISOLATED-FIXTURE\n")
            for path, numbers in ((block, "8:0"), (partition, "8:1")):
                (path / "size").write_text(str(image.stat().st_size // 512))
                (path / "dev").write_text(numbers)
            (block / "diskseq").write_text("14")
            (partition / "partition").write_text("1")
            class_root = sysfs / "class" / "block"
            class_root.mkdir(parents=True)
            (class_root / "sdx").symlink_to(block)
            (class_root / "sdx1").symlink_to(partition)
            (devices / "sdx").write_bytes(b"not the partition")
            shutil.copyfile(image, devices / "sdx1")
            mountinfo = root / "mountinfo"
            mountinfo.write_text("1 0 0:1 / / rw - tmpfs tmpfs rw\n")
            lock = root / "device.lock"
            lock.touch()
            reader = MediaReaderService(str(lock), sysfs_root=sysfs, device_root=devices,
                mountinfo_path=mountinfo, allow_test_backend=True)
            candidates = reader._discover()
            self.assertEqual([candidate.device_path.name for candidate in candidates], ["sdx1"])
            self.assertTrue(reader.probe()["available"])
            self.assertFalse(reader.capabilities()["hardware_verified"])
            mountinfo.write_text("11 1 8:1 / /mounted rw,nosuid - vfat /dev/sdx1 rw\n")
            self.assertEqual(reader.probe()["reason_code"], "media_in_use")
            mountinfo.write_text("1 0 0:1 / / rw - tmpfs tmpfs rw\n")
            (usb / "idProduct").write_text("01ce")
            self.assertEqual(reader.probe()["reason_code"], "media_not_found")


if __name__ == "__main__":
    unittest.main()

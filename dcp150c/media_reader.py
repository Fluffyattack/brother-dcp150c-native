# SPDX-License-Identifier: GPL-2.0-or-later
"""Read this printer's FAT memory cards without mounting or modifying them.

Only explicit ``probe``, ``list_directory`` and ``copy_file`` calls read a
device. The web layer receives opaque, generation-scoped identifiers rather
than paths. Production sources must be real block devices below USB 04f9:01cf.
Regular image files and replacement sysfs trees require an explicit test mode.

The parser handles FAT12, FAT16, FAT32 and Unicode long file names. It does not
repair filesystems or perform any write, mount, media-eject or printer command.
"""

from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
import copy
from dataclasses import dataclass
import errno
import fcntl
import hashlib
import math
import os
from pathlib import Path
import re
import stat
import struct
import threading
import time
import uuid


USB_VENDOR = "04f9"
USB_PRODUCT = "01cf"
SHARED_LOCK = "/run/lock/dcp150c-device.lock"
MAX_DIRECTORY_BYTES = 4 * 1024 * 1024
MAX_DIRECTORY_ENTRIES = 16384
MAX_KNOWN_ENTRIES = 32768
MAX_DIRECTORY_DEPTH = 32
SUPPORTED_FILES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".pdf": "application/pdf"}
IDENTIFIER = re.compile(r"[a-f0-9]{32}\Z")
DEVICE_NAME = re.compile(r"[A-Za-z0-9_.-]{1,64}\Z")


class MediaError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _cancelled(event: threading.Event, deadline: float) -> None:
    if event.is_set():
        raise MediaError("job_cancelled", "The memory-card operation was cancelled.")
    if time.monotonic() >= deadline:
        raise MediaError("processing_timeout", "Reading the memory card exceeded its time limit.")


def _corrupt() -> MediaError:
    return MediaError("media_corrupt", "The memory-card filesystem could not be read safely.")


@dataclass(frozen=True)
class _Candidate:
    device_path: Path
    size_bytes: int
    identity: tuple
    printer_identity: str
    device_numbers: str | None
    regular_fixture: bool


@dataclass(frozen=True)
class _Entry:
    name: str
    first_cluster: int
    size: int
    is_directory: bool
    checks: tuple[tuple[int, bytes], ...]


@dataclass(frozen=True)
class _Record:
    identifier: str
    entry: _Entry | None
    parent_identifier: str | None
    first_cluster: int | None
    name: str
    depth: int
    ancestor_clusters: tuple[int, ...]


class _FatVolume:
    """A bounded FAT view over an already opened read-only descriptor."""
    def __init__(self, descriptor: int, size_bytes: int, event: threading.Event, deadline: float) -> None:
        if fcntl.fcntl(descriptor, fcntl.F_GETFL) & os.O_ACCMODE != os.O_RDONLY:
            raise MediaError("media_access_denied", "The card source must be opened read-only.")
        self.descriptor, self.source_size = descriptor, size_bytes
        self.event, self.deadline = event, deadline
        self.volume_offset = 0
        self._fat_cache: OrderedDict[int, bytes] = OrderedDict()
        boot = self.read_at(0, 512)
        self._source_boot = boot
        try:
            self._read_geometry(boot, size_bytes)
        except MediaError as initial_error:
            # Normally the kernel exposes partition nodes. This also handles a
            # whole removable disk carrying one ordinary primary FAT partition.
            partitions = []
            if boot[510:512] == b"\x55\xaa":
                for position in range(446, 510, 16):
                    entry = boot[position:position + 16]
                    if entry[4] not in {0x01, 0x04, 0x06, 0x0b, 0x0c, 0x0e}:
                        continue
                    start, sectors = struct.unpack_from("<II", entry, 8)
                    offset, length = start * 512, sectors * 512
                    if start and sectors and offset + length <= size_bytes:
                        partitions.append((offset, length))
            if len(partitions) > 1:
                raise MediaError("media_selection_required", "More than one usable card partition is present.") from None
            if not partitions:
                raise initial_error
            self.volume_offset, partition_length = partitions[0]
            self._read_geometry(self.read_at(self.volume_offset, 512), partition_length)
        self._boot_bytes = self.read_at(self.volume_offset, self.bytes_per_sector)
        self.signature = hashlib.sha256(struct.pack("<QQ", self.volume_offset, self.volume_bytes) + self._boot_bytes).hexdigest()

    def read_at(self, offset: int, length: int) -> bytes:
        if offset < 0 or length < 0 or offset + length > self.source_size or length > MAX_DIRECTORY_BYTES:
            raise _corrupt()
        result = bytearray()
        while len(result) < length:
            _cancelled(self.event, self.deadline)
            try:
                chunk = os.pread(self.descriptor, min(65536, length - len(result)), offset + len(result))
            except OSError as error:
                raise MediaError("media_changed", "The memory card is no longer readable. Check it again.") from error
            if not chunk:
                raise _corrupt()
            result.extend(chunk)
        return bytes(result)

    def _read_geometry(self, boot: bytes, available_bytes: int) -> None:
        if (len(boot) != 512 or boot[510:512] != b"\x55\xaa" or boot[0] not in {0xeb, 0xe9}
                or boot[3:11] in {b"EXFAT   ", b"NTFS    "}):
            raise MediaError("media_unsupported", "Use a FAT12, FAT16 or FAT32 memory card.")
        self.bytes_per_sector = struct.unpack_from("<H", boot, 11)[0]
        self.sectors_per_cluster = boot[13]
        reserved = struct.unpack_from("<H", boot, 14)[0]
        copies = boot[16]
        self.root_entry_count = struct.unpack_from("<H", boot, 17)[0]
        total_sectors = struct.unpack_from("<H", boot, 19)[0] or struct.unpack_from("<I", boot, 32)[0]
        fat16_sectors = struct.unpack_from("<H", boot, 22)[0]
        fat32_sectors = struct.unpack_from("<I", boot, 36)[0]
        if (self.bytes_per_sector not in {512, 1024, 2048, 4096} or not reserved or not 1 <= copies <= 4
                or not self.sectors_per_cluster or self.sectors_per_cluster > 128
                or self.sectors_per_cluster & (self.sectors_per_cluster - 1)
                or not (boot[21] == 0xf0 or 0xf8 <= boot[21] <= 0xff)):
            raise MediaError("media_unsupported", "Use a supported FAT memory card.")
        fat_sectors = fat16_sectors or fat32_sectors
        self.root_sectors = math.ceil(self.root_entry_count * 32 / self.bytes_per_sector)
        self.root_sector = reserved + copies * fat_sectors
        self.data_sector = self.root_sector + self.root_sectors
        if not fat_sectors or total_sectors <= self.data_sector or total_sectors * self.bytes_per_sector > available_bytes:
            raise _corrupt()
        self.cluster_count = (total_sectors - self.data_sector) // self.sectors_per_cluster
        self.fat_bits = 32 if not fat16_sectors and fat32_sectors else 12 if self.cluster_count < 4085 else 16
        if not self.cluster_count or self.cluster_count > {12: 4084, 16: 65524, 32: 0x0ffffff4}[self.fat_bits]:
            raise _corrupt()
        if (self.cluster_count + 2) * self.fat_bits > fat_sectors * self.bytes_per_sector * 8:
            raise _corrupt()
        active_copy = 0
        if self.fat_bits == 32:
            flags, version = struct.unpack_from("<HH", boot, 40)
            if self.root_entry_count or version:
                raise MediaError("media_unsupported", "This FAT32 version is not supported.")
            if flags & 0x80:
                active_copy = flags & 0x0f
            if active_copy >= copies:
                raise _corrupt()
            self.root_cluster = struct.unpack_from("<I", boot, 44)[0] & 0x0fffffff
            if not 2 <= self.root_cluster <= self.cluster_count + 1:
                raise _corrupt()
            label = boot[71:82]
        else:
            if not self.root_entry_count:
                raise _corrupt()
            self.root_cluster = None
            label = boot[43:54]
        self.label = "".join(character for character in label.decode("cp437", "replace") if character.isprintable()).strip()
        if self.label == "NO NAME":
            self.label = ""
        self.cluster_bytes = self.bytes_per_sector * self.sectors_per_cluster
        self.fat_start = self.volume_offset + (reserved + active_copy * fat_sectors) * self.bytes_per_sector
        self.fat_bytes = fat_sectors * self.bytes_per_sector
        self.volume_bytes = total_sectors * self.bytes_per_sector

    def unchanged(self) -> bool:
        return (self.read_at(0, 512) == self._source_boot
                and self.read_at(self.volume_offset, self.bytes_per_sector) == self._boot_bytes)

    def _fat_value(self, cluster: int) -> int:
        if not 2 <= cluster <= self.cluster_count + 1:
            raise _corrupt()
        offset = cluster + cluster // 2 if self.fat_bits == 12 else cluster * (self.fat_bits // 8)
        size = 2 if self.fat_bits in {12, 16} else 4
        if offset + size > self.fat_bytes:
            raise _corrupt()
        data = bytearray()
        for position in range(offset, offset + size):
            sector = position // self.bytes_per_sector
            if sector not in self._fat_cache:
                self._fat_cache[sector] = self.read_at(self.fat_start + sector * self.bytes_per_sector, self.bytes_per_sector)
                if len(self._fat_cache) > 128:
                    self._fat_cache.popitem(last=False)
            data.append(self._fat_cache[sector][position % self.bytes_per_sector])
        value = int.from_bytes(data, "little")
        if self.fat_bits == 12:
            value = value >> 4 if cluster & 1 else value & 0xfff
        elif self.fat_bits == 32:
            value &= 0x0fffffff
        return value

    def _next_cluster(self, cluster: int) -> int | None:
        value = self._fat_value(cluster)
        end_marker = {12: 0xff8, 16: 0xfff8, 32: 0x0ffffff8}[self.fat_bits]
        reserved_marker = {12: 0xff0, 16: 0xfff0, 32: 0x0ffffff0}[self.fat_bits]
        if value >= end_marker:
            return None
        if value < 2 or value >= reserved_marker or value > self.cluster_count + 1:
            raise _corrupt()
        return value

    def _cluster_offset(self, cluster: int) -> int:
        if not 2 <= cluster <= self.cluster_count + 1:
            raise _corrupt()
        return self.volume_offset + (self.data_sector + (cluster - 2) * self.sectors_per_cluster) * self.bytes_per_sector

    def _directory_chunks(self, first_cluster: int | None):
        if first_cluster is None:
            remaining = self.root_entry_count * 32
            offset = self.volume_offset + self.root_sector * self.bytes_per_sector
            while remaining:
                size = min(65536, remaining)
                yield offset, self.read_at(offset, size)
                offset += size
                remaining -= size
            return
        seen, count = set(), 0
        cluster = first_cluster
        while cluster is not None:
            if cluster in seen:
                raise _corrupt()
            seen.add(cluster)
            count += self.cluster_bytes
            if count > MAX_DIRECTORY_BYTES:
                raise MediaError("media_directory_limit", "This card folder exceeds the supported listing limit.")
            offset = self._cluster_offset(cluster)
            yield offset, self.read_at(offset, self.cluster_bytes)
            cluster = self._next_cluster(cluster)

    @staticmethod
    def _safe_name(value: str) -> bool:
        return (bool(value) and value not in {".", ".."} and len(value.encode("utf-16-le")) <= 510
                and all(character.isprintable() and character not in '/\\:*?"<>|' for character in value))

    @staticmethod
    def _short_name(entry: bytes) -> str:
        value = (b"\xe5" + entry[1:11]) if entry[0] == 0x05 else entry[:11]
        name, extension = value[:8].rstrip(b" ").decode("cp437"), value[8:].rstrip(b" ").decode("cp437")
        if entry[12] & 8:
            name = name.lower()
        if entry[12] & 16:
            extension = extension.lower()
        return name + ("." + extension if extension else "")

    def entries(self, first_cluster: int | None) -> list[_Entry]:
        result, long_parts, long_checks = [], {}, []
        expected_order = 0
        checksum = None
        slots = 0
        for base, block in self._directory_chunks(first_cluster):
            for position in range(0, len(block), 32):
                _cancelled(self.event, self.deadline)
                slots += 1
                if slots * 32 > MAX_DIRECTORY_BYTES:
                    raise MediaError("media_directory_limit", "This card folder exceeds the supported listing limit.")
                entry = block[position:position + 32]
                if entry[0] == 0:
                    return result
                if entry[0] == 0xe5:
                    long_parts, long_checks, expected_order, checksum = {}, [], 0, None
                    continue
                if entry[11] == 0x0f:
                    order = entry[0] & 0x1f
                    if entry[0] & 0x40:
                        long_parts, long_checks, expected_order, checksum = {}, [], order, entry[13]
                    if (not 1 <= order <= 20 or order != expected_order or entry[13] != checksum
                            or entry[12] or entry[26:28] != b"\0\0" or entry[0] & 0xa0):
                        long_parts, long_checks, expected_order, checksum = {}, [], 0, None
                        continue
                    long_parts[order] = entry[1:11] + entry[14:26] + entry[28:32]
                    long_checks.append((base + position, entry))
                    expected_order -= 1
                    continue
                actual_checksum = 0
                for byte in entry[:11]:
                    actual_checksum = (((actual_checksum & 1) << 7) + (actual_checksum >> 1) + byte) & 0xff
                name = self._short_name(entry)
                valid_long_name = False
                if expected_order == 0 and long_parts and checksum == actual_checksum:
                    encoded = b"".join(long_parts[index] for index in range(1, len(long_parts) + 1))
                    units = [encoded[index:index + 2] for index in range(0, len(encoded), 2)]
                    if b"\0\0" in units:
                        end = units.index(b"\0\0")
                        valid_padding = all(unit == b"\xff\xff" for unit in units[end + 1:])
                        units = units[:end]
                    else:
                        valid_padding = b"\xff\xff" not in units
                    try:
                        candidate = b"".join(units).decode("utf-16-le")
                        if valid_padding and self._safe_name(candidate):
                            name, valid_long_name = candidate, True
                    except UnicodeError:
                        pass
                checks = tuple(long_checks) if valid_long_name else ()
                long_parts, long_checks, expected_order, checksum = {}, [], 0, None
                if entry[11] & 8 or name in {".", ".."}:
                    continue
                if entry[11] & 0xc0 or not self._safe_name(name):
                    raise _corrupt()
                cluster = struct.unpack_from("<H", entry, 26)[0]
                if self.fat_bits == 32:
                    cluster |= struct.unpack_from("<H", entry, 20)[0] << 16
                    cluster &= 0x0fffffff
                size = struct.unpack_from("<I", entry, 28)[0]
                directory = bool(entry[11] & 16)
                if (directory or size) and not 2 <= cluster <= self.cluster_count + 1:
                    raise _corrupt()
                result.append(_Entry(name, cluster, size, directory, checks + ((base + position, entry),)))
                if len(result) > MAX_DIRECTORY_ENTRIES:
                    raise MediaError("media_directory_limit", "This card folder contains too many entries to list safely.")
        return result

    def check_entry(self, entry: _Entry) -> None:
        if any(self.read_at(offset, len(expected)) != expected for offset, expected in entry.checks):
            raise MediaError("media_changed", "The selected card entry changed. Check the card again.")

    def file_clusters(self, entry: _Entry) -> list[tuple[int, int | None]]:
        needed = math.ceil(entry.size / self.cluster_bytes)
        result, seen, cluster = [], set(), entry.first_cluster
        for _ in range(needed):
            if cluster is None or cluster in seen:
                raise _corrupt()
            seen.add(cluster)
            following = self._next_cluster(cluster)
            result.append((cluster, following))
            cluster = following
        if cluster in seen:
            raise _corrupt()
        return result


class MediaReaderService:
    def __init__(self, lock_path: str = SHARED_LOCK, *, printer_serial: str = "",
                 sysfs_root: Path | str = "/sys", device_root: Path | str = "/dev",
                 mountinfo_path: Path | str = "/proc/self/mountinfo", allow_test_backend: bool = False,
                 test_image_path: Path | str | None = None) -> None:
        if not allow_test_backend and (Path(sysfs_root) != Path("/sys") or Path(device_root) != Path("/dev")
                or Path(mountinfo_path) != Path("/proc/self/mountinfo") or test_image_path is not None):
            raise ValueError("Test media sources require allow_test_backend=True")
        if allow_test_backend and test_image_path is None and Path(sysfs_root) == Path("/sys") and Path(device_root) == Path("/dev"):
            raise ValueError("Choose an explicit isolated test source")
        self.lock_path, self.printer_serial = Path(lock_path), printer_serial
        self.sysfs_root, self.device_root = Path(sysfs_root), Path(device_root)
        self.mountinfo_path = Path(mountinfo_path)
        self.allow_test_backend = allow_test_backend
        self.test_image_path = Path(test_image_path) if test_image_path is not None else None
        self._guard, self._operations = threading.RLock(), threading.RLock()
        self._candidate: _Candidate | None = None
        self._volume_signature = ""
        self._records: dict[str, _Record] = {}
        self._entry_identifiers: dict[tuple, str] = {}
        self._capabilities = {"available": False, "configured": True, "reason_code": "media_not_checked",
            "volume_id": None, "root_directory_id": None, "checked_at": None, "read_only": True,
            "hardware_verified": False, "filesystem": None, "label": "", "volume_bytes": None}

    def capabilities(self) -> dict:
        """Read cached state only; startup and browser polling perform no I/O."""
        with self._guard:
            return copy.deepcopy(self._capabilities)

    def _invalidate(self, reason: str, *, checked: bool = False) -> dict:
        with self._guard:
            self._candidate = None
            self._records.clear()
            self._entry_identifiers.clear()
            self._volume_signature = ""
            self._capabilities.update(available=False, reason_code=reason, volume_id=None,
                root_directory_id=None, hardware_verified=False, filesystem=None, label="", volume_bytes=None)
            if checked:
                self._capabilities["checked_at"] = time.time()
            return self.capabilities()

    @contextmanager
    def _operation_lock(self, event: threading.Event):
        deadline = time.monotonic() + 30
        while not self._operations.acquire(timeout=0.05):
            if event.is_set():
                raise MediaError("job_cancelled", "The memory-card operation was cancelled.")
            if time.monotonic() >= deadline:
                raise MediaError("device_busy", "Another memory-card operation is still running.")
        try:
            _cancelled(event, deadline)
            yield
        finally:
            self._operations.release()

    @contextmanager
    def _device_lock(self, event: threading.Event):
        try:
            descriptor = os.open(self.lock_path, os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW)
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                os.close(descriptor)
                raise OSError("Invalid shared lock")
        except OSError:
            raise MediaError("media_lock_unavailable", "The shared printer lock is unavailable.") from None
        deadline = time.monotonic() + 30
        acquired = False
        try:
            while True:
                _cancelled(event, deadline)
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except BlockingIOError:
                    event.wait(0.05)
            yield
        except MediaError as error:
            if error.code == "processing_timeout" and not acquired:
                raise MediaError("device_busy", "The printer is busy. Try the memory card again afterwards.") from None
            raise
        finally:
            os.close(descriptor)

    @staticmethod
    def _text(path: Path) -> str:
        return path.read_text(encoding="ascii").strip()

    def _discover(self) -> list[_Candidate]:
        if self.test_image_path is not None:
            try:
                metadata = self.test_image_path.lstat()
            except FileNotFoundError:
                return []
            if not stat.S_ISREG(metadata.st_mode):
                raise MediaError("media_access_denied", "The isolated card image is not a regular file.")
            identity = (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns)
            return [_Candidate(self.test_image_path, metadata.st_size, identity, "isolated-test-image", None, True)]
        candidates = []
        class_root = self.sysfs_root / "class" / "block"
        try:
            entries = list(class_root.iterdir())
        except OSError:
            return []
        for entry in entries:
            if not DEVICE_NAME.fullmatch(entry.name):
                continue
            try:
                resolved = entry.resolve(strict=True)
                resolved.relative_to(self.sysfs_root.resolve())
                printer = None
                for ancestor in (resolved, *resolved.parents):
                    if not ancestor.is_relative_to(self.sysfs_root):
                        break
                    if (ancestor / "idVendor").is_file() and (ancestor / "idProduct").is_file():
                        if self._text(ancestor / "idVendor").lower() == USB_VENDOR and self._text(ancestor / "idProduct").lower() == USB_PRODUCT:
                            serial = self._text(ancestor / "serial") if (ancestor / "serial").exists() else ""
                            if not self.printer_serial or serial == self.printer_serial:
                                printer = str(ancestor)
                        break
                if printer is None:
                    continue
                # A partition already starts at its filesystem. Prefer these to
                # the corresponding whole-disk node when the kernel has them.
                if not (resolved / "partition").exists() and any((child / "partition").exists() for child in resolved.iterdir() if child.is_dir()):
                    continue
                size = int(self._text(resolved / "size")) * 512
                numbers = self._text(resolved / "dev")
                if not re.fullmatch(r"\d+:\d+", numbers) or size <= 0:
                    continue
                sequence = ""
                for ancestor in (resolved, *resolved.parents):
                    if (ancestor / "diskseq").exists():
                        sequence = self._text(ancestor / "diskseq")
                        break
                    if str(ancestor) == printer:
                        break
                node = self.device_root / entry.name
                metadata = node.lstat()
                if not stat.S_ISBLK(metadata.st_mode) and not (self.allow_test_backend and stat.S_ISREG(metadata.st_mode)):
                    continue
                if stat.S_ISBLK(metadata.st_mode) and numbers != f"{os.major(metadata.st_rdev)}:{os.minor(metadata.st_rdev)}":
                    continue
                fixture = stat.S_ISREG(metadata.st_mode)
                identity = (str(resolved), numbers, sequence, size, metadata.st_ino,
                            metadata.st_mtime_ns if fixture else 0, metadata.st_dev if fixture else metadata.st_rdev)
                candidates.append(_Candidate(node, size, identity, printer, numbers, fixture))
            except (OSError, ValueError):
                continue
        return candidates

    def _mounted_writable(self, candidate: _Candidate) -> bool:
        if candidate.device_numbers is None:
            return False
        try:
            lines = self.mountinfo_path.read_text(encoding="utf-8").splitlines()
            if not lines:
                raise ValueError("Empty mount information")
            for line in lines:
                fields = line.split()
                separator = fields.index("-")
                if (separator < 6 or len(fields) != separator + 4
                        or not re.fullmatch(r"\d+", fields[0]) or not re.fullmatch(r"\d+", fields[1])
                        or not re.fullmatch(r"\d+:\d+", fields[2])
                        or not fields[3].startswith("/") or not fields[4].startswith("/")):
                    raise ValueError("Invalid mount information")
                mount_options = set(fields[5].split(","))
                superblock_options = set(fields[separator + 3].split(","))
                for options in (mount_options, superblock_options):
                    if "" in options or len(options & {"ro", "rw"}) != 1:
                        raise ValueError("Unknown mount writability")
                # A read-only bind mount can still refer to a writable
                # superblock, including inside ProtectSystem namespaces.
                if fields[2] == candidate.device_numbers and "rw" in (mount_options | superblock_options):
                    return True
        except (OSError, UnicodeError, ValueError):
            raise MediaError("media_access_denied", "The card's mount state could not be checked.") from None
        return False

    @contextmanager
    def _open(self, candidate: _Candidate):
        if self._mounted_writable(candidate):
            raise MediaError("media_in_use", "The card is mounted writable by another application. Unmount it before checking it here.")
        try:
            descriptor = os.open(candidate.device_path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        except OSError as error:
            code = "media_access_denied" if error.errno in {errno.EACCES, errno.EPERM, errno.ELOOP} else "media_changed"
            raise MediaError(code, "The memory-card device could not be opened read-only.") from None
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISBLK(metadata.st_mode) and not (self.allow_test_backend and candidate.regular_fixture and stat.S_ISREG(metadata.st_mode)):
                raise MediaError("media_access_denied", "The source is not the printer's block device.")
            if stat.S_ISBLK(metadata.st_mode) and candidate.device_numbers != f"{os.major(metadata.st_rdev)}:{os.minor(metadata.st_rdev)}":
                raise MediaError("media_changed", "The memory-card device changed.")
            if candidate not in self._discover():
                raise MediaError("media_changed", "The memory-card device changed.")
            yield descriptor
            if self._mounted_writable(candidate):
                raise MediaError("media_in_use", "Another application mounted the memory card writable during this operation.")
        finally:
            os.close(descriptor)

    def _register(self, parent: _Record, entries: list[_Entry]) -> list[dict]:
        result = []
        with self._guard:
            for entry in entries:
                if entry.is_directory and (parent.depth >= MAX_DIRECTORY_DEPTH or entry.first_cluster in parent.ancestor_clusters):
                    raise MediaError("media_directory_limit", "The card folder structure is too deep or circular.")
                key = (parent.identifier, entry.checks)
                identifier = self._entry_identifiers.get(key)
                if identifier is None:
                    if len(self._records) >= MAX_KNOWN_ENTRIES:
                        raise MediaError("media_directory_limit", "Too many card entries have been opened. Check the card again to start a new view.")
                    identifier = uuid.uuid4().hex
                    self._entry_identifiers[key] = identifier
                    self._records[identifier] = _Record(identifier, entry, parent.identifier,
                        entry.first_cluster, entry.name, parent.depth + 1,
                        parent.ancestor_clusters + ((entry.first_cluster,) if entry.is_directory else ()))
                result.append({"id": identifier, "name": entry.name, "size": entry.size,
                    "is_directory": entry.is_directory, "supported": not entry.is_directory and Path(entry.name).suffix.lower() in SUPPORTED_FILES})
        return sorted(result, key=lambda item: (not item["is_directory"], item["name"].casefold(), item["id"]))

    def probe(self, cancel_event: threading.Event | None = None) -> dict:
        event = cancel_event or threading.Event()
        with self._operation_lock(event), self._device_lock(event):
            self._invalidate("media_not_checked", checked=True)
            try:
                candidates = self._discover()
                if not candidates:
                    return self._invalidate("media_not_found", checked=True)
                if len({candidate.printer_identity for candidate in candidates}) > 1:
                    return self._invalidate("media_selection_required", checked=True)
                usable, failure = [], None
                deadline = time.monotonic() + 30
                for candidate in candidates:
                    _cancelled(event, deadline)
                    try:
                        with self._open(candidate) as descriptor:
                            volume = _FatVolume(descriptor, candidate.size_bytes, event, deadline)
                            entries = volume.entries(volume.root_cluster)
                            if candidate not in self._discover() or not volume.unchanged():
                                raise MediaError("media_changed", "The card changed during its connection check.")
                            usable.append((candidate, volume, entries))
                    except MediaError as error:
                        if error.code in {"job_cancelled", "processing_timeout"}:
                            raise
                        failure = error
                if len(usable) != 1:
                    reason = "media_selection_required" if usable else failure.code if failure else "media_not_found"
                    return self._invalidate(reason, checked=True)
                candidate, volume, entries = usable[0]
                root_identifier, volume_identifier = uuid.uuid4().hex, uuid.uuid4().hex
                root = _Record(root_identifier, None, None, volume.root_cluster, volume.label, 0,
                               (volume.root_cluster,) if volume.root_cluster is not None else ())
                with self._guard:
                    self._candidate, self._volume_signature = candidate, volume.signature
                    self._records[root_identifier] = root
                    self._capabilities.update(available=True, reason_code="media_ready", volume_id=volume_identifier,
                        root_directory_id=root_identifier, checked_at=time.time(), hardware_verified=not self.allow_test_backend,
                        filesystem=f"FAT{volume.fat_bits}", label=volume.label, volume_bytes=volume.volume_bytes)
                self._register(root, entries)
                return self.capabilities()
            except MediaError as error:
                self._invalidate(error.code, checked=True)
                raise

    @contextmanager
    def _current_volume(self, event: threading.Event):
        with self._guard:
            candidate, signature = self._candidate, self._volume_signature
        if candidate is None:
            raise MediaError("media_not_checked", "Check the memory card before opening files.")
        if candidate not in self._discover():
            raise MediaError("media_changed", "The memory card changed. Check it again.")
        with self._open(candidate) as descriptor:
            volume = _FatVolume(descriptor, candidate.size_bytes, event, time.monotonic() + 60)
            if volume.signature != signature:
                raise MediaError("media_changed", "The memory card changed. Check it again.")
            yield volume
            if candidate not in self._discover() or not volume.unchanged():
                raise MediaError("media_changed", "The memory card changed while it was being read.")

    def _record(self, identifier: str | None, *, directory: bool) -> _Record:
        with self._guard:
            if identifier is None and directory:
                identifier = self._capabilities["root_directory_id"]
            if not isinstance(identifier, str) or not IDENTIFIER.fullmatch(identifier):
                raise MediaError("media_not_checked", "Check the memory card before opening files.")
            record = self._records.get(identifier)
        if record is None:
            raise MediaError("media_changed", "This card selection is no longer available. Check the card again.")
        if directory != (record.entry is None or record.entry.is_directory):
            raise MediaError("invalid_request", "Choose a card file or folder of the requested type.")
        return record

    def list_directory(self, directory_id: str | None = None, cancel_event: threading.Event | None = None,
                       *, offset: int = 0, limit: int = 200) -> dict:
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 1000:
            raise MediaError("invalid_request", "Choose a valid card listing page.")
        event = cancel_event or threading.Event()
        try:
            with self._operation_lock(event), self._device_lock(event), self._current_volume(event) as volume:
                record = self._record(directory_id, directory=True)
                if record.entry:
                    volume.check_entry(record.entry)
                entries = self._register(record, volume.entries(record.first_cluster))
                with self._guard:
                    volume_identifier = self._capabilities["volume_id"]
                return {"volume_id": volume_identifier, "directory_id": record.identifier,
                    "parent_directory_id": record.parent_identifier, "name": record.name,
                    "entries": entries[offset:offset + limit], "offset": offset,
                    "next_offset": offset + limit if offset + limit < len(entries) else None,
                    "entry_count": len(entries), "read_only": True}
        except MediaError as error:
            if error.code in {"media_changed", "media_corrupt", "media_not_found", "media_access_denied", "media_in_use"}:
                self._invalidate(error.code)
            raise

    def copy_file(self, file_id: str, destination: Path | str, cancel_event: threading.Event | None = None,
                  maximum_bytes: int = 20 * 1024 * 1024) -> dict:
        """Copy a selected entry; callers must inspect its actual document format.

        The returned media_type is only a filename-extension hint. The source
        is never opened for writing and the destination must not already exist.
        """
        if type(maximum_bytes) is not int or not 1 <= maximum_bytes <= 128 * 1024 * 1024:
            raise MediaError("invalid_request", "Choose a supported card-file size limit.")
        event, destination = cancel_event or threading.Event(), Path(destination)
        created = False
        try:
            with self._operation_lock(event), self._device_lock(event), self._current_volume(event) as volume:
                record = self._record(file_id, directory=False)
                entry = record.entry
                assert entry is not None
                if entry.size > maximum_bytes:
                    raise MediaError("media_file_too_large", "This card file exceeds the document size limit.")
                media_type = SUPPORTED_FILES.get(Path(entry.name).suffix.lower())
                if media_type is None:
                    raise MediaError("unsupported_document", "Choose a PDF, PNG or JPEG document.")
                volume.check_entry(entry)
                clusters = volume.file_clusters(entry)
                try:
                    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
                except FileExistsError:
                    raise MediaError("media_destination_exists", "The copy destination already exists.") from None
                created = True
                digest, remaining = hashlib.sha256(), entry.size
                with os.fdopen(descriptor, "wb") as output:
                    for cluster, following in clusters:
                        source_offset = volume._cluster_offset(cluster)
                        cluster_remaining = min(volume.cluster_bytes, remaining)
                        while cluster_remaining:
                            chunk = volume.read_at(source_offset, min(65536, cluster_remaining))
                            output.write(chunk)
                            digest.update(chunk)
                            source_offset += len(chunk)
                            remaining -= len(chunk)
                            cluster_remaining -= len(chunk)
                    _cancelled(event, volume.deadline)
                volume._fat_cache.clear()
                if any(volume._next_cluster(cluster) != following for cluster, following in clusters):
                    raise MediaError("media_changed", "The selected card file changed while it was being copied.")
                volume.check_entry(entry)
                result = {"name": entry.name, "size": entry.size, "sha256": digest.hexdigest(),
                          "media_type": media_type, "hardware_verified": not self.allow_test_backend}
            return result
        except BaseException as error:
            if created:
                destination.unlink(missing_ok=True)
            if isinstance(error, MediaError) and error.code in {"media_changed", "media_corrupt", "media_not_found", "media_access_denied", "media_in_use"}:
                self._invalidate(error.code)
            raise

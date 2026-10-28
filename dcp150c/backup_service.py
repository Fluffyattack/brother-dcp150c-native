# SPDX-License-Identifier: GPL-2.0-or-later
"""Portable, explicitly selected document backups with bounded ZIP processing."""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import stat
import struct
import threading
import time
import uuid
import zipfile

from .document_storage import IDENTIFIER, StorageError, document_name, utc_time


MANIFEST_LIMIT = 2 * 1024 * 1024
ARCHIVE_OVERHEAD = 4 * 1024 * 1024
BACKUP_LIFETIME = 3600


def check_cancelled(event) -> None:
    if event.is_set():
        raise StorageError("job_cancelled", "The backup operation was cancelled.")


def invalid_backup() -> StorageError:
    return StorageError("invalid_backup", "Choose a complete, supported Control Center backup.")


class BackupService:
    def __init__(self, storage, processor, validate_profile, export_settings) -> None:
        self.storage, self.processor = storage, processor
        self.validate_profile, self.export_settings = validate_profile, export_settings
        self.directory = storage.root / "backups"
        self.directory.mkdir(mode=0o700, exist_ok=True)
        self._upload_guard = threading.Lock()
        self._receiving: set[str] = set()
        with storage.guard, storage.connection:
            storage.connection.execute("CREATE TABLE IF NOT EXISTS backups (id TEXT PRIMARY KEY, created_at REAL NOT NULL, expires_at REAL NOT NULL, size INTEGER NOT NULL)")
        self.prune()

    @property
    def maximum_upload_bytes(self) -> int:
        return self.storage.settings()["max_storage_mb"] * 1024 * 1024 + ARCHIVE_OVERHEAD

    def list(self) -> list[dict]:
        with self.storage.guard:
            rows = self.storage.connection.execute("SELECT * FROM backups WHERE expires_at>? ORDER BY created_at DESC", (time.time(),)).fetchall()
        return [{"id": row["id"], "size": row["size"], "created_at": utc_time(row["created_at"]),
                 "expires_at": utc_time(row["expires_at"]), "download_url": f"/api/backups/{row['id']}/file"} for row in rows]

    def prune(self) -> None:
        with self.storage.guard, self.storage.connection:
            self.storage.connection.execute("DELETE FROM backups WHERE expires_at<?", (time.time(),))
            known = {row["id"] + ".zip" for row in self.storage.connection.execute("SELECT id FROM backups")}
            known.update(identifier + ".incoming" for identifier in self._receiving)
            # Pending uploads are kept only while their accepted job can use them.
            for row in self.storage.connection.execute("SELECT parameters FROM jobs WHERE state IN ('queued','running')"):
                identifier = json.loads(row["parameters"]).get("archive_id")
                if isinstance(identifier, str) and IDENTIFIER.fullmatch(identifier):
                    known.add(identifier + ".incoming")
            for path in self.directory.iterdir():
                if path.name not in known and path.is_file() and path.suffix in {".zip", ".incoming"}:
                    path.unlink(missing_ok=True)

    @contextmanager
    def download(self, identifier: str):
        if not isinstance(identifier, str) or not IDENTIFIER.fullmatch(identifier):
            raise invalid_backup()
        # Opening before releasing the lock makes expiry safe during a download.
        with self.storage.guard:
            row = self.storage.connection.execute("SELECT expires_at FROM backups WHERE id=?", (identifier,)).fetchone()
            if not row or row["expires_at"] <= time.time():
                raise invalid_backup()
            handle = (self.directory / (identifier + ".zip")).open("rb")
        try:
            yield handle
        finally:
            handle.close()

    def delete(self, identifier: str) -> None:
        if not isinstance(identifier, str) or not IDENTIFIER.fullmatch(identifier):
            raise invalid_backup()
        with self.storage.guard, self.storage.connection:
            self.storage.connection.execute("DELETE FROM backups WHERE id=?", (identifier,))
            (self.directory / (identifier + ".zip")).unlink(missing_ok=True)

    def validate_selection(self, values: dict) -> dict:
        if (not isinstance(values, dict) or set(values) - {"document_ids", "include_settings"}
                or not isinstance(values.get("document_ids"), list) or len(values["document_ids"]) > 500
                or type(values.get("include_settings", True)) is not bool):
            raise invalid_backup()
        identifiers = values["document_ids"]
        if any(not isinstance(identifier, str) or not IDENTIFIER.fullmatch(identifier) for identifier in identifiers):
            raise invalid_backup()
        if len(set(identifiers)) != len(identifiers) or (not identifiers and not values.get("include_settings", True)):
            raise invalid_backup()
        return {"document_ids": identifiers, "include_settings": values.get("include_settings", True)}

    def export(self, values: dict, event) -> dict:
        with ExitStack() as leases, self.storage.workspace() as workspace:
            records = []
            for identifier in values["document_ids"]:
                leases.enter_context(self.storage.lease(identifier))
                records.append(self.storage.get(identifier))
            required = sum(self.storage.path(record["id"]).stat().st_size +
                           (self.storage.path(record["id"], "text").stat().st_size if record["has_text"] else 0) for record in records)
            if required > self.maximum_upload_bytes - ARCHIVE_OVERHEAD:
                raise StorageError("storage_full", "The selected documents exceed the current storage limit.")
            if shutil.disk_usage(self.storage.root).free < required + ARCHIVE_OVERHEAD + 32 * 1024 * 1024:
                raise StorageError("storage_full", "There is insufficient space to prepare this backup.")
            if len(self.list()) >= 3:
                raise StorageError("backup_limit", "Remove a prepared backup before creating another one.")
            manifest = {"format": "brother-dcp150c-backup", "version": 1, "created_at": utc_time(),
                        "preferences": self.export_settings() if values["include_settings"] else None, "documents": []}
            archive = workspace / "backup.zip"
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as package:
                for index, record in enumerate(records, 1):
                    check_cancelled(event)
                    source = self.storage.path(record["id"])
                    entry = {key: record[key] for key in ("name", "media_type", "pages", "created_at", "keep", "scan")}
                    entry["file"] = self._write_entry(package, source, f"documents/{index:04d}/document{source.suffix}", event)
                    entry["text"] = self._write_entry(package, self.storage.path(record["id"], "text"), f"documents/{index:04d}/text.txt", event) if record["has_text"] else None
                    manifest["documents"].append(entry)
                encoded = json.dumps(manifest, ensure_ascii=False, allow_nan=False).encode()
                if len(encoded) > MANIFEST_LIMIT:
                    raise invalid_backup()
                package.writestr("manifest.json", encoded)
            check_cancelled(event)
            identifier, now = uuid.uuid4().hex, time.time()
            destination = self.directory / (identifier + ".zip")
            with self.storage.guard:
                archive.replace(destination)
                os.chmod(destination, 0o600)
                try:
                    with self.storage.connection:
                        self.storage.connection.execute("INSERT INTO backups VALUES (?,?,?,?)", (identifier, now, now + BACKUP_LIFETIME, destination.stat().st_size))
                except BaseException:
                    destination.unlink(missing_ok=True)
                    raise
            return {"backup_id": identifier, "document_count": len(records)}

    @staticmethod
    def _write_entry(package, source: Path, name: str, event) -> dict:
        digest, size = hashlib.sha256(), 0
        with source.open("rb") as original, package.open(name, "w", force_zip64=True) as destination:
            while chunk := original.read(65536):
                check_cancelled(event)
                size += len(chunk)
                digest.update(chunk)
                destination.write(chunk)
        return {"path": name, "size": size, "sha256": digest.hexdigest()}

    @contextmanager
    def receive(self, length: int):
        """Serialize streamed uploads and remove files not handed to a job."""
        if not 0 < length <= self.maximum_upload_bytes:
            raise StorageError("backup_too_large", "This backup exceeds the current storage limit.")
        if not self._upload_guard.acquire(blocking=False):
            raise StorageError("backup_busy", "Wait for the current backup upload to finish.")
        identifier = uuid.uuid4().hex
        path = self.directory / (identifier + ".incoming")
        try:
            if shutil.disk_usage(self.storage.root).free < 2 * length + 32 * 1024 * 1024:
                raise StorageError("storage_full", "There is insufficient space to unpack this backup.")
            with self.storage.guard:
                self._receiving.add(identifier)
            yield identifier, path
        finally:
            with self.storage.guard:
                self._receiving.discard(identifier)
                active = any(json.loads(row["parameters"]).get("archive_id") == identifier for row in self.storage.connection.execute("SELECT parameters FROM jobs WHERE state IN ('queued','running')"))
            if not active:
                path.unlink(missing_ok=True)
            self._upload_guard.release()

    @staticmethod
    def _check_directory(path: Path) -> None:
        """Bound central-directory memory before zipfile reads its member list."""
        with path.open("rb") as handle:
            handle.seek(0, 2)
            length = handle.tell()
            handle.seek(max(0, length - 65557))
            tail = handle.read()
            position = tail.rfind(b"PK\x05\x06")
            if position < 0 or len(tail) - position < 22:
                raise invalid_backup()
            _, disk, directory_disk, disk_count, count, size, offset, comment = struct.unpack_from("<4s4H2IH", tail, position)
            if disk or directory_disk or position + 22 + comment != len(tail):
                raise invalid_backup()
            end_position = length - len(tail) + position
            if count == 65535 or size == 0xffffffff or offset == 0xffffffff:
                if end_position < 20:
                    raise invalid_backup()
                handle.seek(end_position - 20)
                signature, locator_disk, zip64_offset, disk_total = struct.unpack("<4sIQI", handle.read(20))
                if signature != b"PK\x06\x07" or locator_disk or disk_total != 1 or zip64_offset > end_position - 76:
                    raise invalid_backup()
                handle.seek(zip64_offset)
                fields = struct.unpack("<4sQ2H2I4Q", handle.read(56))
                if fields[0] != b"PK\x06\x06" or fields[1] < 44 or fields[4] or fields[5]:
                    raise invalid_backup()
                disk_count, count, size, offset = fields[6:10]
            if disk_count != count or not 1 <= count <= 1001 or size > 1024 * 1024 or offset + size > end_position:
                raise invalid_backup()

    def restore(self, archive_id: str, restore_settings: bool, event) -> dict:
        archive = self.directory / (archive_id + ".incoming")
        try:
            self._check_directory(archive)
            with zipfile.ZipFile(archive) as package, self.storage.workspace() as workspace:
                members = package.infolist()
                if not 1 <= len(members) <= 1001:
                    raise invalid_backup()
                names = [item.filename for item in members]
                if len(set(names)) != len(names) or "manifest.json" not in names:
                    raise invalid_backup()
                for item in members:
                    mode = item.external_attr >> 16
                    if (item.is_dir() or (stat.S_IFMT(mode) not in {0, stat.S_IFREG}) or item.flag_bits & 1
                            or item.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}):
                        raise invalid_backup()
                if package.getinfo("manifest.json").file_size > MANIFEST_LIMIT or sum(item.file_size for item in members) > self.maximum_upload_bytes:
                    raise invalid_backup()
                manifest = json.loads(package.read("manifest.json"), parse_constant=lambda value: (_ for _ in ()).throw(invalid_backup()))
                preferences, entries, expected = self._validate_manifest(manifest, restore_settings)
                if set(names) != expected:
                    raise invalid_backup()
                staged = []
                for index, entry in enumerate(entries):
                    check_cancelled(event)
                    source = workspace / f"{index}.document"
                    self._extract(package, entry["file"], source, event)
                    metadata = self.processor.inspect(source)
                    if metadata["media_type"] != entry["media_type"] or metadata["page_count"] != entry["pages"]:
                        raise invalid_backup()
                    text = workspace / f"{index}.txt" if entry["text"] else None
                    if text:
                        self._extract(package, entry["text"], text, event)
                        # Validate incrementally; never load a large OCR text into RAM.
                        with text.open(encoding="utf-8", errors="strict") as handle:
                            while handle.read(65536):
                                check_cancelled(event)
                    preview = workspace / f"{index}.preview.png"
                    if shutil.disk_usage(self.storage.root).free < 32 * 1024 * 1024:
                        raise StorageError("storage_full", "There is insufficient space to restore document previews.")
                    self.processor.preview(source, preview, cancel_event=event)
                    staged.append({**entry, "source": source, "text": text, "metadata": metadata, "preview": preview})
                check_cancelled(event)
                documents = self.storage.restore_batch(staged, preferences, event)
            return {"restored_document_ids": [document["id"] for document in documents],
                    "document_count": len(documents), "profiles_restored": len(preferences["profiles"]) if preferences else 0}
        except (zipfile.BadZipFile, zipfile.LargeZipFile, ValueError, KeyError, TypeError, struct.error, UnicodeError, OverflowError, RecursionError):
            raise invalid_backup() from None
        finally:
            archive.unlink(missing_ok=True)

    def _validate_manifest(self, manifest, restore_settings):
        if (not isinstance(manifest, dict) or set(manifest) != {"format", "version", "created_at", "preferences", "documents"}
                or manifest["format"] != "brother-dcp150c-backup" or type(manifest["version"]) is not int or manifest["version"] != 1
                or not isinstance(manifest["documents"], list) or len(manifest["documents"]) > 500):
            raise invalid_backup()
        preferences = manifest["preferences"] if restore_settings else None
        if preferences is not None:
            if (not isinstance(preferences, dict) or set(preferences) != {"format", "settings", "profiles"}
                    or type(preferences["format"]) is not int or preferences["format"] != 1
                    or not isinstance(preferences["profiles"], list) or len(preferences["profiles"]) > 50):
                raise invalid_backup()
            preferences = {"settings": self.storage.validate_settings(preferences["settings"]),
                           "profiles": [self.validate_profile(profile) for profile in preferences["profiles"]]}
        expected, entries = {"manifest.json"}, []
        for index, entry in enumerate(manifest["documents"], 1):
            if (not isinstance(entry, dict) or set(entry) != {"name", "media_type", "pages", "created_at", "keep", "scan", "file", "text"}
                    or not isinstance(entry["media_type"], str) or entry["media_type"] not in {"application/pdf", "image/png", "image/jpeg"}
                    or type(entry["pages"]) is not int or not 1 <= entry["pages"] <= 25 or type(entry["keep"]) is not bool
                    or not isinstance(entry["created_at"], str)):
                raise invalid_backup()
            name = document_name(entry["name"])
            created = datetime.fromisoformat(entry["created_at"].replace("Z", "+00:00"))
            if created.tzinfo is None or not 0 <= created.timestamp() <= time.time() + 300:
                raise invalid_backup()
            scan = entry["scan"]
            if scan is not None:
                if (not isinstance(scan, dict) or set(scan) - {"kind", "width", "height", "resolution", "mode", "area", "device", "content_type", "hardware_verified"}
                        or len(json.dumps(scan)) > 16384 or scan.get("kind") not in {"preview", "scan"}
                        or not isinstance(scan.get("device"), str) or len(scan["device"]) > 2048
                        or type(scan.get("hardware_verified")) is not bool):
                    raise invalid_backup()
                for key in ("width", "height"):
                    if type(scan.get(key)) is not int or not 1 <= scan[key] <= 100000:
                        raise invalid_backup()
                self._validate_scan_area(scan)
            extension = {"application/pdf": ".pdf", "image/png": ".png", "image/jpeg": ".jpg"}[entry["media_type"]]
            for key, suffix in (("file", "document" + extension), ("text", "text.txt")):
                item = entry[key]
                if key == "text" and item is None:
                    continue
                path = f"documents/{index:04d}/{suffix}"
                if (not isinstance(item, dict) or set(item) != {"path", "size", "sha256"} or item["path"] != path
                        or type(item["size"]) is not int or not 0 <= item["size"] <= self.maximum_upload_bytes
                        or not isinstance(item["sha256"], str) or len(item["sha256"]) != 64
                        or any(character not in "0123456789abcdef" for character in item["sha256"])):
                    raise invalid_backup()
                expected.add(path)
            entries.append({**entry, "name": name, "created_at": created.timestamp()})
        return preferences, entries, expected

    @staticmethod
    def _validate_scan_area(scan) -> None:
        area = scan.get("area")
        if (not isinstance(area, dict) or set(area) != {"left", "top", "width", "height"}
                or type(scan.get("resolution")) is not int or scan["resolution"] not in {75, 100, 150, 200, 300, 600}
                or not isinstance(scan.get("mode"), str) or scan["mode"] not in {"color", "gray", "lineart"}):
            raise invalid_backup()
        for key, value in area.items():
            if (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
                    or not 0 <= value <= 500 or (key in {"width", "height"} and value == 0)):
                raise invalid_backup()

    @staticmethod
    def _extract(package, entry: dict, destination: Path, event) -> None:
        info = package.getinfo(entry["path"])
        if info.file_size != entry["size"]:
            raise invalid_backup()
        digest, size = hashlib.sha256(), 0
        with package.open(info) as source, destination.open("xb") as output:
            os.chmod(destination, 0o600)
            while chunk := source.read(65536):
                check_cancelled(event)
                size += len(chunk)
                if size > entry["size"]:
                    raise invalid_backup()
                digest.update(chunk)
                output.write(chunk)
        if size != entry["size"] or digest.hexdigest() != entry["sha256"]:
            raise invalid_backup()

# SPDX-License-Identifier: GPL-2.0-or-later
"""Private document files and durable metadata for the browser Control Center."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import fcntl
import os
from pathlib import Path
import re
import shutil
import sqlite3
import threading
import time
import uuid


IDENTIFIER = re.compile(r"[a-f0-9]{32}\Z")
DEFAULT_SETTINGS = {"retention_hours": 24, "max_storage_mb": 512}


class StorageError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def utc_time(timestamp: float | None = None) -> str:
    return datetime.fromtimestamp(time.time() if timestamp is None else timestamp,
                                  timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def document_name(value: str) -> str:
    if not isinstance(value, str):
        raise StorageError("invalid_document_name", "Please give the document a name.")
    name = value.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(character for character in name if character.isprintable()).strip()[:180]
    if not name or name in {".", ".."}:
        raise StorageError("invalid_document_name", "Please give the document a name.")
    return name


class DocumentStorage:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        self._instance_lock = os.open(self.root / ".instance.lock", os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self._instance_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self._instance_lock)
            raise StorageError("storage_in_use", "This document storage is already open in another Control Center.") from None
        self.documents_directory = self.root / "documents"
        self.work_directory = self.root / "work"
        for directory in (self.documents_directory, self.work_directory):
            directory.mkdir(mode=0o700, exist_ok=True)
        self.guard = threading.RLock()
        self.connection = sqlite3.connect(self.root / "control-center.sqlite3", check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        with self.guard, self.connection:
            self.connection.executescript("""
                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, filename TEXT NOT NULL,
                    media_type TEXT NOT NULL, pages INTEGER NOT NULL, size INTEGER NOT NULL,
                    created_at REAL NOT NULL, keep INTEGER NOT NULL DEFAULT 0,
                    expires_at REAL NOT NULL, text_filename TEXT
                );
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS profiles (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL, settings TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS scan_document_metadata (
                    document_id TEXT PRIMARY KEY, metadata TEXT NOT NULL
                );
            """)
        self._leases: dict[str, int] = {}
        self._closed = False
        # Only this instance owns work directories. Incomplete operations are not
        # replayed after a restart and their private temporary files can be removed.
        for directory in self.work_directory.iterdir():
            if IDENTIFIER.fullmatch(directory.name) and directory.is_dir() and not directory.is_symlink():
                shutil.rmtree(directory)

    def settings(self) -> dict:
        with self.guard:
            rows = self.connection.execute("SELECT key,value FROM settings").fetchall()
        return {**DEFAULT_SETTINGS, **{row["key"]: json.loads(row["value"]) for row in rows}}

    @staticmethod
    def validate_settings(values: dict) -> dict:
        if not isinstance(values, dict) or set(values) - set(DEFAULT_SETTINGS):
            raise StorageError("invalid_settings", "These settings are not supported.")
        for key, value in values.items():
            minimum, maximum = (1, 168) if key == "retention_hours" else (64, 4096)
            if type(value) is not int or not minimum <= value <= maximum:
                raise StorageError("invalid_settings", "A setting is outside the supported range.")
        return values

    def update_settings(self, values: dict) -> dict:
        values = self.validate_settings(values)
        with self.guard, self.connection:
            for key, value in values.items():
                self.connection.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, json.dumps(value)))
            if "retention_hours" in values:
                self.connection.execute("UPDATE documents SET expires_at=created_at+? WHERE keep=0", (values["retention_hours"] * 3600,))
                if self.connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='scan_sessions'").fetchone():
                    self.connection.execute("UPDATE scan_sessions SET expires_at=updated_at+?", (values["retention_hours"] * 3600,))
        return self.settings()

    def check_space(self, additional_bytes: int) -> None:
        limit = self.settings()["max_storage_mb"] * 1024 * 1024
        total = sum(path.stat().st_size for path in self.documents_directory.rglob("*") if path.is_file())
        if total + additional_bytes > limit or shutil.disk_usage(self.root).free < additional_bytes + 32 * 1024 * 1024:
            raise StorageError("storage_full", "The document storage is full. Delete documents or increase its limit.")

    @contextmanager
    def workspace(self):
        directory = self.work_directory / uuid.uuid4().hex
        directory.mkdir(mode=0o700)
        try:
            yield directory
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    def _record(self, row: sqlite3.Row) -> dict:
        return {"id": row["id"], "name": row["name"], "media_type": row["media_type"],
                "pages": row["pages"], "size": row["size"], "created_at": utc_time(row["created_at"]),
                "keep": bool(row["keep"]), "expires_at": None if row["keep"] else utc_time(row["expires_at"]),
                "kind": "pdf" if row["media_type"] == "application/pdf" else "image",
                "has_text": bool(row["text_filename"]),
                "scan": json.loads(row["scan_metadata"]) if row["scan_metadata"] else None,
                "has_preview": (self.documents_directory / row["id"] / "preview.png").is_file()}

    def list(self) -> list[dict]:
        with self.guard:
            rows = self.connection.execute("SELECT documents.*,scan_document_metadata.metadata AS scan_metadata FROM documents LEFT JOIN scan_document_metadata ON documents.id=scan_document_metadata.document_id ORDER BY created_at DESC LIMIT 500").fetchall()
        return [self._record(row) for row in rows]

    def _row(self, identifier: str) -> sqlite3.Row:
        if not isinstance(identifier, str) or not IDENTIFIER.fullmatch(identifier):
            raise StorageError("document_not_found", "This document is no longer available.")
        row = self.connection.execute("SELECT documents.*,scan_document_metadata.metadata AS scan_metadata FROM documents LEFT JOIN scan_document_metadata ON documents.id=scan_document_metadata.document_id WHERE documents.id=?", (identifier,)).fetchone()
        if row is None:
            raise StorageError("document_not_found", "This document is no longer available.")
        return row

    def get(self, identifier: str) -> dict:
        with self.guard:
            return self._record(self._row(identifier))

    def path(self, identifier: str, kind: str = "file") -> Path:
        with self.guard:
            row = self._row(identifier)
            filename = row["filename"] if kind == "file" else row["text_filename"] if kind == "text" else "preview.png"
            if not filename:
                raise StorageError("document_not_found", "This document has no text download.")
            return self.documents_directory / identifier / filename

    @contextmanager
    def lease(self, identifier: str):
        with self.guard:
            self._row(identifier)
            self._leases[identifier] = self._leases.get(identifier, 0) + 1
        try:
            yield self.path(identifier)
        finally:
            with self.guard:
                self._leases[identifier] -= 1
                if not self._leases[identifier]:
                    del self._leases[identifier]

    def add(self, source: Path, name: str, metadata: dict, *, text: Path | None = None,
            scan_metadata: dict | None = None) -> dict:
        name = document_name(name)
        media_type = metadata["media_type"]
        extension = {"application/pdf": ".pdf", "image/png": ".png", "image/jpeg": ".jpg"}.get(media_type)
        if extension is None:
            raise StorageError("unsupported_document", "Choose a PDF, PNG or JPEG document.")
        identifier = uuid.uuid4().hex
        destination = self.documents_directory / identifier
        with self.guard:
            if self.connection.execute("SELECT count(*) FROM documents").fetchone()[0] >= 500:
                raise StorageError("storage_full", "The document storage contains too many documents.")
            self.check_space(source.stat().st_size + (text.stat().st_size if text else 0))
            destination.mkdir(mode=0o700)
            filename = "document" + extension
            try:
                shutil.copyfile(source, destination / filename)
                os.chmod(destination / filename, 0o600)
                if text:
                    shutil.copyfile(text, destination / "text.txt")
                    os.chmod(destination / "text.txt", 0o600)
                now = time.time()
                with self.connection:
                    self.connection.execute("INSERT INTO documents VALUES (?,?,?,?,?,?,?,?,?,?)", (
                        identifier, name, filename, media_type, metadata["page_count"], source.stat().st_size,
                        now, 0, now + self.settings()["retention_hours"] * 3600, "text.txt" if text else None))
                    if scan_metadata is not None:
                        self.connection.execute("INSERT INTO scan_document_metadata VALUES (?,?)", (
                            identifier, json.dumps(scan_metadata, allow_nan=False)))
            except BaseException:
                shutil.rmtree(destination, ignore_errors=True)
                raise
            return self.get(identifier)

    def update(self, identifier: str, values: dict) -> dict:
        if set(values) - {"name", "keep"} or ("keep" in values and type(values["keep"]) is not bool):
            raise StorageError("invalid_settings", "These document settings are not supported.")
        with self.guard, self.connection:
            row = self._row(identifier)
            name = document_name(values["name"]) if "name" in values else row["name"]
            keep = values.get("keep", bool(row["keep"]))
            self.connection.execute("UPDATE documents SET name=?,keep=?,expires_at=? WHERE id=?", (
                name, int(keep), time.time() + self.settings()["retention_hours"] * 3600, identifier))
        return self.get(identifier)

    def delete(self, identifier: str) -> None:
        with self.guard:
            self._row(identifier)
            if identifier in self._scan_session_references():
                raise StorageError("document_in_scan_session", "Remove this page from its scanning document before deleting the original.")
            if self._in_use(identifier):
                raise StorageError("document_in_use", "Wait for the document's current operation to finish.")
            # Commit metadata first. After a crash, unreferenced files are cleaned
            # by prune; a missing directory can never leave an undeletable row.
            with self.connection:
                self.connection.execute("DELETE FROM scan_document_metadata WHERE document_id=?", (identifier,))
                self.connection.execute("DELETE FROM documents WHERE id=?", (identifier,))
            try:
                shutil.rmtree(self.documents_directory / identifier)
            except FileNotFoundError:
                pass

    def prune(self) -> None:
        with self.guard:
            expired = self.connection.execute("SELECT id FROM documents WHERE keep=0 AND expires_at<?", (time.time(),)).fetchall()
            for row in expired:
                if not self._in_use(row["id"]):
                    self.delete(row["id"])
            known = {row["id"] for row in self.connection.execute("SELECT id FROM documents")}
            with self.connection:
                self.connection.execute("DELETE FROM scan_document_metadata WHERE document_id NOT IN (SELECT id FROM documents)")
            for directory in self.documents_directory.iterdir():
                if IDENTIFIER.fullmatch(directory.name) and directory.name not in known and directory.is_dir() and not directory.is_symlink():
                    try:
                        shutil.rmtree(directory)
                    except OSError:
                        pass

    def _in_use(self, identifier: str) -> bool:
        """Protect queued inputs too, before the single worker acquires a lease."""
        if self._leases.get(identifier):
            return True
        if identifier in self._scan_session_references():
            return True
        if not self.connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'").fetchone():
            return False
        for row in self.connection.execute("SELECT parameters FROM jobs WHERE state IN ('queued','running')"):
            parameters = json.loads(row["parameters"])
            if parameters.get("document_id") == identifier or identifier in parameters.get("document_ids", []):
                return True
        return False

    def _scan_session_references(self) -> set[str]:
        if not self.connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='scan_sessions'").fetchone():
            return set()
        rows = self.connection.execute("SELECT pages FROM scan_sessions WHERE expires_at>? OR active_job_id IS NOT NULL", (time.time(),))
        return {page["document_id"] for row in rows for page in json.loads(row["pages"])}

    def profiles(self) -> list[dict]:
        with self.guard:
            rows = self.connection.execute("SELECT * FROM profiles ORDER BY name COLLATE NOCASE").fetchall()
        return [{"id": row["id"], "name": row["name"], "kind": row["kind"], "settings": json.loads(row["settings"])} for row in rows]

    def save_profile(self, name: str, kind: str, settings: dict, identifier: str | None = None) -> dict:
        name = document_name(name)
        identifier = identifier or uuid.uuid4().hex
        if kind not in {"print", "scan", "ocr", "copy"} or not IDENTIFIER.fullmatch(identifier):
            raise StorageError("invalid_profile", "This profile type is not supported.")
        with self.guard, self.connection:
            if len(self.profiles()) >= 50 and not any(profile["id"] == identifier for profile in self.profiles()):
                raise StorageError("profile_limit", "Delete a profile before adding another one.")
            self.connection.execute("INSERT OR REPLACE INTO profiles VALUES (?,?,?,?)", (identifier, name, kind, json.dumps(settings)))
        return {"id": identifier, "name": name, "kind": kind, "settings": settings}

    def delete_profile(self, identifier: str) -> None:
        with self.guard, self.connection:
            self.connection.execute("DELETE FROM profiles WHERE id=?", (identifier,))

    def close(self) -> None:
        with self.guard:
            if not self._closed:
                self._closed = True
                self.connection.close()
                os.close(self._instance_lock)

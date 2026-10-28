# SPDX-License-Identifier: GPL-2.0-or-later
"""Durable flatbed scanning sessions with explicit next-page and finish actions.

Pages retain their original document identifiers. Reordering, rotation and crop
are saved as editing instructions, leaving original image files unchanged.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid

from .document_storage import IDENTIFIER, DocumentStorage, StorageError, document_name, utc_time
from .job_manager import JobManager


class ScanSessions:
    def __init__(self, storage: DocumentStorage, jobs: JobManager, *, validate_settings,
                 capture_document, combine_pages, queue_preview) -> None:
        self.storage = storage
        self.jobs = jobs
        self.validate_settings = validate_settings
        self.capture_document = capture_document
        self.combine_pages = combine_pages
        self.queue_preview = queue_preview
        with storage.guard, storage.connection:
            storage.connection.execute("""CREATE TABLE IF NOT EXISTS scan_sessions (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, settings TEXT NOT NULL,
                pages TEXT NOT NULL, state TEXT NOT NULL, revision INTEGER NOT NULL,
                created_at REAL NOT NULL, updated_at REAL NOT NULL, expires_at REAL NOT NULL,
                active_job_id TEXT, result_document_id TEXT, request_key TEXT UNIQUE, request_hash TEXT NOT NULL
            )""")
        self.recover()

    def _row(self, identifier):
        if not isinstance(identifier, str) or not IDENTIFIER.fullmatch(identifier):
            raise StorageError("scan_session_not_found", "This scanning document is no longer available.")
        row = self.storage.connection.execute("SELECT * FROM scan_sessions WHERE id=?", (identifier,)).fetchone()
        if row is None or (row["expires_at"] < time.time() and not row["active_job_id"]):
            raise StorageError("scan_session_not_found", "This scanning document is no longer available.")
        return row

    @staticmethod
    def _record(row) -> dict:
        pages = json.loads(row["pages"])
        return {"id": row["id"], "name": row["name"], "settings": json.loads(row["settings"]),
                "pages": pages, "page_count": len(pages), "state": row["state"], "revision": row["revision"],
                "created_at": utc_time(row["created_at"]), "updated_at": utc_time(row["updated_at"]),
                "expires_at": utc_time(row["expires_at"]), "active_job_id": row["active_job_id"],
                "result_document_id": row["result_document_id"]}

    def get(self, identifier: str) -> dict:
        with self.storage.guard:
            return self._record(self._row(identifier))

    def list(self) -> list[dict]:
        with self.storage.guard:
            rows = self.storage.connection.execute("SELECT * FROM scan_sessions WHERE expires_at>? OR active_job_id IS NOT NULL ORDER BY updated_at DESC LIMIT 64", (time.time(),)).fetchall()
        return [self._record(row) for row in rows]

    @staticmethod
    def _revision(values: dict) -> int:
        revision = values.get("revision")
        if type(revision) is not int or revision < 0:
            raise StorageError("invalid_scan_session", "Refresh the scanning document before changing it.")
        return revision

    @staticmethod
    def _editable(row, revision: int) -> None:
        if row["active_job_id"]:
            raise StorageError("scan_session_busy", "Wait for the current page or PDF operation to finish.")
        if row["state"] != "open":
            raise StorageError("scan_session_finished", "This document is finished. Start a new scanning document.")
        if row["revision"] != revision:
            raise StorageError("scan_session_conflict", "This document changed in another window. Refresh it before editing.")

    def create(self, values: dict, request_key: str | None = None) -> dict:
        if not isinstance(values, dict) or set(values) - {"name", "settings"}:
            raise StorageError("invalid_scan_session", "Choose a document name and scan settings.")
        name = document_name(values.get("name", "Scanned document"))
        if not isinstance(values.get("settings", {}), dict):
            raise StorageError("invalid_scan_session", "Choose supported scan settings.")
        settings = self.validate_settings({**values.get("settings", {}), "output_format": "png"})
        encoded_settings = json.dumps(settings, allow_nan=False, sort_keys=True)
        request_hash = hashlib.sha256(json.dumps([name, settings], sort_keys=True, allow_nan=False).encode()).hexdigest()
        if request_key is not None and (not isinstance(request_key, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{8,128}", request_key)):
            raise StorageError("invalid_request", "The request identifier is invalid.")
        with self.storage.guard, self.storage.connection:
            if request_key:
                previous = self.storage.connection.execute("SELECT * FROM scan_sessions WHERE request_key=?", (request_key,)).fetchone()
                if previous:
                    if previous["request_hash"] != request_hash:
                        raise StorageError("request_conflict", "This request identifier was used for another document.")
                    return self._record(previous)
            if self.storage.connection.execute("SELECT count(*) FROM scan_sessions").fetchone()[0] >= 64:
                raise StorageError("scan_session_limit", "Remove an old scanning document before creating another one.")
            identifier, now = uuid.uuid4().hex, time.time()
            self.storage.connection.execute("INSERT INTO scan_sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                identifier, name, encoded_settings, "[]", "open", 0, now, now,
                now + self.storage.settings()["retention_hours"] * 3600, None, None, request_key, request_hash))
            return self.get(identifier)

    def update(self, identifier: str, values: dict) -> dict:
        if not isinstance(values, dict) or set(values) - {"revision", "name", "pages"}:
            raise StorageError("invalid_scan_session", "Choose supported page editing settings.")
        revision = self._revision(values)
        with self.storage.guard, self.storage.connection:
            row = self._row(identifier)
            self._editable(row, revision)
            original_pages = json.loads(row["pages"])
            pages = values.get("pages", original_pages)
            if not isinstance(pages, list) or len(pages) > 25:
                raise StorageError("invalid_pages", "A scanning document can contain up to 25 pages.")
            by_identifier = {page["id"]: page for page in original_pages}
            updated, selected = [], set()
            from .document_processing import validated_crop, validated_rotation
            for page in pages:
                if not isinstance(page, dict) or set(page) - {"id", "document_id", "rotation", "crop"}:
                    raise StorageError("invalid_pages", "Choose pages from this scanning document.")
                page_identifier = page.get("id")
                if not isinstance(page_identifier, str) or page_identifier not in by_identifier or page_identifier in selected:
                    raise StorageError("invalid_pages", "Each page can appear once in the scanning document.")
                original = by_identifier[page_identifier]
                if page.get("document_id", original["document_id"]) != original["document_id"]:
                    raise StorageError("invalid_pages", "A page's original document cannot be replaced.")
                rotation = validated_rotation(page.get("rotation", original["rotation"]))
                crop = page.get("crop", original.get("crop"))
                crop_values = validated_crop(crop)
                crop = dict(zip(("left", "top", "width", "height"), crop_values)) if crop_values else None
                updated.append({**original, "rotation": rotation, "crop": crop})
                selected.add(page_identifier)
            self._write(identifier, name=document_name(values.get("name", row["name"])), pages=updated)
            return self.get(identifier)

    def _write(self, identifier: str, *, name=None, pages=None, settings=None, active_job_id=None,
               state: str = "open", result_document_id=None) -> None:
        row = self._row(identifier)
        now = time.time()
        self.storage.connection.execute("UPDATE scan_sessions SET name=?,pages=?,settings=?,state=?,revision=revision+1,updated_at=?,expires_at=?,active_job_id=?,result_document_id=? WHERE id=?", (
            row["name"] if name is None else name, row["pages"] if pages is None else json.dumps(pages, allow_nan=False),
            row["settings"] if settings is None else json.dumps(settings, allow_nan=False), state, now,
            now + self.storage.settings()["retention_hours"] * 3600, active_job_id,
            result_document_id if result_document_id is not None else row["result_document_id"], identifier))

    def delete(self, identifier: str) -> None:
        with self.storage.guard, self.storage.connection:
            row = self._row(identifier)
            if row["active_job_id"]:
                raise StorageError("scan_session_busy", "Cancel the current operation before removing this scanning document.")
            # Original documents remain in the library with their normal retention.
            self.storage.connection.execute("DELETE FROM scan_sessions WHERE id=?", (identifier,))

    def capture(self, identifier: str, values: dict, request_key: str | None) -> dict:
        if set(values) - {"revision", "settings"}:
            raise StorageError("invalid_scan_session", "Choose supported scan settings.")
        revision = self._revision(values)
        if "settings" in values and not isinstance(values["settings"], dict):
            raise StorageError("invalid_scan_session", "Choose supported scan settings.")
        settings = self.validate_settings({**values["settings"], "output_format": "png"}) if "settings" in values else None
        reserved = {}
        def reserve(job_identifier):
            row = self._row(identifier)
            self._editable(row, revision)
            pages = json.loads(row["pages"])
            if len(pages) >= 25:
                raise StorageError("scan_session_page_limit", "Finish this document before scanning more than 25 pages.")
            chosen_settings = settings or json.loads(row["settings"])
            reserved.update(settings={**chosen_settings, "name": row["name"] + " · " + str(len(pages) + 1)})
            self._write(identifier, settings=chosen_settings, active_job_id=job_identifier)
        def action(event, job_identifier):
            try:
                document = self.capture_document(reserved["settings"], event)
                if event.is_set():
                    raise StorageError("job_cancelled", "The operation was cancelled.")
                with self.storage.guard, self.storage.connection:
                    row = self._row(identifier)
                    pages = json.loads(row["pages"])
                    pages.append({"id": uuid.uuid4().hex, "document_id": document["id"], "rotation": 0, "crop": None})
                    self._write(identifier, pages=pages)
                self.queue_preview(document["id"])
                return {"document_id": document["id"], "scan_session_id": identifier}
            finally:
                self._release(identifier, job_identifier)
        return self.jobs.submit("scan_page", {"scan_session_id": identifier, **values}, action, request_key, on_reserved=reserve)

    def finish(self, identifier: str, values: dict, request_key: str | None) -> dict:
        if set(values) - {"revision", "ocr", "languages"} or type(values.get("ocr", False)) is not bool:
            raise StorageError("invalid_scan_session", "Choose whether to create a searchable PDF.")
        revision = self._revision(values)
        languages = values.get("languages", "deu+eng")
        if not isinstance(languages, str) or len(languages) > 80:
            raise StorageError("ocr_languages", "Choose installed recognition languages.")
        reserved = {}
        def reserve(job_identifier):
            row = self._row(identifier)
            self._editable(row, revision)
            pages = json.loads(row["pages"])
            if not pages:
                raise StorageError("scan_session_empty", "Scan at least one page before creating the PDF.")
            reserved.update(name=row["name"], pages=pages)
            self._write(identifier, active_job_id=job_identifier, state="finishing")
        def action(event, job_identifier):
            try:
                document = self.combine_pages(reserved["name"], reserved["pages"], values.get("ocr", False), languages, event)
                with self.storage.guard, self.storage.connection:
                    self._write(identifier, state="completed", result_document_id=document["id"])
                self.queue_preview(document["id"])
                return {"document_id": document["id"], "scan_session_id": identifier}
            finally:
                self._release(identifier, job_identifier)
        return self.jobs.submit("scan_finish", {"scan_session_id": identifier, **values}, action, request_key, on_reserved=reserve)

    def _release(self, identifier: str, job_identifier: str) -> None:
        with self.storage.guard, self.storage.connection:
            row = self.storage.connection.execute("SELECT * FROM scan_sessions WHERE id=? AND active_job_id=?", (identifier, job_identifier)).fetchone()
            if row:
                self._write(identifier)

    def recover(self) -> None:
        """Free interrupted/cancelled reservations without repeating device work."""
        with self.storage.guard, self.storage.connection:
            rows = self.storage.connection.execute("SELECT * FROM scan_sessions WHERE active_job_id IS NOT NULL").fetchall()
            for row in rows:
                job = self.storage.connection.execute("SELECT state FROM jobs WHERE id=?", (row["active_job_id"],)).fetchone()
                if job is None or job["state"] not in {"queued", "running"}:
                    self._write(row["id"], state="open")
            self.storage.connection.execute("DELETE FROM scan_sessions WHERE expires_at<? AND active_job_id IS NULL", (time.time(),))

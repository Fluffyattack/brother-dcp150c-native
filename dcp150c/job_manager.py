# SPDX-License-Identifier: GPL-2.0-or-later
"""One bounded processing queue with durable history and explicit cancellation."""

from __future__ import annotations

import hashlib
import json
import logging
import queue
import re
import threading
import time
import uuid
from typing import Callable

from .document_storage import DocumentStorage, StorageError, utc_time


LOG = logging.getLogger("dcp150c.jobs")


class JobManager:
    def __init__(self, storage: DocumentStorage) -> None:
        self.storage = storage
        self._queue: queue.Queue = queue.Queue(maxsize=20)
        self._events: dict[str, threading.Event] = {}
        self._stopping = threading.Event()
        self._guard = threading.RLock()
        with storage.guard, storage.connection:
            storage.connection.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, state TEXT NOT NULL,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    parameters TEXT NOT NULL, result TEXT NOT NULL, message_code TEXT NOT NULL,
                    idempotency_key TEXT UNIQUE, request_hash TEXT NOT NULL
                );
                UPDATE jobs SET state='interrupted', message_code='job_interrupted'
                    WHERE state IN ('queued','running');
            """)
        self._worker = threading.Thread(target=self._run, name="dcp150c-documents", daemon=False)
        self._worker.start()

    def _record(self, row) -> dict:
        parameters, result = json.loads(row["parameters"]), json.loads(row["result"])
        return {"id": row["id"], "kind": row["kind"], "state": row["state"],
                "created_at": utc_time(row["created_at"]), "updated_at": utc_time(row["updated_at"]),
                "message_code": row["message_code"], "document_id": parameters.get("document_id"),
                "scan_session_id": parameters.get("scan_session_id"),
                "result_document_id": result.get("document_id"), "cups_job_id": result.get("cups_job_id"),
                "progress": 100 if row["state"] in {"succeeded", "submitted"} else None,
                "result": result}

    def list(self) -> list[dict]:
        with self.storage.guard:
            rows = self.storage.connection.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 50").fetchall()
        return [self._record(row) for row in rows]

    def unfinished_print_jobs(self) -> list[dict]:
        with self.storage.guard:
            rows = self.storage.connection.execute("SELECT * FROM jobs WHERE state IN ('submitted','interrupted') ORDER BY created_at").fetchall()
        return [self._record(row) for row in rows if json.loads(row["result"]).get("cups_job_id")]

    def get(self, identifier: str) -> dict:
        with self.storage.guard:
            row = self.storage.connection.execute("SELECT * FROM jobs WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise StorageError("job_not_found", "This operation is no longer available.")
        return self._record(row)

    def submit(self, kind: str, parameters: dict, action: Callable, idempotency_key: str | None = None,
               *, on_reserved: Callable | None = None) -> dict:
        request_hash = hashlib.sha256(json.dumps([kind, parameters], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if idempotency_key is not None and not re.fullmatch(r"[a-zA-Z0-9_.-]{8,128}", idempotency_key):
            raise StorageError("invalid_request", "The request identifier is invalid.")
        with self._guard, self.storage.guard:
            if idempotency_key:
                previous = self.storage.connection.execute("SELECT * FROM jobs WHERE idempotency_key=?", (idempotency_key,)).fetchone()
                if previous:
                    if previous["request_hash"] != request_hash:
                        raise StorageError("request_conflict", "This request identifier was used for a different operation.")
                    return self._record(previous)
            if self._stopping.is_set() or self._queue.full():
                raise StorageError("job_queue_full", "Wait for current operations to finish before starting another one.")
            identifier, now = uuid.uuid4().hex, time.time()
            event = threading.Event()
            # The reservation and associated workflow metadata commit together.
            # Never expose work to the worker before its durable record exists.
            with self.storage.connection:
                self.storage.connection.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?)", (
                    identifier, kind, "queued", now, now, json.dumps(parameters), "{}", "job_queued", idempotency_key, request_hash))
                if on_reserved:
                    on_reserved(identifier)
            self._events[identifier] = event
            self._queue.put_nowait((identifier, event, action))
            return self.get(identifier)

    def cancel(self, identifier: str) -> dict:
        with self._guard:
            job = self.get(identifier)
            if job["state"] in {"queued", "running"}:
                event = self._events.get(identifier)
                if event:
                    event.set()
                if job["state"] == "queued":
                    self._update(identifier, "cancelled", "job_cancelled")
            return job

    def mark_cancelled(self, identifier: str) -> None:
        self._update(identifier, "cancelled", "job_cancelled")

    def remember_result(self, identifier: str, result: dict) -> None:
        """Persist a held CUPS job before it is released to the physical queue."""
        with self.storage.guard, self.storage.connection:
            row = self.storage.connection.execute("SELECT result FROM jobs WHERE id=?", (identifier,)).fetchone()
            previous = json.loads(row["result"]) if row else {}
            self.storage.connection.execute("UPDATE jobs SET result=?,updated_at=? WHERE id=?", (
                json.dumps({**previous, **result}), time.time(), identifier))

    def _update(self, identifier: str, state: str, code: str, result: dict | None = None) -> None:
        with self.storage.guard, self.storage.connection:
            if result is not None:
                self.remember_result(identifier, result)
            self.storage.connection.execute("UPDATE jobs SET state=?,message_code=?,updated_at=? WHERE id=?", (
                state, code, time.time(), identifier))

    def _run(self) -> None:
        while not self._stopping.is_set() or not self._queue.empty():
            try:
                identifier, event, action = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                if event.is_set():
                    self._update(identifier, "cancelled", "job_cancelled")
                    continue
                self._update(identifier, "running", "job_running")
                result = action(event, identifier) or {}
                state = "submitted" if result.get("cups_job_id") else "succeeded"
                self._update(identifier, state, "job_submitted" if state == "submitted" else "job_succeeded", result)
            except Exception as error:
                cancelled = event.is_set() or getattr(error, "code", "") == "job_cancelled"
                code = "job_cancelled" if cancelled else getattr(error, "code", "processing_failed")
                self._update(identifier, "cancelled" if cancelled else "failed", code)
                # Only an error class/code is logged; document names/content stay private.
                LOG.warning("Operation %s ended with %s", identifier, code)
            finally:
                with self._guard:
                    self._events.pop(identifier, None)
                self._queue.task_done()
                try:
                    self.storage.prune()
                except OSError:
                    LOG.warning("Expired documents could not be removed yet")

    def close(self) -> None:
        self._stopping.set()
        with self._guard:
            for event in self._events.values():
                event.set()
        self._worker.join()

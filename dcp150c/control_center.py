# SPDX-License-Identifier: GPL-2.0-or-later
"""Coordinate explicit browser actions without doing device work in HTTP handlers."""

from __future__ import annotations

import copy
from contextlib import ExitStack
import json
import os
from pathlib import Path
import threading

from .access_control import AccessControl
from .backup_service import BackupService
from .document_storage import DocumentStorage, StorageError, document_name
from .job_manager import JobManager
from .scanner_service import ScannerService
from .scan_sessions import ScanSessions


class ControlCenter:
    def __init__(self, state_directory: Path | str, password_file: Path | str, *,
                 queue: str = "DCP150C-Native", lock_path: str = "/run/lock/dcp150c-device.lock",
                 scanner_device: str = "") -> None:
        new_database = not (Path(state_directory) / "control-center.sqlite3").exists()
        self.access = AccessControl(password_file)
        self.storage = DocumentStorage(state_directory)
        self.jobs = JobManager(self.storage)
        self.scanner = ScannerService(lock_path=lock_path, device=scanner_device)
        self.processor = self.printing = self.ocr = None
        self._capabilities = {"documents": {"available": False}, "print": {"available": False},
                              "ocr": {"available": False, "languages": [], "engine": "tesseract"}}
        try:
            from .document_processing import DocumentProcessor
            from .printing_service import PrintingService
            from .ocr_service import OCRService
            self.processor = DocumentProcessor()
            self.printing = PrintingService(queue)
            self.ocr = OCRService()
            printing_capabilities = self.printing.capabilities()
            printing_capabilities.update(papers=printing_capabilities.get("paper_sizes", []),
                                         media=list(printing_capabilities.get("media_qualities", {})),
                                         qualities=list(dict.fromkeys(quality for qualities in printing_capabilities.get("media_qualities", {}).values() for quality in qualities)))
            self._capabilities.update(documents=self.processor.capabilities(), print=printing_capabilities,
                                      ocr=self.ocr.capabilities())
        except ImportError:
            pass  # Status-only installations remain useful without document dependencies.
        if new_database:
            for name, kind, settings in (
                ("Document", "scan", {"resolution": 300, "mode": "color", "output_format": "pdf"}),
                ("Photo", "scan", {"resolution": 600, "mode": "color", "output_format": "png"}),
                ("Black and white", "scan", {"resolution": 300, "mode": "lineart", "output_format": "pdf"}),
                ("Searchable document", "ocr", {"languages": "deu+eng"}),
                ("Copy", "copy", {"resolution": 300, "mode": "color", "copies": 1}),
            ):
                self.storage.save_profile(name, kind, settings)
        self.scan_sessions = ScanSessions(self.storage, self.jobs, validate_settings=self._scan_settings,
            capture_document=lambda values, event: self._scan_to_document(values, event, generate_preview=False),
            combine_pages=self._finish_scan_pages, queue_preview=self._queue_preview)
        self.backups = BackupService(self.storage, self.processor, self.validate_profile, self.export_settings)
        self.storage.prune()
        self._maintenance_stop = threading.Event()
        self._maintenance_worker = threading.Thread(target=self._maintain, name="dcp150c-retention", daemon=False)
        self._maintenance_worker.start()

    def snapshot(self) -> dict:
        capabilities = copy.deepcopy(self._capabilities)
        capabilities.update(scanner=self.scanner.capabilities(), authentication_configured=self.access.configured)
        return {"capabilities": capabilities, "documents": self.storage.list(), "jobs": self.jobs.list(),
                "profiles": self.storage.profiles(), "settings": self.storage.settings(),
                "scan_sessions": self.scan_sessions.list(), "backups": self.backups.list(),
                "backup_upload_limit": self.backups.maximum_upload_bytes}

    def create_backup(self, values: dict, key: str | None) -> dict:
        settings = self.backups.validate_selection(values)
        def reserve(_identifier):
            for identifier in settings["document_ids"]:
                self.storage.get(identifier)
        return self.jobs.submit("backup_export", settings,
            lambda event, _identifier: self.backups.export(settings, event), key, on_reserved=reserve)

    def restore_backup(self, archive_id: str, archive_sha256: str, restore_settings: bool, key: str | None) -> dict:
        self.require_documents()
        parameters = {"archive_sha256": archive_sha256, "restore_settings": restore_settings}
        def reserve(identifier):
            # Keep the transient path out of the request hash: a retry streams
            # identical bytes into a new file but must reuse the accepted job.
            self.storage.connection.execute("UPDATE jobs SET parameters=? WHERE id=?", (
                json.dumps({**parameters, "archive_id": archive_id}), identifier))
        return self.jobs.submit("backup_restore", parameters,
            lambda event, _identifier: self.backups.restore(archive_id, restore_settings, event), key, on_reserved=reserve)

    def require_documents(self) -> None:
        if self.processor is None or not self._capabilities["documents"].get("available"):
            raise StorageError("document_dependencies", "Install the PDF and image processing tools first.")

    def import_document(self, path: Path, name: str) -> dict:
        self.require_documents()
        metadata = self.processor.inspect(path)
        document = self.storage.add(path, name, metadata)
        self._queue_preview(document["id"])
        return document

    def _queue_preview(self, identifier: str) -> None:
        def action(event, _job_identifier):
            with self.storage.lease(identifier) as source:
                self.processor.preview(source, self.storage.path(identifier, "preview"), cancel_event=event)
            return {}
        try:
            self.jobs.submit("preview", {"document_id": identifier}, action)
        except StorageError:
            pass  # The file/download remains usable; another explicit edit can create a preview.

    @staticmethod
    def _print_settings(values: dict) -> dict:
        settings = {key: value for key, value in values.items() if key != "document_id"}
        for old, new in (("paper_size", "paper"), ("colour", "color"), ("page_ranges", "pages"), ("image_fit", "fit")):
            if old in settings:
                settings[new] = settings.pop(old)
        return settings

    @staticmethod
    def _scan_settings(values: dict) -> dict:
        if not isinstance(values, dict) or set(values) - {"resolution", "mode", "output_format", "area", "crop", "name"}:
            raise StorageError("invalid_scan_settings", "Choose supported scan settings.")
        settings = {"resolution": 300, "mode": "color", "output_format": "pdf", **values}
        if (type(settings["resolution"]) is not int or settings["resolution"] not in {75, 100, 150, 200, 300, 600}
                or not isinstance(settings["mode"], str) or settings["mode"] not in {"color", "gray", "lineart"}
                or not isinstance(settings["output_format"], str) or settings["output_format"] not in {"pdf", "png", "jpeg"}):
            raise StorageError("invalid_scan_settings", "Choose supported scan settings.")
        if settings.get("crop") is not None:
            from .document_processing import validated_crop
            validated_crop(settings["crop"])
        area = settings.get("area")
        if area is not None:
            import math
            if not isinstance(area, dict) or set(area) - {"left", "top", "width", "height"}:
                raise StorageError("invalid_scan_settings", "Choose a scan area inside the original glass.")
            for name, value in area.items():
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not (0 <= value <= 500) or (name in {"width", "height"} and value <= 0):
                    raise StorageError("invalid_scan_settings", "Choose a scan area inside the original glass.")
        return settings

    def print_document(self, values: dict, key: str | None) -> dict:
        self.require_documents()
        identifier = values.get("document_id", "")
        self.storage.get(identifier)
        settings = self.printing.validate_settings(self._print_settings(values))
        def action(event, job_identifier):
            def remember(result):
                self.jobs.remember_result(job_identifier, {"cups_job_id": result["job_id"]})
            with self.storage.lease(identifier) as source:
                result = self.printing.submit(source, settings, cancel_event=event, on_submitted=remember)
            return {"cups_job_id": result["job_id"]}
        return self.jobs.submit("print", {"document_id": identifier, **settings}, action, key)

    def detect_scanner(self, key: str | None) -> dict:
        return self.jobs.submit("scanner_detect", {}, lambda event, _identifier: {"scanner": self.scanner.probe(event)}, key)

    def _scan_to_document(self, values: dict, event: threading.Event, *, generate_preview: bool = True,
                          scan_kind: str = "scan") -> dict:
        self.require_documents()
        settings = self._scan_settings(values)
        with self.storage.workspace() as directory:
            scanned = directory / "scan.png"
            scan_result = self.scanner.scan(scanned, {key: value for key, value in settings.items() if key in {"resolution", "mode", "area"}}, event)
            if settings.get("crop") is not None:
                from .document_processing import validated_crop
                from PIL import Image
                import math
                left, top, width, height = validated_crop(settings["crop"])
                cropped_path = directory / "cropped.png"
                with Image.open(scanned) as image:
                    box = (math.floor(image.width * left), math.floor(image.height * top),
                           math.ceil(image.width * (left + width)), math.ceil(image.height * (top + height)))
                    image.crop(box).save(cropped_path, "PNG")
                scanned = cropped_path
            output = scanned
            if settings["output_format"] == "pdf":
                output = directory / "scan.pdf"
                self.processor.prepare(scanned, output, cancel_event=event)
            elif settings["output_format"] == "jpeg":
                output = directory / "scan.jpg"
                from PIL import Image
                with Image.open(scanned) as image:
                    image.convert("RGB").save(output, "JPEG", quality=95)
            name = settings.get("name") or ("Scan " + self._timestamp_name())
            scan_metadata = {**scan_result, "kind": scan_kind} if isinstance(scan_result, dict) and "area" in scan_result else None
            document = self.storage.add(output, name, self.processor.inspect(output), scan_metadata=scan_metadata)
        if generate_preview:
            self._make_preview(document["id"], event)
        return document

    def _finish_scan_pages(self, name: str, pages: list[dict], recognize_text: bool,
                           languages: str, event: threading.Event) -> dict:
        self.require_documents()
        with ExitStack() as leases, self.storage.workspace() as directory:
            prepared_pages = []
            for index, page in enumerate(pages):
                source = leases.enter_context(self.storage.lease(page["document_id"]))
                output = directory / ("page-" + str(index + 1) + ".pdf")
                self.processor.prepare(source, output, rotation=page["rotation"], crop=page.get("crop"), cancel_event=event)
                prepared_pages.append(output)
            combined = directory / "combined.pdf"
            self.processor.merge(prepared_pages, combined, cancel_event=event)
            text = None
            if recognize_text:
                recognized, text = directory / "searchable.pdf", directory / "text.txt"
                self.ocr.process(combined, recognized, text, languages, event)
                combined = recognized
            if event.is_set():
                raise StorageError("job_cancelled", "The operation was cancelled.")
            return self.storage.add(combined, name, self.processor.inspect(combined), text=text)

    @staticmethod
    def _timestamp_name() -> str:
        from datetime import datetime
        return datetime.now().strftime("%Y-%m-%d %H-%M-%S")

    def _make_preview(self, identifier: str, event: threading.Event) -> None:
        with self.storage.lease(identifier) as path:
            self.processor.preview(path, self.storage.path(identifier, "preview"), cancel_event=event)

    def scan(self, values: dict, key: str | None) -> dict:
        settings = self._scan_settings(values)
        return self.jobs.submit("scan", settings, lambda event, _identifier: {"document_id": self._scan_to_document(settings, event)["id"]}, key)

    def preview_scan(self, values: dict, key: str | None) -> dict:
        if not isinstance(values, dict) or set(values) - {"mode"} or not isinstance(values.get("mode", "color"), str) or values.get("mode", "color") not in {"color", "gray", "lineart"}:
            raise StorageError("invalid_scan_settings", "Choose a supported preview mode.")
        reserved = {}
        def reserve(_identifier):
            capabilities = self.scanner.capabilities()
            if not capabilities.get("available") or not capabilities.get("resolutions"):
                raise StorageError("scanner_not_found", "Check the scanner before requesting a preview.")
            area = capabilities["area"]
            reserved.update(settings={"mode": values.get("mode", "color"), "resolution": min(capabilities["resolutions"]),
                "output_format": "png", "area": {"left": 0, "top": 0, "width": area["width"], "height": area["height"]}})
        def action(event, _identifier):
            document = self._scan_to_document(reserved["settings"], event, scan_kind="preview")
            return {"document_id": document["id"]}
        return self.jobs.submit("scan_preview", values, action, key, on_reserved=reserve)

    def copy(self, values: dict, key: str | None) -> dict:
        self.require_documents()
        scan_values = {key: value for key, value in values.items() if key in {"resolution", "mode", "area", "crop", "name"}}
        scan_settings = self._scan_settings(scan_values)
        print_values = {key: value for key, value in values.items() if key not in scan_values and key != "output_format"}
        print_settings = self.printing.validate_settings(self._print_settings(print_values))
        def action(event, job_identifier):
            document = self._scan_to_document(scan_settings, event)
            if event.is_set():
                raise StorageError("job_cancelled", "The operation was cancelled.")
            self.jobs.remember_result(job_identifier, {"document_id": document["id"]})
            with self.storage.lease(document["id"]) as path:
                result = self.printing.submit(path, print_settings, cancel_event=event,
                    on_submitted=lambda result: self.jobs.remember_result(job_identifier, {"cups_job_id": result["job_id"]}))
            return {"document_id": document["id"], "cups_job_id": result["job_id"]}
        return self.jobs.submit("copy", {"scan": scan_settings, "print": print_settings}, action, key)

    def recognize(self, identifier: str, values: dict, key: str | None) -> dict:
        self.require_documents()
        original = self.storage.get(identifier)
        if set(values) - {"languages"}:
            raise StorageError("invalid_settings", "Choose supported text recognition settings.")
        languages = values.get("languages", "deu+eng")
        if not isinstance(languages, str) or len(languages) > 80:
            raise StorageError("ocr_languages", "Choose installed OCR languages.")
        def action(event, _identifier):
            with self.storage.lease(identifier) as source, self.storage.workspace() as directory:
                output, text = directory / "recognized.pdf", directory / "recognized.txt"
                self.ocr.process(source, output, text, languages, event)
                document = self.storage.add(output, original["name"] + " · OCR", self.processor.inspect(output), text=text)
            self._make_preview(document["id"], event)
            return {"document_id": document["id"]}
        return self.jobs.submit("ocr", {"document_id": identifier, "languages": languages}, action, key)

    def transform(self, identifier: str, values: dict, key: str | None) -> dict:
        self.require_documents()
        original = self.storage.get(identifier)
        if set(values) - {"pages", "rotation", "crop", "output_format"} or values.get("output_format", "pdf") != "pdf":
            raise StorageError("invalid_settings", "Save edited pages as a PDF document.")
        def action(event, _identifier):
            with self.storage.lease(identifier) as source, self.storage.workspace() as directory:
                output = directory / "edited.pdf"
                self.processor.prepare(source, output, pages=values.get("pages"), rotation=values.get("rotation", 0),
                                       crop=values.get("crop"), paper=None, fit="original", cancel_event=event)
                document = self.storage.add(output, original["name"] + " · Edited", self.processor.inspect(output))
            self._make_preview(document["id"], event)
            return {"document_id": document["id"]}
        return self.jobs.submit("transform", {"document_id": identifier, **values}, action, key)

    def merge(self, values: dict, key: str | None) -> dict:
        self.require_documents()
        identifiers = values.get("document_ids")
        if (set(values) - {"document_ids", "name"} or not isinstance(identifiers, list)
                or not 2 <= len(identifiers) <= 25 or any(not isinstance(value, str) for value in identifiers)):
            raise StorageError("invalid_pages", "Choose between two and 25 documents in page order.")
        if sum(self.storage.get(identifier)["pages"] for identifier in identifiers) > 25:
            raise StorageError("document_page_limit", "A merged document can contain up to 25 pages.")
        name = document_name(values.get("name", "Combined document"))
        def action(event, _identifier):
            with ExitStack() as leases, self.storage.workspace() as directory:
                paths = [leases.enter_context(self.storage.lease(identifier)) for identifier in identifiers]
                output = directory / "combined.pdf"
                self.processor.merge(paths, output, cancel_event=event)
                document = self.storage.add(output, name, self.processor.inspect(output))
            self._make_preview(document["id"], event)
            return {"document_id": document["id"]}
        return self.jobs.submit("merge", {"document_ids": identifiers, "name": name}, action, key)

    def cancel(self, identifier: str) -> dict:
        job = self.jobs.cancel(identifier)
        self.scan_sessions.recover()
        if job.get("cups_job_id") and self.printing:
            self.printing.cancel(job["cups_job_id"])
            self.jobs.mark_cancelled(identifier)
        return self.jobs.get(identifier)

    def validate_profile(self, values: dict) -> dict:
        if not isinstance(values, dict) or set(values) - {"name", "kind", "settings", "id"}:
            raise StorageError("invalid_profile", "Choose a supported profile.")
        name = document_name(values.get("name", ""))
        kind, settings = values.get("kind"), values.get("settings", {})
        if not isinstance(settings, dict):
            raise StorageError("invalid_profile", "Choose supported profile settings.")
        if kind == "print":
            self.require_documents()
            settings = self.printing.validate_settings(self._print_settings(settings))
        elif kind == "scan":
            settings = self._scan_settings(settings)
        elif kind == "ocr":
            if set(settings) != {"languages"} or not isinstance(settings["languages"], str) or len(settings["languages"]) > 80:
                raise StorageError("ocr_languages", "Choose installed OCR languages.")
        elif kind == "copy":
            # Copy profiles use the same validated fields as their two operations.
            scan_values = {key: value for key, value in settings.items() if key in {"resolution", "mode", "area", "crop", "name"}}
            self._scan_settings(scan_values)
            self.require_documents()
            self.printing.validate_settings(self._print_settings({key: value for key, value in settings.items() if key not in scan_values}))
        else:
            raise StorageError("invalid_profile", "Choose a supported profile type.")
        return {"name": name, "kind": kind, "settings": settings}

    def save_profile(self, values: dict) -> dict:
        profile = self.validate_profile(values)
        return self.storage.save_profile(**profile, identifier=values.get("id"))

    def export_settings(self) -> dict:
        return {"format": 1, "settings": self.storage.settings(), "profiles": [
            {key: profile[key] for key in ("name", "kind", "settings")} for profile in self.storage.profiles()]}

    def import_settings(self, values: dict) -> dict:
        if (not isinstance(values, dict) or set(values) != {"format", "settings", "profiles"}
                or values["format"] != 1 or not isinstance(values["profiles"], list)
                or len(values["profiles"]) + len(self.storage.profiles()) > 50):
            raise StorageError("invalid_settings", "Choose a valid Control Center settings export.")
        settings = self.storage.validate_settings(values["settings"])
        profiles = [self.validate_profile(profile) for profile in values["profiles"]]
        self.storage.update_settings(settings)
        for profile in profiles:
            self.storage.save_profile(**profile)
        return {"settings": self.storage.settings(), "profiles": self.storage.profiles()}

    def close(self) -> None:
        self.jobs.close()
        self._maintenance_stop.set()
        self._maintenance_worker.join()
        self.storage.close()

    def _maintain(self) -> None:
        """Refresh known print jobs and retention without USB traffic or HTTP work."""
        while not self._maintenance_stop.wait(5):
            try:
                self.scan_sessions.recover()
                self.storage.prune()
                self.backups.prune()
                if not self.printing:
                    continue
                for job in self.jobs.unfinished_print_jobs():
                    if self._maintenance_stop.is_set():
                        break
                    if not job["cups_job_id"] or job["state"] not in {"submitted", "interrupted"}:
                        continue
                    try:
                        current = self.printing.job_status(job["cups_job_id"])
                    except (ValueError, RuntimeError):
                        continue  # A temporarily unreachable scheduler does not finish a job.
                    state = current["state"]
                    states = {3: ("submitted", "print_queued"), 4: ("submitted", "print_held"),
                              5: ("submitted", "print_processing"), 6: ("submitted", "print_stopped"),
                              7: ("cancelled", "job_cancelled"), 8: ("failed", "print_aborted"),
                              9: ("succeeded", "print_completed")}
                    if state in states:
                        next_state, code = states[state]
                        if job["state"] == "interrupted" and state == 4:
                            next_state = "interrupted"
                        self.jobs._update(job["id"], next_state, code, {"cups_state": state})
            except OSError:
                pass  # Read-only state remains available until the next maintenance attempt.

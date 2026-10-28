# SPDX-License-Identifier: GPL-2.0-or-later
"""Explicit, validated print submission through the installed native CUPS queue."""
from __future__ import annotations

from pathlib import Path
import re
import shutil
import tempfile
from urllib.parse import unquote, urlsplit

from .command_runner import CommandError, run_command
from .status import CupsClient, StatusError, _first
from .document_processing import (DocumentError, DocumentProcessor, PAPER_DIMENSIONS,
                                  validated_crop, validated_rotation)

MEDIA_QUALITIES = {
    'Plain': ['Draft', 'Fast', 'Normal', 'Fine'],
    'Inkjet': ['Fine', 'Photo'],
    'BrotherGlossy': ['Fine', 'Photo', 'Highest'],
    'Glossy': ['Fine', 'Photo', 'Highest'],
    'Transparencies': ['Normal', 'Fine'],
}


class PrintingError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def validate_settings(settings: dict | None = None) -> dict:
    if settings is None:
        settings = {}
    if not isinstance(settings, dict):
        raise PrintingError('invalid_print_settings', 'Choose valid print settings.')
    allowed = {'copies', 'paper', 'media', 'quality', 'color', 'pages', 'rotation', 'fit', 'crop'}
    if set(settings) - allowed:
        raise PrintingError('invalid_print_settings', 'An unsupported print setting was supplied.')
    result = {'copies': 1, 'paper': 'A4', 'media': 'Plain', 'quality': 'Normal',
              'color': 'Color', 'pages': None, 'rotation': 0, 'fit': 'fit', 'crop': None,
              **settings}
    if type(result['copies']) is not int or not 1 <= result['copies'] <= 99:
        raise PrintingError('invalid_copies', 'Choose between one and 99 copies.')
    if not isinstance(result['paper'], str) or result['paper'] not in PAPER_DIMENSIONS:
        raise PrintingError('invalid_paper', 'Choose a supported paper size.')
    if not isinstance(result['media'], str) or result['media'] not in MEDIA_QUALITIES:
        raise PrintingError('invalid_media', 'Choose a supported paper material.')
    if result['quality'] not in MEDIA_QUALITIES[result['media']]:
        raise PrintingError('invalid_quality', 'Choose a quality supported by the paper material.')
    aliases = {'color': 'Color', 'colour': 'Color', 'monochrome': 'Mono',
               'grayscale': 'Mono', 'greyscale': 'Mono', 'Mono': 'Mono', 'Color': 'Color'}
    try:
        result['color'] = aliases[result['color']]
    except (KeyError, TypeError) as exc:
        raise PrintingError('invalid_colour', 'Choose colour or grayscale printing.') from exc
    if result['fit'] not in ('fit', 'fill'):
        raise PrintingError('invalid_fit', 'Choose fitting or filling the paper.')
    try:
        result['rotation'] = validated_rotation(result['rotation'])
        validated_crop(result['crop'])
    except DocumentError as exc:
        raise PrintingError(exc.code, str(exc)) from exc
    # Actual page bounds are validated against the source by DocumentProcessor.
    if result['pages'] is not None and not isinstance(result['pages'], (str, list)):
        raise PrintingError('invalid_pages', 'Choose existing document pages.')
    return result


class PrintingService:
    def __init__(self, queue: str = 'DCP150C-Native', command_runner=run_command):
        if not isinstance(queue, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,126}', queue):
            raise PrintingError('invalid_queue', 'The printer queue name is invalid.')
        self.queue = queue
        self.command_runner = command_runner
        self.documents = DocumentProcessor(command_runner)
        self.cups = CupsClient(timeout=2.0)

    @staticmethod
    def validate_settings(settings=None):
        return validate_settings(settings)

    def capabilities(self) -> dict:
        return {'available': all(shutil.which(command) for command in ('lp', 'cancel', 'lpstat'))
                and self.documents.capabilities()['available'], 'queue': self.queue,
                'paper_sizes': list(PAPER_DIMENSIONS), 'media_qualities': MEDIA_QUALITIES,
                'colour_modes': ['Color', 'Mono'], 'maximum_copies': 99}

    def _job_identifier(self, job_id) -> str:
        if not isinstance(job_id, str) or not re.fullmatch(re.escape(self.queue) + r'-[1-9][0-9]{0,9}', job_id):
            raise PrintingError('invalid_print_job', 'Choose a print job from this printer queue.')
        return job_id

    def submit(self, path, settings=None, cancel_event=None, on_submitted=None) -> dict:
        """Prepare a document, create a held job, record its ID, then release it.

        ``on_submitted`` can persist the identifier before printing is released.
        A refresh or a download never calls this method; callers need an explicit
        user action and their own durable idempotency key.
        """
        options = validate_settings(settings)
        path = Path(path)
        job_id = None
        try:
            with tempfile.TemporaryDirectory(prefix='print-', dir=path.parent) as directory:
                prepared = Path(directory) / 'document.pdf'
                self.documents.prepare(path, prepared, pages=options['pages'], rotation=options['rotation'],
                                       paper=options['paper'], fit=options['fit'], crop=options['crop'],
                                       cancel_event=cancel_event)
                if cancel_event is not None and cancel_event.is_set():
                    raise PrintingError('job_cancelled', 'The operation was cancelled.')
                arguments = ['lp', '-d', self.queue, '-n', str(options['copies']), '-H', 'hold',
                             '-t', 'DCP-150C Control Center', '-o', 'PageSize=' + options['paper'],
                             '-o', 'Media=' + options['media'], '-o', 'PrintQuality=' + options['quality'],
                             '-o', 'ColorModel=' + ('RGB' if options['color'] == 'Color' else 'Gray'),
                             str(prepared.resolve())]
                # Holding the job makes a lost submission response harmless: it
                # cannot reach the device until the known identifier is released.
                response = self.command_runner(arguments, timeout=20)
                match = re.search(rb'request id is (' + re.escape(self.queue.encode('ascii')) + rb'-[1-9][0-9]*)\b',
                                  response.stdout)
                if not match:
                    raise PrintingError('print_submission_uncertain',
                                        'The print service did not return a job identifier. Check the queue before retrying.')
                job_id = self._job_identifier(match.group(1).decode('ascii'))
                result = {'job_id': job_id, 'queue': self.queue, 'state': 'held'}
                if on_submitted is not None:
                    on_submitted(result.copy())
                if cancel_event is not None and cancel_event.is_set():
                    raise PrintingError('job_cancelled', 'The operation was cancelled.')
                self.command_runner(['lp', '-i', job_id, '-H', 'resume'], timeout=20)
                if cancel_event is not None and cancel_event.is_set():
                    raise PrintingError('job_cancelled', 'The operation was cancelled.')
                result['state'] = 'submitted'
                return result
        except (DocumentError, CommandError) as exc:
            if job_id:
                self._cancel_after_failure(job_id)
            raise PrintingError(exc.code, str(exc)) from exc
        except Exception:
            if job_id:
                self._cancel_after_failure(job_id)
            raise

    def _cancel_after_failure(self, job_id):
        try:
            self.cancel(job_id)
        except (PrintingError, CommandError):
            pass

    def cancel(self, job_id) -> dict:
        job_id = self._job_identifier(job_id)
        try:
            self.command_runner(['cancel', job_id], timeout=15)
        except CommandError as exc:
            raise PrintingError(exc.code, str(exc)) from exc
        return {'job_id': job_id, 'state': 'cancel_requested'}

    def job_status(self, job_id) -> dict:
        """Read one scheduler state, retaining the distinction from physical output."""
        job_id = self._job_identifier(job_id)
        numeric_identifier = job_id.rsplit('-', 1)[1]
        path = '/jobs/' + numeric_identifier
        try:
            groups = self.cups._request(0x0009, path, [
                (0x45, 'job-uri', ['ipp://localhost' + path]),
                (0x44, 'requested-attributes', ['job-state', 'job-state-reasons', 'job-printer-uri']),
            ])
            attributes = next((values for tag, values in groups if tag == 2), None)
            if attributes is None:
                raise StatusError('The print service did not return this job.')
            printer_uri = str(_first(attributes, 'job-printer-uri', ''))
            printer_path = unquote(urlsplit(printer_uri).path)
            if printer_path != '/printers/' + self.queue:
                raise StatusError('The job does not belong to this printer queue.')
            state = _first(attributes, 'job-state')
            if type(state) is not int or not 3 <= state <= 9:
                raise StatusError('The print service returned an unknown job state.')
            reasons = attributes.get('job-state-reasons', [])
            if not isinstance(reasons, list) or len(reasons) > 32 or any(
                    not isinstance(reason, str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,127}', reason)
                    for reason in reasons):
                raise StatusError('The print service returned invalid job reasons.')
            return {'job_id': job_id, 'state': state, 'reason_codes': reasons}
        except (StatusError, ValueError) as exc:
            raise PrintingError('print_status_unavailable', 'The print job status is unavailable.') from exc

    def jobs(self) -> list[dict]:
        """Read current queue entries; an absent job is not proof of paper output."""
        try:
            response = self.command_runner(['lpstat', '-W', 'not-completed', '-o', self.queue], timeout=10)
        except CommandError as exc:
            raise PrintingError(exc.code, str(exc)) from exc
        result = []
        for line in response.stdout.decode('utf-8', errors='replace').splitlines():
            fields = line.split()
            if fields and re.fullmatch(re.escape(self.queue) + r'-[1-9][0-9]{0,9}', fields[0]):
                result.append({'job_id': fields[0], 'owner': fields[1] if len(fields) > 1 else '',
                               'state': 'queued'})
        return result[:100]

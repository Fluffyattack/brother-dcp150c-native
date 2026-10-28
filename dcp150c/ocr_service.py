# SPDX-License-Identifier: GPL-2.0-or-later
"""Local neural OCR with searchable PDFs and a plain-text sidecar.

Tesseract's LSTM engine processes one page at a time with one CPU thread. This
keeps memory bounded on small Raspberry Pis, and keeps documents on the host.
"""
from __future__ import annotations

import math
from pathlib import Path
import re
import shutil
import tempfile
import threading

from .command_runner import CommandError, run_command
from .document_processing import DocumentError, DocumentProcessor, MAX_DOCUMENT_BYTES


class OCRError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class OCRService:
    def __init__(self, command_runner=run_command):
        self.command_runner = command_runner
        self.documents = DocumentProcessor(command_runner)
        self._capabilities_cache = None
        self._capabilities_guard = threading.Lock()

    def capabilities(self, *, refresh=False) -> dict:
        with self._capabilities_guard:
            if self._capabilities_cache is not None and not refresh:
                return {**self._capabilities_cache, 'languages': self._capabilities_cache['languages'].copy()}
            result = self._read_capabilities()
            if result['available']:
                self._capabilities_cache = {**result, 'languages': result['languages'].copy()}
            return result

    def _read_capabilities(self) -> dict:
        result = {'available': False, 'engine': 'tesseract', 'neural_engine': 'LSTM',
                  'languages': [], 'searchable_pdf': True, 'local_processing': True}
        if not all(shutil.which(command) for command in ('tesseract', 'pdftoppm')):
            result['reason'] = 'ocr_dependencies'
            return result
        try:
            version = self.command_runner(['tesseract', '--version'], timeout=30)
            match = re.search(rb'tesseract\s+(\d+(?:\.\d+){1,2})', version.stdout + b'\n' + version.stderr)
            if not match or int(match.group(1).split(b'.')[0]) < 4:
                result['reason'] = 'ocr_dependencies'
                return result
            language_result = self.command_runner(['tesseract', '--list-langs'], timeout=30)
            installed = (language_result.stdout + b'\n' + language_result.stderr).decode('utf-8', errors='replace').splitlines()
            result['languages'] = sorted({line.strip() for line in installed
                                          if re.fullmatch(r'[a-z]{3}(?:_[a-z0-9]+)?', line.strip())
                                          and line.strip() != 'osd'})
            result['version'] = match.group(1).decode('ascii')
            result['available'] = bool(result['languages']) and self.documents.capabilities()['available']
        except (CommandError, OSError):
            result['reason'] = 'ocr_dependencies'
        return result

    def process(self, input_path, output_pdf, output_text, languages='deu+eng',
                cancel_event=None, progress=None) -> dict:
        """Recognize every page and write a real searchable PDF plus UTF-8 text."""
        from pypdf import PdfReader, PdfWriter
        input_path, output_pdf, output_text = map(Path, (input_path, output_pdf, output_text))
        if len({path.resolve() for path in (input_path, output_pdf, output_text)}) != 3:
            raise OCRError('document_output', 'Save OCR results as separate files.')
        capabilities = self.capabilities()
        if not capabilities['available']:
            raise OCRError('ocr_dependencies', 'Install Tesseract, its language data and PDF tools first.')
        if isinstance(languages, list):
            selected_languages = languages
        elif isinstance(languages, str) and len(languages) <= 80:
            selected_languages = languages.split('+')
        else:
            selected_languages = []
        if (not 1 <= len(selected_languages) <= 3 or
                len(set(selected_languages)) != len(selected_languages) or
                any(language not in capabilities['languages'] for language in selected_languages)):
            raise OCRError('ocr_languages', 'Choose up to three installed OCR languages.')
        try:
            metadata = self.documents.inspect(input_path)
            originals = PdfReader(str(input_path)) if metadata['media_type'] == 'application/pdf' else None
            writer = PdfWriter()
            character_count = 0
            adaptive_retry_pages = adaptive_recovered_pages = 0
            with tempfile.TemporaryDirectory(prefix='ocr-', dir=output_pdf.parent) as directory:
                work = Path(directory)
                text_pages = []
                for index in range(metadata['page_count']):
                    if cancel_event is not None and cancel_event.is_set():
                        raise OCRError('job_cancelled', 'The operation was cancelled.')
                    image = work / 'page.png'
                    original_width = original_height = None
                    if originals is not None:
                        original = originals.pages[index]
                        if original.rotation:
                            original.transfer_rotation_to_content()
                        original_width = float(original.mediabox.width)
                        original_height = float(original.mediabox.height)
                        # Cap page rendering at 25 megapixels, even for oversized PDFs.
                        dpi = min(300, max(36, math.floor(5000 * 72 / max(original_width, original_height))))
                        self.command_runner(['pdftoppm', '-f', str(index + 1), '-singlefile', '-r',
                                             str(dpi), '-png', str(input_path.resolve()), str(work / 'page')],
                                            timeout=120, cancel_event=cancel_event)
                    else:
                        with self.documents._image(input_path) as loaded:
                            if max(loaded.size) > 5000:
                                loaded.thumbnail((5000, 5000))
                            loaded.save(image, 'PNG')
                    if not image.is_file() or image.stat().st_size > MAX_DOCUMENT_BYTES:
                        raise OCRError('ocr_page_size', 'A rendered OCR page exceeds the processing limit.')
                    basename = work / 'recognized'
                    self.command_runner(['tesseract', str(image), str(basename), '-l',
                                         '+'.join(selected_languages), '--oem', '1', '--psm', '3',
                                         '--dpi', '300', 'pdf', 'txt'], timeout=240,
                                        cancel_event=cancel_event, env={'OMP_THREAD_LIMIT': '1'})
                    text_file = basename.with_suffix('.txt')
                    if not text_file.is_file() or text_file.stat().st_size > 2 * 1024 * 1024:
                        raise OCRError('ocr_failed', 'OCR text output exceeds the processing limit.')
                    recognized_text = text_file.read_text(encoding='utf-8')
                    recognized = PdfReader(str(basename.with_suffix('.pdf')))
                    if len(recognized.pages) != 1:
                        raise OCRError('ocr_failed', 'OCR did not produce a complete page.')
                    if cancel_event is not None and cancel_event.is_set():
                        raise OCRError('job_cancelled', 'The operation was cancelled.')
                    # Tesseract 5's local thresholding can recover text that
                    # global thresholding misses on uneven or dark backgrounds.
                    # Retry only an empty result, once, on the same saved image.
                    major_version = int(capabilities.get('version', '0').split('.')[0])
                    if not recognized_text.strip() and major_version >= 5:
                        adaptive_retry_pages += 1
                        adaptive = work / 'recognized-adaptive'
                        self.command_runner(['tesseract', str(image), str(adaptive), '-l',
                            '+'.join(selected_languages), '--oem', '1', '--psm', '3',
                            '--dpi', '300', '-c', 'thresholding_method=2', 'pdf', 'txt'],
                            timeout=240, cancel_event=cancel_event, env={'OMP_THREAD_LIMIT': '1'})
                        adaptive_text = adaptive.with_suffix('.txt')
                        if not adaptive_text.is_file() or adaptive_text.stat().st_size > 2 * 1024 * 1024:
                            raise OCRError('ocr_failed', 'OCR text output exceeds the processing limit.')
                        recovered_text = adaptive_text.read_text(encoding='utf-8')
                        recovered = PdfReader(str(adaptive.with_suffix('.pdf')))
                        if len(recovered.pages) != 1:
                            raise OCRError('ocr_failed', 'OCR did not produce a complete page.')
                        if recovered_text.strip():
                            recognized_text, recognized = recovered_text, recovered
                            adaptive_recovered_pages += 1
                    page = recognized.pages[0]
                    if original_width is not None:
                        page.scale_to(original_width, original_height)
                    writer.add_page(page)
                    character_count += len(recognized_text)
                    text_pages.append(recognized_text)
                    if progress is not None:
                        progress(index + 1, metadata['page_count'])
                    image.unlink(missing_ok=True)
                writer.add_metadata({'/Producer': 'Brother DCP-150C Native Control Center · Tesseract LSTM'})
                with output_pdf.open('wb') as output:
                    writer.write(output)
                output_text.write_text('\n\f\n'.join(text_pages), encoding='utf-8')
            self.documents.inspect(output_pdf)
            return {'pdf': str(output_pdf), 'text': str(output_text), 'pages': metadata['page_count'],
                    'engine': 'tesseract', 'languages': selected_languages, 'characters': character_count,
                    'adaptive_retry_pages': adaptive_retry_pages, 'adaptive_recovered_pages': adaptive_recovered_pages}
        except (DocumentError, CommandError) as exc:
            output_pdf.unlink(missing_ok=True)
            output_text.unlink(missing_ok=True)
            raise OCRError(exc.code, str(exc)) from exc
        except OCRError:
            output_pdf.unlink(missing_ok=True)
            output_text.unlink(missing_ok=True)
            raise
        except Exception as exc:
            output_pdf.unlink(missing_ok=True)
            output_text.unlink(missing_ok=True)
            raise OCRError('ocr_failed', 'The document could not be recognized.') from exc

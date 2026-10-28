"""Offline document checks. No test submits a job to a printer or scanner."""
from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest

from PIL import Image, ImageDraw, ImageFont
from pypdf import PdfReader, PdfWriter

from dcp150c.document_processing import DocumentError, DocumentProcessor
from dcp150c.ocr_service import OCRError, OCRService
from dcp150c.printing_service import PrintingError, PrintingService, validate_settings


class DocumentWorkflows(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.processor = DocumentProcessor()
        self.red = self.root / 'red.png'
        self.blue = self.root / 'blue.png'
        for path, colour in ((self.red, 'red'), (self.blue, 'blue')):
            with Image.new('RGB', (600, 900), colour) as image:
                image.save(path)
        self.original = self.root / 'two-pages.pdf'
        self.processor.merge([self.red, self.blue], self.original)

    def tearDown(self):
        self.directory.cleanup()

    def test_reorder_rotate_crop_changes_real_rendering(self):
        prepared = self.root / 'prepared.pdf'
        self.processor.prepare(self.original, prepared, pages=[2, 1], rotation=90,
                               paper=None, crop={'left': 0.1, 'top': 0.2, 'right': 0.9, 'bottom': 0.8})
        reader = PdfReader(prepared)
        self.assertEqual(len(reader.pages), 2)
        self.assertAlmostEqual(float(reader.pages[0].mediabox.width), 172.8, places=1)
        self.assertAlmostEqual(float(reader.pages[0].mediabox.height), 86.4, places=1)
        for number, dominant in ((1, 2), (2, 0)):
            preview = self.root / f'preview-{number}.png'
            self.processor.preview(prepared, preview, page=number)
            with Image.open(preview) as image:
                centre = image.convert('RGB').getpixel((image.width // 2, image.height // 2))
                self.assertGreater(centre[dominant], 200)
                self.assertLess(sum(centre) - centre[dominant], 70)

    def test_image_fitting_and_transparency(self):
        transparent = self.root / 'transparent.png'
        with Image.new('RGBA', (400, 200), (255, 0, 0, 0)) as image:
            image.save(transparent)
        prepared = self.root / 'transparent.pdf'
        metadata = self.processor.prepare(transparent, prepared, paper='A4', fit='fill')
        self.assertEqual((metadata['width'], metadata['height']), (595, 842))
        preview = self.root / 'transparent-preview.png'
        self.processor.preview(prepared, preview)
        with Image.open(preview) as image:
            self.assertGreater(min(image.convert('RGB').getpixel((200, 200))), 245)

    def test_malformed_and_encrypted_documents_rejected(self):
        malformed = self.root / 'bad.pdf'
        malformed.write_bytes(b'%PDF-1.7\nnot a valid document')
        with self.assertRaises(DocumentError):
            self.processor.inspect(malformed)
        encrypted = self.root / 'encrypted.pdf'
        writer = PdfWriter()
        writer.add_blank_page(595, 842)
        writer.encrypt('password')
        with encrypted.open('wb') as output:
            writer.write(output)
        with self.assertRaises(DocumentError) as failure:
            self.processor.inspect(encrypted)
        self.assertEqual(failure.exception.code, 'encrypted_document')

    def test_page_limits_and_invalid_crop_rejected(self):
        for pages in ([0], [3], [True], '1-99999999'):
            with self.assertRaises(DocumentError):
                self.processor.prepare(self.original, self.root / 'invalid.pdf', pages=pages)
        with self.assertRaises(DocumentError):
            self.processor.prepare(self.original, self.root / 'invalid.pdf',
                                   crop={'left': 0.9, 'top': 0, 'right': 0.1, 'bottom': 1})
        many_pages = self.root / 'too-many.pdf'
        writer = PdfWriter()
        for _ in range(26):
            writer.add_blank_page(595, 842)
        with many_pages.open('wb') as output:
            writer.write(output)
        with self.assertRaises(DocumentError):
            self.processor.inspect(many_pages)

    def test_held_print_has_prepared_pages_and_persisted_identifier(self):
        commands = []
        recorded = []
        def runner(arguments, **_kwargs):
            commands.append(arguments)
            if '-d' in arguments:
                self.assertEqual(len(PdfReader(arguments[-1]).pages), 1)
                return subprocess.CompletedProcess(arguments, 0,
                                                   b'request id is DCP150C-Native-42 (1 file(s))\n', b'')
            self.assertEqual(recorded[0]['state'], 'held')
            return subprocess.CompletedProcess(arguments, 0, b'', b'')
        service = PrintingService(command_runner=runner)
        result = service.submit(self.original, {'copies': 2, 'pages': [2], 'rotation': 90},
                                on_submitted=recorded.append)
        self.assertEqual(result['job_id'], 'DCP150C-Native-42')
        self.assertIn('hold', commands[0])
        self.assertIn('resume', commands[1])
        self.assertEqual(commands[0][commands[0].index('-n') + 1], '2')

    def test_cancelled_held_job_is_never_released(self):
        commands = []
        cancellation = threading.Event()
        def runner(arguments, **_kwargs):
            commands.append(arguments)
            if '-d' in arguments:
                cancellation.set()
                return subprocess.CompletedProcess(arguments, 0, b'request id is DCP150C-Native-43\n', b'')
            return subprocess.CompletedProcess(arguments, 0, b'', b'')
        with self.assertRaises(PrintingError) as failure:
            PrintingService(command_runner=runner).submit(self.original, cancel_event=cancellation)
        self.assertEqual(failure.exception.code, 'job_cancelled')
        self.assertEqual(commands[-1], ['cancel', 'DCP150C-Native-43'])
        self.assertFalse(any('resume' in command for command in commands))

    def test_print_validation_rejects_unsafe_or_unsupported_options(self):
        for settings in ({'quality': 'Highest', 'media': 'Plain'}, {'copies': True},
                         {'copies': 100}, {'paper': '-o malicious'}, {'command': 'anything'}):
            with self.assertRaises(PrintingError):
                validate_settings(settings)
        with self.assertRaises(PrintingError):
            PrintingService().cancel('Another-Queue-42')

    def test_merge_api_honours_cancellation_between_real_documents(self):
        cancellation = threading.Event()
        original_prepare = self.processor.prepare
        def prepare_first_then_cancel(*args, **kwargs):
            result = original_prepare(*args, **kwargs)
            cancellation.set()
            return result
        self.processor.prepare = prepare_first_then_cancel
        output = self.root / 'cancelled-merge.pdf'
        with self.assertRaises(DocumentError) as failure:
            self.processor.merge([self.original, self.original], output, cancel_event=cancellation)
        self.assertEqual(failure.exception.code, 'job_cancelled')
        self.assertFalse(output.exists())

    def test_ocr_capabilities_cache_success_but_allow_explicit_refresh(self):
        calls = []
        def runner(arguments, **kwargs):
            calls.append((arguments, kwargs))
            # Exercise installations that report version/languages on stderr.
            output = b'tesseract 5.5.0\n' if '--version' in arguments else b'eng\ndeu\nosd\n'
            return subprocess.CompletedProcess(arguments, 0, b'', output)
        service = OCRService(runner)
        self.assertTrue(service.capabilities()['available'])
        self.assertTrue(service.capabilities()['available'])
        self.assertEqual(len(calls), 2)
        self.assertEqual(service.capabilities()['languages'], ['deu', 'eng'])
        service.capabilities(refresh=True)
        self.assertEqual(len(calls), 4)
        self.assertTrue(all(options['timeout'] == 30 for _, options in calls))

    def test_ipp_job_states_and_queue_boundary(self):
        service = PrintingService()
        calls = []
        class FakeCups:
            state = 9
            queue = 'DCP150C-Native'
            def _request(fake, operation, path, attributes):
                calls.append((operation, path, attributes))
                return [(2, {'job-state': [fake.state],
                             'job-printer-uri': ['ipp://localhost:631/printers/' + fake.queue],
                             'job-state-reasons': ['job-completed-successfully']})]
        service.cups = FakeCups()
        for state in (7, 8, 9):
            service.cups.state = state
            self.assertEqual(service.job_status('DCP150C-Native-42')['state'], state)
        self.assertEqual(calls[0][0:2], (0x0009, '/jobs/42'))
        self.assertIn((0x45, 'job-uri', ['ipp://localhost/jobs/42']), calls[0][2])
        service.cups.queue = 'Other-Printer'
        with self.assertRaises(PrintingError) as failure:
            service.job_status('DCP150C-Native-42')
        self.assertEqual(failure.exception.code, 'print_status_unavailable')
        with self.assertRaises(PrintingError):
            service.job_status('Other-Printer-42')

    @unittest.skipUnless(all(shutil.which(name) for name in ('tesseract', 'pdftotext', 'pdftoppm')),
                         'Tesseract and PDF tools are needed for actual OCR checks')
    def test_real_neural_ocr_creates_searchable_multipage_pdf(self):
        source = self.root / 'document.png'
        font = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 48)
        with Image.new('RGB', (1800, 2400), 'white') as image:
            draw = ImageDraw.Draw(image)
            for row, text in enumerate(('Invoice 2026', 'Rechnung Nummer 12345',
                                        'Total 123.45 EUR', 'Thank you for your order.')):
                draw.text((140, 180 + row * 100), text, fill='black', font=font)
            image.save(source)
        two_pages = self.root / 'scanned.pdf'
        self.processor.merge([source, source], two_pages)
        searchable, text = self.root / 'searchable.pdf', self.root / 'recognized.txt'
        service = OCRService()
        self.assertIn('deu', service.capabilities()['languages'])
        result = service.process(two_pages, searchable, text)
        self.assertEqual(result['pages'], 2)
        self.assertIn('12345', text.read_text())
        extracted = subprocess.run(['pdftotext', str(searchable), '-'], capture_output=True,
                                   check=True, timeout=20).stdout.decode('utf-8')
        self.assertEqual(extracted.count('Invoice 2026'), 2)
        self.assertIn('123.45', extracted)
        self.assertEqual(len(PdfReader(searchable).pages), 2)
        cancelled = threading.Event()
        cancelled.set()
        with self.assertRaises(OCRError) as failure:
            service.process(two_pages, self.root / 'cancelled.pdf', self.root / 'cancelled.txt',
                            cancel_event=cancelled)
        self.assertEqual(failure.exception.code, 'job_cancelled')
        self.assertFalse((self.root / 'cancelled.pdf').exists())


if __name__ == '__main__':
    unittest.main()

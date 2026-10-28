# SPDX-License-Identifier: GPL-2.0-or-later
"""Bounded PDF and image preparation for the browser Control Center.

Page numbers start at one. Cropping uses fractions of the displayed page, from
its upper left corner; ``left``, ``top``, ``width`` and ``height`` are in 0–1.
"""
from __future__ import annotations

from contextlib import contextmanager
from io import BytesIO
import math
from pathlib import Path
import tempfile
import warnings

from .command_runner import CommandError, run_command

MAX_DOCUMENT_BYTES = 20 * 1024 * 1024
MAX_DOCUMENT_PAGES = 25
MAX_IMAGE_PIXELS = 40_000_000
PAPER_DIMENSIONS = {
    'A4': (595, 842), 'Letter': (612, 792), 'Legal': (612, 1008),
    'Executive': (522, 756), 'B5': (516, 729), 'A5': (420, 595),
    'A6': (298, 419), 'PostC4x6': (288, 432), 'IndexC5x8': (360, 576),
    'PhotoL': (252, 360), 'Photo2L': (359, 505), 'PostCard': (283, 419),
    'DoublePostCardRotated': (420, 567), 'EnvC5': (459, 649),
    'EnvDL': (312, 624), 'Env10': (297, 684), 'EnvMonarch': (279, 540),
    'EnvYou4': (297, 664), 'BrA4_B': (621, 868), 'BrLetter_B': (638, 818),
    'BrA6_B': (325, 445), 'BrPostC4x6_B': (314, 458),
    'BrIndexC5x8_B': (386, 602), 'BrPhotoL_B': (279, 386),
    'BrPhoto2L_B': (386, 531), 'BrHagaki_B': (309, 445),
}


class DocumentError(ValueError):
    """A document failure with a translation-friendly public error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def dependencies_available() -> bool:
    try:
        import PIL.Image  # noqa: F401
        import pypdf  # noqa: F401
    except ImportError:
        return False
    return True


def selected_pages(selection, page_count: int) -> list[int]:
    """Validate ordered one-based pages, including expressions such as 1,3–5."""
    if selection is None or selection == '':
        return list(range(1, page_count + 1))
    if isinstance(selection, str):
        if len(selection) > 200:
            raise DocumentError('invalid_pages', 'The page selection is too long.')
        pages = []
        try:
            for part in selection.split(','):
                values = part.strip().split('-')
                if not all(value.strip().isdigit() for value in values) or len(values) > 2:
                    raise ValueError
                start = int(values[0])
                end = int(values[-1])
                if not 1 <= start <= end <= page_count:
                    raise ValueError
                pages.extend(range(start, end + 1))
                if len(pages) > MAX_DOCUMENT_PAGES:
                    raise ValueError
        except (TypeError, ValueError) as exc:
            raise DocumentError('invalid_pages', 'Choose existing pages in the document.') from exc
    elif isinstance(selection, list):
        pages = selection.copy()
    else:
        raise DocumentError('invalid_pages', 'The page selection must contain page numbers.')
    if not pages or len(pages) > MAX_DOCUMENT_PAGES or any(
            type(page) is not int or not 1 <= page <= page_count for page in pages):
        raise DocumentError('invalid_pages', 'Choose between one and 25 existing pages.')
    return pages


def validated_rotation(rotation) -> int:
    if type(rotation) is not int or rotation not in (0, 90, 180, 270):
        raise DocumentError('invalid_rotation', 'Choose a rotation of 0, 90, 180 or 270 degrees.')
    return rotation


def validated_crop(crop):
    if crop is None:
        return None
    if isinstance(crop, dict) and set(crop) == {'left', 'top', 'right', 'bottom'}:
        try:
            crop = {'left': crop['left'], 'top': crop['top'],
                    'width': crop['right'] - crop['left'],
                    'height': crop['bottom'] - crop['top']}
        except (TypeError, ValueError) as exc:
            raise DocumentError('invalid_crop', 'The crop contains an invalid measurement.') from exc
    if not isinstance(crop, dict) or set(crop) != {'left', 'top', 'width', 'height'}:
        raise DocumentError('invalid_crop', 'The crop must describe its position and size.')
    values = []
    for name in ('left', 'top', 'width', 'height'):
        value = crop[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise DocumentError('invalid_crop', 'The crop contains an invalid measurement.')
        values.append(float(value))
    left, top, width, height = values
    if (left < 0 or top < 0 or width <= 0 or height <= 0 or
            left + width > 1.0000001 or top + height > 1.0000001):
        raise DocumentError('invalid_crop', 'The crop must remain inside the page.')
    return left, top, width, height


class DocumentProcessor:
    def __init__(self, command_runner=run_command):
        self.command_runner = command_runner

    def capabilities(self) -> dict:
        import shutil
        return {'available': dependencies_available(), 'preview': bool(shutil.which('pdftoppm')),
                'formats': ['application/pdf', 'image/png', 'image/jpeg'],
                'maximum_pages': MAX_DOCUMENT_PAGES, 'maximum_upload_bytes': MAX_DOCUMENT_BYTES,
                'paper_sizes': list(PAPER_DIMENSIONS)}

    def _check_file(self, path) -> Path:
        path = Path(path)
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise DocumentError('document_unavailable', 'The document is unavailable.') from exc
        if size <= 0 or size > MAX_DOCUMENT_BYTES:
            raise DocumentError('document_size', 'The document must be between one byte and 20 MiB.')
        if not dependencies_available():
            raise DocumentError('document_dependencies', 'Install the document-processing dependencies first.')
        return path

    @contextmanager
    def _image(self, path):
        from PIL import Image, ImageOps
        allocated_images = {}
        def retain(image):
            allocated_images[id(image)] = image
            return image
        def release(image):
            if allocated_images.pop(id(image), None) is not None:
                image.close()
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('error', Image.DecompressionBombWarning)
                source = retain(Image.open(path))
                if source.format not in ('PNG', 'JPEG') or getattr(source, 'n_frames', 1) != 1:
                    raise DocumentError('document_format', 'Upload a PDF, JPEG or single-frame PNG document.')
                pixels = source.width * source.height
                if pixels > MAX_IMAGE_PIXELS:
                    raise DocumentError('image_size', 'The image exceeds the 40 megapixel limit.')
                has_transparency = source.mode in ('RGBA', 'LA') or 'transparency' in source.info
                # Alpha compositing and four-channel conversion need additional
                # full-size buffers. Check their limit before decoding pixels.
                if (has_transparency or source.mode == 'CMYK') and pixels > 20_000_000:
                    raise DocumentError('image_size', 'Images with transparency or CMYK colour exceed the 20 megapixel limit.')
                source.load()
                try:
                    orientation = source.getexif().get(274, 1)
                except (ValueError, TypeError):
                    orientation = 1
                displayed = retain(ImageOps.exif_transpose(source)) if orientation in range(2, 9) else source
                if displayed is not source:
                    # Free the old pixels before PDF/JPEG encoding starts.
                    release(source)
                if has_transparency:
                    rgba = displayed if displayed.mode == 'RGBA' else retain(displayed.convert('RGBA'))
                    if rgba is not displayed:
                        release(displayed)
                    image = retain(Image.new('RGB', rgba.size, 'white'))
                    alpha = retain(rgba.getchannel('A'))
                    image.paste(rgba, mask=alpha)
                    release(alpha)
                    release(rgba)
                elif displayed.mode == 'RGB':
                    image = displayed  # Reuse the existing full-size buffer.
                else:
                    image = retain(displayed.convert('RGB'))
                    release(displayed)
                yield image
        except DocumentError:
            raise
        except Exception as exc:
            raise DocumentError('document_format', 'The image could not be read safely.') from exc
        finally:
            for image in allocated_images.values():
                image.close()

    def _reader(self, path):
        from pypdf import PdfReader
        try:
            reader = PdfReader(str(path), strict=False)
            if reader.is_encrypted:
                raise DocumentError('encrypted_document', 'Use a PDF without password protection.')
            if not 1 <= len(reader.pages) <= MAX_DOCUMENT_PAGES:
                raise DocumentError('document_pages', 'A document can contain between one and 25 pages.')
            for page in reader.pages:
                width, height = float(page.mediabox.width), float(page.mediabox.height)
                if not all(math.isfinite(value) and 0 < value <= 14400 for value in (width, height)):
                    raise DocumentError('page_size', 'A document page has an unsupported size.')
            return reader
        except DocumentError:
            raise
        except Exception as exc:
            raise DocumentError('document_format', 'The PDF could not be read safely.') from exc

    def inspect(self, path) -> dict:
        path = self._check_file(path)
        try:
            with path.open('rb') as source:
                signature = source.read(8)
            if signature.startswith(b'%PDF-'):
                reader = self._reader(path)
                first = reader.pages[0]
                return {'media_type': 'application/pdf', 'page_count': len(reader.pages),
                        'width': float(first.mediabox.width), 'height': float(first.mediabox.height),
                        'unit': 'pt'}
            with self._image(path) as image:
                media_type = 'image/png' if signature.startswith(b'\x89PNG') else 'image/jpeg'
                return {'media_type': media_type, 'page_count': 1,
                        'width': image.width, 'height': image.height, 'unit': 'px'}
        except DocumentError:
            raise
        except Exception as exc:
            raise DocumentError('document_format', 'The document could not be read safely.') from exc

    @staticmethod
    def _clean_page(page):
        # Document actions, links and attachments are not needed for printing.
        for key in ('/AA', '/Annots', '/PieceInfo'):
            if key in page:
                del page[key]
        if page.rotation:
            page.transfer_rotation_to_content()
        return page

    def prepare(self, input_path, output_path, *, pages=None, rotation=0,
                paper='A4', fit='fit', crop=None, cancel_event=None) -> dict:
        """Create a PDF with the selected, cropped and rotated pages in order."""
        from pypdf import PdfReader, PdfWriter, Transformation
        from pypdf.generic import RectangleObject
        input_path = self._check_file(input_path)
        output_path = Path(output_path)
        if input_path.resolve() == output_path.resolve():
            raise DocumentError('document_output', 'Save the prepared document as a separate file.')
        rotation = validated_rotation(rotation)
        crop = validated_crop(crop)
        if paper is not None and paper not in PAPER_DIMENSIONS:
            raise DocumentError('invalid_paper', 'Choose a supported paper size.')
        if fit not in ('fit', 'fill', 'original'):
            raise DocumentError('invalid_fit', 'Choose fitting, filling or original page size.')
        metadata = self.inspect(input_path)
        memory_pdf = None
        if metadata['media_type'] == 'application/pdf':
            reader = self._reader(input_path)
        else:
            memory_pdf = BytesIO()
            with self._image(input_path) as image:
                image.save(memory_pdf, 'PDF', resolution=300.0)
            memory_pdf.seek(0)
            reader = PdfReader(memory_pdf)
        writer = PdfWriter()
        try:
            for number in selected_pages(pages, len(reader.pages)):
                if cancel_event is not None and cancel_event.is_set():
                    raise DocumentError('job_cancelled', 'The operation was cancelled.')
                # Clone before modifying because a page may appear more than once.
                from copy import deepcopy
                page = self._clean_page(deepcopy(reader.pages[number - 1]))
                if rotation:
                    page.rotate(rotation)
                    page.transfer_rotation_to_content()
                width, height = float(page.mediabox.width), float(page.mediabox.height)
                origin_x, origin_y = float(page.mediabox.left), float(page.mediabox.bottom)
                if crop:
                    left, top, crop_width, crop_height = crop
                    origin_x += width * left
                    origin_y += height * (1 - top - crop_height)
                    width, height = width * crop_width, height * crop_height
                page.add_transformation(Transformation().translate(-origin_x, -origin_y))
                page.mediabox = RectangleObject((0, 0, width, height))
                page.cropbox = RectangleObject((0, 0, width, height))
                if paper is None or fit == 'original':
                    writer.add_page(page)
                    continue
                target_width, target_height = PAPER_DIMENSIONS[paper]
                scale = (max if fit == 'fill' else min)(target_width / width, target_height / height)
                target = writer.add_blank_page(target_width, target_height)
                transform = Transformation().scale(scale).translate(
                    (target_width - width * scale) / 2, (target_height - height * scale) / 2)
                target.merge_transformed_page(page, transform, expand=False)
            writer.add_metadata({'/Producer': 'Brother DCP-150C Native Control Center'})
            with output_path.open('wb') as output:
                writer.write(output)
        except DocumentError:
            raise
        except Exception as exc:
            output_path.unlink(missing_ok=True)
            raise DocumentError('document_processing', 'The document could not be prepared.') from exc
        finally:
            if memory_pdf is not None:
                memory_pdf.close()
        return self.inspect(output_path)

    def preview(self, input_path, output_path, *, page=1, cancel_event=None) -> dict:
        """Render one bounded PNG preview without embedding an active PDF viewer."""
        from PIL import Image
        metadata = self.inspect(input_path)
        selected_pages([page], metadata['page_count'])
        output_path = Path(output_path)
        if Path(input_path).resolve() == output_path.resolve():
            raise DocumentError('document_output', 'Save the preview as a separate file.')
        if metadata['media_type'] != 'application/pdf':
            with self._image(input_path) as image:
                image.thumbnail((1400, 1400), Image.Resampling.LANCZOS)
                image.save(output_path, 'PNG')
        else:
            with tempfile.TemporaryDirectory(prefix='preview-', dir=output_path.parent) as directory:
                prefix = Path(directory) / 'page'
                try:
                    self.command_runner(['pdftoppm', '-f', str(page), '-singlefile', '-scale-to',
                                         '1400', '-png', str(Path(input_path).resolve()), str(prefix)],
                                        timeout=60, cancel_event=cancel_event)
                    rendered = prefix.with_suffix('.png')
                    if not rendered.is_file() or rendered.stat().st_size > MAX_DOCUMENT_BYTES:
                        raise DocumentError('document_preview', 'The document preview is unavailable.')
                    rendered.replace(output_path)
                except CommandError as exc:
                    raise DocumentError(exc.code, str(exc)) from exc
        return {'media_type': 'image/png', 'page_count': 1}

    def merge(self, input_paths, output_path, *, cancel_event=None) -> dict:
        """Combine PDFs and images in the requested order, up to 25 pages."""
        from pypdf import PdfWriter
        paths = list(input_paths)
        output_path = Path(output_path)
        if not paths or len(paths) > MAX_DOCUMENT_PAGES or any(
                Path(path).resolve() == output_path.resolve() for path in paths):
            raise DocumentError('document_pages', 'Select between one and 25 documents for a new PDF.')
        writer = PdfWriter()
        total_pages = 0
        try:
            with tempfile.TemporaryDirectory(prefix='merge-', dir=output_path.parent) as directory:
                for index, path in enumerate(paths):
                    if cancel_event is not None and cancel_event.is_set():
                        raise DocumentError('job_cancelled', 'The operation was cancelled.')
                    metadata = self.inspect(path)
                    total_pages += metadata['page_count']
                    if total_pages > MAX_DOCUMENT_PAGES:
                        raise DocumentError('document_pages', 'The merged document exceeds 25 pages.')
                    normalized = Path(directory) / f'document-{index}.pdf'
                    self.prepare(path, normalized, paper=None, cancel_event=cancel_event)
                    reader = self._reader(normalized)
                    for page in reader.pages:
                        if cancel_event is not None and cancel_event.is_set():
                            raise DocumentError('job_cancelled', 'The operation was cancelled.')
                        writer.add_page(page)
                writer.add_metadata({'/Producer': 'Brother DCP-150C Native Control Center'})
                with output_path.open('wb') as output:
                    writer.write(output)
        except DocumentError:
            raise
        except Exception as exc:
            output_path.unlink(missing_ok=True)
            raise DocumentError('document_processing', 'The documents could not be combined.') from exc
        return self.inspect(output_path)

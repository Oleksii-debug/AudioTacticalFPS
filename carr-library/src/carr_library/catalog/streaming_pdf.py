"""Identity-bound, bounded streaming for supported PDF originals.

The existing bytes intake and non-PDF limits remain unchanged. A file snapshot
is an internal capability, not workspace authorization or text-review approval.
PDF parsing remains in the existing owned worker with its page/pixel/time limits.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
import ctypes
import os
from pathlib import Path, PureWindowsPath
import re
import shutil

from .intake import MAX_BYTES, IntakeError, snapshot

# PDFium FPDF_FILEACCESS uses an unsigned long, which is 32 bits on Windows.
MAX_PDF_BYTES = (1 << 32) - 1
CHUNK_BYTES = 1024 * 1024
MAX_READ_BYTES = 16 * CHUNK_BYTES
DISK_MARGIN = 32 * CHUNK_BYTES


def pdf_budget(value):
    if type(value) is not int or not 0 < value <= MAX_PDF_BYTES:
        raise IntakeError('Invalid supported PDF byte budget.')
    return value


def valid_original_size(mime, size, *, pdf_max_bytes=MAX_PDF_BYTES):
    limit = pdf_budget(pdf_max_bytes) if mime == 'application/pdf' else MAX_BYTES
    return type(size) is int and 0 < size <= limit


def _cancel(cancelled):
    if cancelled():
        raise InterruptedError('Original source operation cancelled.')


def _regular(path):
    from ..audio.jobs import regular_file
    info = regular_file(path)
    if info.st_nlink != 1:
        raise IntakeError('Original source must have exactly one link.')
    return info


def _digest(stream, size, cancelled):
    stream.seek(0)
    digest, count, head = sha256(), 0, b''
    while True:
        _cancel(cancelled)
        block = stream.read(min(CHUNK_BYTES, size - count + 1))
        if not block:
            break
        if not head:
            head = block[:262]
        count += len(block)
        if count > size:
            raise IntakeError('Original source size changed.')
        digest.update(block)
    if count != size or not head.removeprefix(b'\xef\xbb\xbf').startswith(b'%PDF-'):
        raise IntakeError('Invalid or changed PDF original.')
    return digest.hexdigest()


class _BoundedReader:
    """Seekable parser view: no unbounded read or single oversized allocation."""
    def __init__(self, stream, size, cancelled):
        self.stream, self.size, self.cancelled = stream, size, cancelled

    def tell(self):
        return self.stream.tell()

    def seek(self, offset, whence=0):
        _cancel(self.cancelled)
        if type(offset) is not int or whence not in (0, 1, 2):
            raise IntakeError('Invalid PDF parser seek.')
        position = offset + (self.tell() if whence == 1 else self.size if whence == 2 else 0)
        if not 0 <= position <= self.size:
            raise IntakeError('PDF parser sought outside the original.')
        return self.stream.seek(position)

    def read(self, size=-1):
        _cancel(self.cancelled)
        if type(size) is not int or not 0 <= size <= MAX_READ_BYTES:
            raise IntakeError('PDF parser request exceeds its bounded stream allowance.')
        return self.stream.read(min(size, self.size - self.tell()))

    def readinto(self, buffer):
        _cancel(self.cancelled)
        size = len(buffer)
        if size > MAX_READ_BYTES or self.tell() + size > self.size:
            raise IntakeError('PDF parser block exceeds the original or read allowance.')
        count = self.stream.readinto(buffer)
        if count != size:
            raise IntakeError('PDF parser received a short source block.')
        return count

    def readable(self):
        return True

    def seekable(self):
        return True


@dataclass(frozen=True)
class FileAsset:
    path: Path
    digest: str
    size: int
    identity: tuple
    mime: str = 'application/pdf'

    def __post_init__(self):
        if (self.mime != 'application/pdf' or not valid_original_size(self.mime, self.size)
                or not isinstance(self.digest, str) or not re.fullmatch('[0-9a-f]{64}', self.digest)
                or not isinstance(self.identity, tuple) or len(self.identity) != 5
                or any(type(v) is not int for v in self.identity)
                or self.identity[2] != self.size):
            raise IntakeError('Invalid file-backed PDF snapshot.')

    def manifest(self):
        return dict(sha256=self.digest, mime=self.mime, size=self.size)

    @classmethod
    def capture(cls, path, *, max_bytes=MAX_PDF_BYTES, cancelled=lambda: False,
                expected_manifest=None, expected_identity=None):
        from ..audio.jobs import checked_directory, file_identity, verified_reader
        pdf_budget(max_bytes)
        if expected_manifest is not None and (
                not isinstance(expected_manifest, dict) or set(expected_manifest) != {'sha256', 'mime', 'size'}
                or expected_manifest['mime'] != 'application/pdf'
                or not valid_original_size('application/pdf', expected_manifest['size'], pdf_max_bytes=max_bytes)
                or not isinstance(expected_manifest['sha256'], str)
                or not re.fullmatch('[0-9a-f]{64}', expected_manifest['sha256'])):
            raise IntakeError('Invalid expected PDF manifest.')
        if expected_identity is not None and (
                not isinstance(expected_identity, (tuple, list)) or len(expected_identity) != 5
                or any(type(value) is not int for value in expected_identity)):
            raise IntakeError('Invalid expected PDF file identity.')
        _cancel(cancelled)
        path = Path(path)
        # Validate aliases before canonicalizing; never hide a link or reparse.
        _regular(path)
        path = checked_directory(path.parent) / path.name
        info = _regular(path)
        if not valid_original_size('application/pdf', info.st_size, pdf_max_bytes=max_bytes):
            raise IntakeError('PDF original exceeds its selected byte budget.')
        identity = file_identity(info)
        if expected_identity is not None and tuple(expected_identity) != identity:
            raise IntakeError('PDF original identity changed.')
        with verified_reader(path, expected_identity=identity) as stream:
            digest = _digest(stream, info.st_size, cancelled)
            _regular(path)
        result = cls(path, digest, info.st_size, identity)
        if expected_manifest is not None and result.manifest() != expected_manifest:
            raise IntakeError('PDF original differs from its recorded manifest.')
        return result

    @contextmanager
    def open(self, *, cancelled=lambda: False):
        from ..audio.jobs import file_identity, verified_reader
        if file_identity(_regular(self.path)) != self.identity:
            raise IntakeError('PDF original identity changed.')
        with verified_reader(self.path, expected_identity=self.identity) as stream:
            if _digest(stream, self.size, cancelled) != self.digest:
                raise IntakeError('PDF original checksum changed.')
            stream.seek(0)
            yield _BoundedReader(stream, self.size, cancelled)
            # Same-size mutation is rejected even if a writer restores mtime.
            if _digest(stream, self.size, cancelled) != self.digest:
                raise IntakeError('PDF original changed during use.')
            _regular(self.path)

    def copy_to(self, destination, *, cancelled=lambda: False):
        from ..audio.jobs import checked_directory, file_identity, verified_reader
        destination = Path(destination)
        checked_directory(destination.parent)
        _cancel(cancelled)
        if shutil.disk_usage(destination.parent).free < self.size + DISK_MARGIN:
            raise IntakeError('Insufficient free space for the exact PDF working copy.')
        if file_identity(_regular(self.path)) != self.identity:
            raise IntakeError('PDF original identity changed.')
        created = False
        try:
            with verified_reader(self.path, expected_identity=self.identity) as source:
                with destination.open('xb') as target:
                    created = True
                    digest, count = sha256(), 0
                    while True:
                        _cancel(cancelled)
                        block = source.read(min(CHUNK_BYTES, self.size - count + 1))
                        if not block:
                            break
                        count += len(block)
                        if count > self.size:
                            raise IntakeError('PDF original grew during copying.')
                        target.write(block)
                        digest.update(block)
                    if count != self.size or digest.hexdigest() != self.digest:
                        raise IntakeError('PDF original changed during copying.')
                    _regular(self.path)
                    target.flush()
                    os.fsync(target.fileno())
            return destination
        except BaseException:
            if created:
                try:
                    destination.unlink(missing_ok=True)
                except OSError:
                    pass
            raise


def snapshot_original(root, relative, *, pdf_max_bytes=MAX_PDF_BYTES,
                      cancelled=lambda: False):
    """Select exactly one local original; only larger PDF uses file-backed I/O."""
    pdf_budget(pdf_max_bytes)
    raw = os.fspath(relative)
    if (not isinstance(raw, str) or '\\' in raw or ':' in raw or '\x00' in raw
            or any(p in ('', '.', '..') or p.rstrip(' .') != p
                   or PureWindowsPath(p).is_reserved() for p in raw.split('/'))):
        raise IntakeError('Unsafe original relative path.')
    from ..audio.jobs import checked_directory
    root = checked_directory(root)
    path = root.joinpath(*raw.split('/'))
    info = _regular(path)
    if path.suffix.casefold() == '.pdf':
        if not valid_original_size('application/pdf', info.st_size, pdf_max_bytes=pdf_max_bytes):
            raise IntakeError('PDF original exceeds its selected byte budget.')
        if info.st_size > MAX_BYTES:
            return FileAsset.capture(path, max_bytes=pdf_max_bytes, cancelled=cancelled)
    return snapshot(root, raw)


@contextmanager
def original_stream(asset, *, cancelled=lambda: False):
    if isinstance(asset, FileAsset):
        with asset.open(cancelled=cancelled) as stream:
            yield stream
    else:
        data = asset if type(asset) is bytes else asset.data
        if type(data) is not bytes or not 0 < len(data) <= MAX_BYTES:
            raise IntakeError('Invalid bounded original bytes.')
        with BytesIO(data) as stream:
            yield stream


@contextmanager
def pdf_document(asset, *, cancelled=lambda: False):
    """Reuse PDFium with a fail-closed, owned seekable source on large originals."""
    import pypdfium2 as pdfium
    if not isinstance(asset, FileAsset):
        data = asset if type(asset) is bytes else asset.data
        if type(data) is not bytes or not 0 < len(data) <= MAX_BYTES:
            raise IntakeError('Invalid bounded PDF bytes.')
        with pdfium.PdfDocument(data) as document:
            yield document
        return
    with asset.open(cancelled=cancelled) as stream:
        access = pdfium.raw.FPDF_FILEACCESS()
        access.m_FileLen = asset.size
        failures = []

        def read_block(_parameter, position, destination, size):
            try:
                _cancel(cancelled)
                if position > asset.size or size > asset.size - position:
                    raise IntakeError('PDFium requested bytes outside the original.')
                stream.seek(position)
                # A native block may be large; fill its existing buffer in chunks.
                done = 0
                address = ctypes.cast(destination, ctypes.c_void_p).value
                if size and not address:
                    raise IntakeError('PDFium supplied an invalid read buffer.')
                while done < size:
                    count = min(CHUNK_BYTES, size - done)
                    view = (ctypes.c_ubyte * count).from_address(address + done)
                    stream.readinto(view)
                    done += count
                return 1
            except BaseException as exc:
                if not failures:
                    failures.append(exc)
                return 0

        callback = type(access.m_GetBlock)(read_block)
        access.m_GetBlock = callback
        access.m_Param = None
        raw = pdfium.raw.FPDF_LoadCustomDocument(ctypes.byref(access), None)
        if not raw:
            if failures:
                raise failures[0]
            raise IntakeError('PDFium could not open the streamed original.')
        # Keep access, callback and stream alive until the native document closes.
        with pdfium.PdfDocument(raw) as document:
            yield document
        if failures:
            raise failures[0]

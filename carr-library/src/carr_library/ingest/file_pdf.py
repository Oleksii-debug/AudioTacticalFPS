"""Held file-backed PDF input for the existing parser/OCR implementation.

Only the current isolated import/review worker calls open_pdf. Backup uses
inspect_pdf, which streams hashes and a format prefix without executing a parser.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
import os
from pathlib import Path

from ..audio.jobs import file_identity, regular_file, verified_reader
from .source_profiles import PdfProfile, profile_from_value
from .streamed_text import check_cancelled

MAX_READ_BYTES = 16 * 1024**2


@dataclass(frozen=True)
class PdfSnapshot:
    digest: str
    size: int
    profile: PdfProfile
    identity: tuple
    mime: str = 'application/pdf'

    def manifest(self):
        return dict(sha256=self.digest, mime=self.mime, size=self.size)


def _regular(path, profile):
    info = regular_file(path)
    if (Path(path).suffix.casefold() != '.pdf' or info.st_nlink != 1
            or not 0 < info.st_size <= profile.maximum_bytes):
        raise ValueError('Choose a nonempty unaliased PDF within its selected original-byte allowance.')
    return info


def _hash(stream, size, cancelled):
    hashed, copied = sha256(), 0
    stream.seek(0)
    while copied < size:
        check_cancelled(cancelled)
        block = stream.read(min(1024**2, size - copied))
        if not block:
            raise ValueError('The retained PDF ended before its original size.')
        if copied == 0 and not block.startswith(b'%PDF-'):
            raise ValueError('The selected PDF has no PDF format header.')
        copied += len(block)
        hashed.update(block)
    if stream.read(1):
        raise ValueError('The retained PDF grew beyond its original size.')
    check_cancelled(cancelled)
    return hashed.hexdigest()


def inspect_pdf(path, profile, *, expected_digest=None, expected_size=None,
                expected_identity=None, cancelled=lambda: False):
    profile = profile_from_value(profile)
    if not isinstance(profile, PdfProfile):
        raise ValueError('Use the explicit PDF source profile.')
    info = _regular(path, profile)
    identity = file_identity(info)
    if ((expected_identity is not None and tuple(expected_identity) != identity)
            or (expected_size is not None and expected_size != info.st_size)):
        raise ValueError('The selected PDF source identity changed.')
    with verified_reader(path, expected_identity=identity) as stream:
        digest = _hash(stream, info.st_size, cancelled)
        if os.fstat(stream.fileno()).st_nlink != 1:
            raise ValueError('The PDF source became an alias.')
    if expected_digest is not None and digest != expected_digest:
        raise ValueError('The complete PDF original checksum changed.')
    return PdfSnapshot(digest, info.st_size, profile, identity)


class _BoundedReader:
    """Prevent one parser read from allocating an entire multi-GB original."""
    def __init__(self, stream, cancelled):
        self._stream, self._cancelled = stream, cancelled

    def _count(self, size):
        check_cancelled(self._cancelled)
        if type(size) is not int or not 0 <= size <= MAX_READ_BYTES:
            raise ValueError('The PDF parser requested an oversized individual read.')
        return size

    def read(self, size=-1):
        return self._stream.read(self._count(size))

    def readinto(self, buffer):
        self._count(len(buffer))
        return self._stream.readinto(buffer)

    def readline(self, size=-1):
        check_cancelled(self._cancelled)
        if size != -1:
            return self._stream.readline(self._count(size))
        value = self._stream.readline(MAX_READ_BYTES + 1)
        if len(value) > MAX_READ_BYTES:
            raise ValueError('The PDF parser requested an oversized line.')
        return value

    def seek(self, offset, whence=0):
        check_cancelled(self._cancelled)
        return self._stream.seek(offset, whence)

    def tell(self):
        return self._stream.tell()

    def __getattr__(self, name):
        return getattr(self._stream, name)


@contextmanager
def open_pdf(path, profile, *, expected_digest=None, expected_size=None,
             expected_identity=None, cancelled=lambda: False):
    """Separate seekable native readers stay held until both parsers are closed."""
    profile = profile_from_value(profile)
    if not isinstance(profile, PdfProfile):
        raise ValueError('Use the explicit PDF source profile.')
    info = _regular(path, profile)
    identity = file_identity(info)
    if ((expected_identity is not None and tuple(expected_identity) != identity)
            or (expected_size is not None and expected_size != info.st_size)):
        raise ValueError('The PDF source changed before parser admission.')
    from pypdf import PdfReader
    import pypdfium2 as pdfium
    with verified_reader(path, expected_identity=identity) as first:
        digest = _hash(first, info.st_size, cancelled)
        if expected_digest is not None and digest != expected_digest:
            raise ValueError('The complete PDF original checksum changed.')
        first.seek(0)
        reader = PdfReader(_BoundedReader(first, cancelled))
        try:
            if reader.is_encrypted or not 1 <= len(reader.pages) <= profile.maximum_records:
                raise ValueError('Encrypted PDF or selected page allowance exceeded.')
            with verified_reader(path, expected_identity=identity) as second:
                with pdfium.PdfDocument(_BoundedReader(second, cancelled)) as document:
                    if len(document) != len(reader.pages):
                        raise ValueError('The current PDF parsers disagree about page count.')
                    yield reader, document, PdfSnapshot(digest, info.st_size, profile, identity)
                    check_cancelled(cancelled)
        finally:
            reader.close()

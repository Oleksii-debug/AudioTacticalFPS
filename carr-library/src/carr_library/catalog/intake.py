"""Read-only, bounded catalog intake candidate; never extracts archives or executes data."""
from contextlib import contextmanager
from dataclasses import dataclass
import codecs
from io import BytesIO
from hashlib import sha256
from pathlib import Path, PureWindowsPath
import os
import re
import stat

from ._filetype.archive import Exe, Pdf, Rtf, Zip
from .office_archive import validate_office

MAX_BYTES = 64 * 1024 * 1024
MAX_STREAM_BYTES = 128 * 1024 * 1024
MAX_STREAM_SEGMENT_BYTES = 4 * 1024 * 1024
STREAM_MIMES = frozenset({'text/plain', 'application/pdf'})
_BLOCK = 1024 * 1024
_TEXT_CONTROLS = re.compile(rb'[\x00-\x08\x0b\x0e-\x1f]')


class IntakeError(ValueError):
    pass


@dataclass(frozen=True)
class AssetSnapshot:
    digest: str
    mime: str
    size: int
    data: bytes

    def manifest(self):
        """No original path, filename, content or inferred author."""
        return {"sha256": self.digest, "mime": self.mime, "size": self.size}


def classify(data, suffix):
    """Header screening only, not structural parsing or malware scanning."""
    if not data:
        raise IntakeError("empty asset")
    head = data[:262]
    if Exe().match(head) or head.startswith(b"\x7fELF"):
        raise IntakeError("executable not accepted by this adapter")
    from ..extract.document_formats import (FORMAT_BY_SUFFIX, classify_extended,
        RASTER_BY_SUFFIX, raster_format)
    raster = raster_format(data)
    if raster is not None or suffix.lower() in RASTER_BY_SUFFIX:
        if raster is None or RASTER_BY_SUFFIX.get(suffix.lower()) != raster:
            raise IntakeError('unsupported or mismatched raster image extension')
        return raster.mime
    if suffix.lower() in FORMAT_BY_SUFFIX:
        try:
            return classify_extended(data, suffix.lower())
        except ValueError as exc:
            raise IntakeError(str(exc)) from exc
    if Zip().match(head):
        try:
            return validate_office(data, suffix.lower())
        except ValueError as exc:
            raise IntakeError(str(exc)) from exc
    if Pdf().match(head):
        mime = "application/pdf"
        if not head.removeprefix(b"\xef\xbb\xbf").startswith(b"%PDF-"):
            raise IntakeError("invalid PDF header")
    elif Rtf().match(head):
        mime = "application/rtf"
    else:
        try:
            data.decode("utf-8-sig")
        except UnicodeError as exc:
            raise IntakeError("unsupported binary or text encoding") from exc
        # Valid UTF-8 cannot hide an ASCII control inside a multibyte character.
        if _TEXT_CONTROLS.search(data):
            raise IntakeError("binary control character in text")
        mime = "text/plain"
    expected = {".pdf": "application/pdf", ".rtf": "application/rtf", ".txt": "text/plain"}
    if expected.get(suffix.lower()) != mime:
        raise IntakeError("unsupported or mismatched file extension")
    return mime


def snapshot(root, relative, *, max_bytes=MAX_BYTES, cancelled=lambda: False):
    """Read a selected relative file under a trusted local root; return immutable bytes.

    Reject symlinks/reparse points and changed files. Concurrent hostile directory
    replacement is outside this candidate's threat model; use a trusted staging root.
    Callers must parse the returned bytes, not reopen the original path.
    """
    if cancelled():
        raise InterruptedError('Source intake cancelled.')
    if not 0 < max_bytes <= MAX_BYTES:
        raise IntakeError("invalid size limit")
    raw = os.fspath(relative)
    if not isinstance(raw, str) or "\\" in raw or ":" in raw or "\x00" in raw:
        raise IntakeError("unsafe relative path")
    parts = raw.split("/")
    if any(p in ("", ".", "..") or p.rstrip(" .") != p or PureWindowsPath(p).is_reserved() for p in parts):
        raise IntakeError("unsafe relative path")
    root = Path(root).resolve(strict=True)
    path = root
    for part in parts:
        path = path / part
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise IntakeError("link or reparse point")
    if not path.resolve(strict=True).is_relative_to(root) or not stat.S_ISREG(info.st_mode):
        raise IntakeError("not a regular file within root")
    if info.st_size > max_bytes:
        raise IntakeError("asset too large")
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or (info.st_dev, info.st_ino) != (before.st_dev, before.st_ino):
            raise IntakeError("file replaced")
        data = stream.read(max_bytes + 1)
        after = os.fstat(stream.fileno())
    if cancelled():
        raise InterruptedError('Source intake cancelled.')
    if len(data) > max_bytes or len(data) != before.st_size or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        raise IntakeError("asset too large or changed during read")
    mime = classify(data, path.suffix)
    return AssetSnapshot(sha256(data).hexdigest(), mime, len(data), data)


def source_limit(mime):
    """Only streaming TXT/PDF opt into the bounded larger-source profile."""
    return MAX_STREAM_BYTES if mime in STREAM_MIMES else MAX_BYTES


def _stamp(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _stream_change_time(stream):
    """Track in-place writes on the already held Windows file descriptor."""
    if os.name != 'nt':
        return None
    import ctypes
    import msvcrt

    class FileBasicInfo(ctypes.Structure):
        _fields_ = [('CreationTime', ctypes.c_int64), ('LastAccessTime', ctypes.c_int64),
                    ('LastWriteTime', ctypes.c_int64), ('ChangeTime', ctypes.c_int64),
                    ('FileAttributes', ctypes.c_uint32)]

    query = ctypes.WinDLL('kernel32', use_last_error=True).GetFileInformationByHandleEx
    query.argtypes = [ctypes.c_void_p, ctypes.c_int32, ctypes.c_void_p, ctypes.c_uint32]
    query.restype = ctypes.c_int32
    info = FileBasicInfo()
    handle = msvcrt.get_osfhandle(stream.fileno())
    if not query(handle, 0, ctypes.byref(info), ctypes.sizeof(info)):
        raise IntakeError('Source change time could not be verified '
                          f'(Windows error {ctypes.get_last_error()}).')
    if info.ChangeTime <= 0 or info.FileAttributes & (0x400 | 0x10):
        raise IntakeError('Source change time requires a regular local file.')
    return info.ChangeTime



def _open_file_source(path):
    """Hold a Windows read handle that excludes concurrent writes and renames.

    File timestamps can stay equal across quick writes. Sharing protection lasts
    for this exact held descriptor; later opens still recheck the saved identity.
    Other readers remain allowed. The portable reader retains its current checks.
    """
    if os.name != 'nt':
        return path.open('rb')
    import ctypes
    from ctypes import wintypes
    import msvcrt

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                       ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes, close.restype = [wintypes.HANDLE], wintypes.BOOL
    # GENERIC_READ; FILE_SHARE_READ only; OPEN_EXISTING;
    # OPEN_REPARSE_POINT so the following held-descriptor checks reject a swap.
    handle = create(str(path), 0x80000000, 0x00000001, None, 3, 0x00200000, None)
    if handle == ctypes.c_void_p(-1).value:
        code = ctypes.get_last_error()
        raise IntakeError('The selected source changed or is unavailable for a stable read '
                          f'(Windows error {code}). Close any application writing it and retry.')
    descriptor = None
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        handle = None  # CRT descriptor now owns the exact native handle.
        result = os.fdopen(descriptor, 'rb')
        descriptor = None  # The Python stream now owns the descriptor.
        return result
    finally:
        if descriptor is not None:
            os.close(descriptor)
        elif handle is not None:
            close(handle)


def _selected(root, relative):
    raw = os.fspath(relative)
    if not isinstance(raw, str) or "\\" in raw or ":" in raw or "\x00" in raw:
        raise IntakeError("unsafe relative path")
    parts = raw.split("/")
    if any(p in ("", ".", "..") or p.rstrip(" .") != p or PureWindowsPath(p).is_reserved() for p in parts):
        raise IntakeError("unsafe relative path")
    root = Path(root).resolve(strict=True)
    path = root
    for part in parts:
        path = path / part
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise IntakeError("link or reparse point")
    if (not path.resolve(strict=True).is_relative_to(root) or not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1):
        raise IntakeError("not an unlinked regular file within root")
    return path, info


@dataclass(frozen=True)
class FileSnapshot:
    """Verified manifest plus exact local file identity; never arbitrary saved path authority.

    Consumers keep this read handle open while parsing, copy/hash before
    publication, and verify complete bytes again before granting source review.
    Only the trusted source resolver creates a live snapshot from passive state.
    """
    digest: str
    mime: str
    size: int
    path: Path
    identity: tuple

    def manifest(self):
        return {"sha256": self.digest, "mime": self.mime, "size": self.size}

    @contextmanager
    def open(self, *, cancelled=lambda: False):
        if cancelled():
            raise InterruptedError('Source verification cancelled.')
        path, before = _selected(self.path.parent, self.path.name)
        if (_stamp(before) != self.identity or before.st_size != self.size
                or self.mime not in STREAM_MIMES or not 0 < self.size <= source_limit(self.mime)):
            raise IntakeError('File-backed source changed before reading.')
        with _open_file_source(path) as stream:
            if _stamp(os.fstat(stream.fileno())) != self.identity:
                raise IntakeError('File-backed source changed while opening.')
            change_time = _stream_change_time(stream)
            try:
                yield stream
            except OSError as error:
                if os.name != 'nt' or isinstance(error, (InterruptedError, TimeoutError)):
                    raise
                raise IntakeError('File-backed source changed or became unavailable while reading.') from error
            finally:
                _, named = _selected(path.parent, path.name)
                if (_stamp(os.fstat(stream.fileno())) != self.identity
                        or _stamp(named) != self.identity
                        or _stream_change_time(stream) != change_time):
                    raise IntakeError('File-backed source changed while reading.')
        if cancelled():
            raise InterruptedError('Source verification cancelled.')

    def verify(self, *, cancelled=lambda: False):
        with self.open(cancelled=cancelled) as stream:
            digest, size = _hash_stream(stream, cancelled=cancelled, maximum=self.size)
        if size != self.size or digest != self.digest:
            raise IntakeError('File-backed source digest changed.')
        return self

    def copy_to(self, destination, *, cancelled=lambda: False):
        """Create one new private copy, preserving exact verified bytes."""
        with self.open(cancelled=cancelled) as reader, Path(destination).open('xb') as writer:
            digest, size = _hash_stream(reader, cancelled=cancelled, maximum=self.size, writer=writer)
            writer.flush()
            os.fsync(writer.fileno())
        if size != self.size or digest != self.digest:
            raise IntakeError('File-backed source changed before copy publication.')
        path, info = _selected(Path(destination).parent, Path(destination).name)
        copied = FileSnapshot(self.digest, self.mime, size, path, _stamp(info))
        return copied.verify(cancelled=cancelled)


def _hash_stream(stream, *, cancelled, maximum, writer=None, inspect=None):
    hashed, size = sha256(), 0
    while block := stream.read(_BLOCK):
        if cancelled():
            raise InterruptedError('Source verification cancelled.')
        size += len(block)
        if size > maximum:
            raise IntakeError('Source exceeds its file-backed byte budget.')
        hashed.update(block)
        if writer is not None:
            writer.write(block)
        if inspect is not None:
            inspect(block)
    if cancelled():
        raise InterruptedError('Source verification cancelled.')
    return hashed.hexdigest(), size


def file_snapshot(root, relative, *, cancelled=lambda: False):
    """Screen and hash an explicitly selected TXT/PDF using bounded blocks."""
    path, before = _selected(root, relative)
    mime = {'.txt': 'text/plain', '.pdf': 'application/pdf'}.get(path.suffix.lower())
    if mime is None or not 0 < before.st_size <= source_limit(mime):
        raise IntakeError('Only nonempty TXT/PDF support file-backed intake up to 128 MiB.')
    decoder = codecs.getincrementaldecoder('utf-8-sig')('strict') if mime == 'text/plain' else None
    first = True
    def inspect(block):
        nonlocal first
        if first:
            head = block[:262]
            if mime == 'application/pdf':
                if not Pdf().match(head) or not head.removeprefix(b'\xef\xbb\xbf').startswith(b'%PDF-'):
                    raise IntakeError('invalid PDF header')
            elif (Exe().match(head) or head.startswith(b'\x7fELF')
                  or Zip().match(head) or Pdf().match(head) or Rtf().match(head)):
                raise IntakeError('unsupported or mismatched file extension')
            first = False
        if decoder is not None:
            decoder.decode(block)
            if _TEXT_CONTROLS.search(block):
                raise IntakeError('binary control character in text')
    candidate = FileSnapshot('', mime, before.st_size, path, _stamp(before))
    try:
        with candidate.open(cancelled=cancelled) as stream:
            digest, size = _hash_stream(stream, cancelled=cancelled, maximum=before.st_size, inspect=inspect)
        if decoder is not None:
            decoder.decode(b'', final=True)
    except UnicodeError as exc:
        raise IntakeError('unsupported binary or text encoding') from exc
    if size != before.st_size:
        raise IntakeError('Source changed during file-backed intake.')
    return FileSnapshot(digest, mime, size, path, _stamp(before))


def source_snapshot(root, relative, *, cancelled=lambda: False):
    """Preserve the legacy immutable-byte profile; stream only larger TXT/PDF."""
    path, before = _selected(root, relative)
    if before.st_size > MAX_BYTES and path.suffix.lower() in ('.txt', '.pdf'):
        return file_snapshot(root, relative, cancelled=cancelled)
    return snapshot(root, relative, cancelled=cancelled)


@contextmanager
def source_stream(asset, *, cancelled=lambda: False):
    if isinstance(asset, FileSnapshot):
        with asset.open(cancelled=cancelled) as stream:
            yield stream
    elif isinstance(asset, AssetSnapshot):
        if cancelled():
            raise InterruptedError('Source verification cancelled.')
        with BytesIO(asset.data) as stream:
            yield stream
    else:
        raise IntakeError('A reviewed source snapshot is required.')


def copy_snapshot(asset, destination, *, cancelled=lambda: False):
    if isinstance(asset, FileSnapshot):
        return asset.copy_to(destination, cancelled=cancelled)
    if not isinstance(asset, AssetSnapshot) or sha256(asset.data).hexdigest() != asset.digest:
        raise IntakeError('Snapshot digest mismatch.')
    if cancelled():
        raise InterruptedError('Source copy cancelled.')
    with Path(destination).open('xb') as stream:
        stream.write(asset.data)
        stream.flush()
        os.fsync(stream.fileno())
    if cancelled():
        raise InterruptedError('Source copy cancelled.')
    return asset


def _text_records(asset, *, cancelled=lambda: False):
    """Consume every yielded record before trusting any result: final hash binds all bytes."""
    if asset.mime != 'text/plain':
        raise IntakeError('A strict TXT source is required.')
    maximum = MAX_STREAM_SEGMENT_BYTES if asset.size > MAX_BYTES else MAX_BYTES
    count, start, length, pieces, hashed, total = 0, 0, 0, [], sha256(), 0
    def decoded(raw, index):
        text = raw.decode('utf-8-sig' if index == 0 else 'utf-8', errors='strict')
        if _TEXT_CONTROLS.search(raw):
            raise IntakeError('binary control character in text')
        return text
    with source_stream(asset, cancelled=cancelled) as stream:
        while block := stream.read(_BLOCK):
            if cancelled():
                raise InterruptedError('TXT source verification cancelled.')
            total += len(block)
            if total > asset.size:
                raise IntakeError('TXT source grew during verification.')
            hashed.update(block)
            parts = block.split(b'\f')
            for position, part in enumerate(parts):
                length += len(part)
                if length > maximum:
                    raise IntakeError('A large TXT logical segment exceeds 4 MiB; split only at original form-feed boundaries.')
                pieces.append(part)
                if position < len(parts) - 1:
                    if count >= 1000:
                        raise IntakeError('TXT source exceeds 1000 original logical segments.')
                    yield start, length, decoded(b''.join(pieces), count)
                    count += 1
                    start += length + 1
                    pieces, length = [], 0
        if count >= 1000:
            raise IntakeError('TXT source exceeds 1000 original logical segments.')
        yield start, length, decoded(b''.join(pieces), count)
    if total != asset.size or hashed.hexdigest() != asset.digest:
        raise IntakeError('TXT source digest changed during verification.')
    if cancelled():
        raise InterruptedError('TXT source verification cancelled.')


def text_segments(asset, *, cancelled=lambda: False):
    for _, _, text in _text_records(asset, cancelled=cancelled):
        yield text


class TextSegments:
    """Bounded offset index for a private staged TXT; final publication rehashes it."""
    def __init__(self, asset, *, cancelled=lambda: False):
        ranges, nonempty = [], False
        for offset, length, text in _text_records(asset, cancelled=cancelled):
            ranges.append((offset, length))
            nonempty = nonempty or bool(text.strip())
        if not nonempty:
            raise IntakeError('empty text')
        self.asset, self.ranges, self.cancelled = asset, tuple(ranges), cancelled

    def __len__(self):
        return len(self.ranges)

    def rebind(self, asset):
        if asset.manifest() != self.asset.manifest():
            raise IntakeError('Staged TXT differs from its source manifest.')
        self.asset = asset

    def __getitem__(self, index):
        offset, size = self.ranges[index]
        with source_stream(self.asset, cancelled=self.cancelled) as stream:
            stream.seek(offset)
            raw = stream.read(size)
            if len(raw) != size:
                raise IntakeError('Staged TXT segment was truncated.')
        return raw.decode('utf-8-sig' if index == 0 else 'utf-8', errors='strict')

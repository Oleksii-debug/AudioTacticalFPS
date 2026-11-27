"""Read-only, bounded catalog intake candidate; never extracts archives or executes data."""
from array import array
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
# Integer/protocol safety only. Every operation supplies its reviewed byte budget.
MAX_STREAM_BYTES = 2 ** 63 - 1
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
    from ..extract.document_formats import FORMAT_BY_SUFFIX, classify_extended
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
            decoded = data.decode("utf-8-sig")
        except UnicodeError as exc:
            raise IntakeError("unsupported binary or text encoding") from exc
        if any(ord(c) < 32 and c not in "\t\r\n\f" for c in decoded):
            raise IntakeError("binary control character in text")
        mime = "text/plain"
    expected = {".pdf": "application/pdf", ".rtf": "application/rtf", ".txt": "text/plain"}
    if expected.get(suffix.lower()) != mime:
        raise IntakeError("unsupported or mismatched file extension")
    return mime


def source_limit(mime, maximum=MAX_BYTES):
    if type(maximum) is not int or not 0 < maximum <= MAX_STREAM_BYTES:
        raise IntakeError('Invalid source byte allowance.')
    return maximum if mime in STREAM_MIMES else min(maximum, MAX_BYTES)


def _stamp(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns,
            getattr(info, 'st_file_attributes', 0))


def _parent_stamp(path):
    from ..durable_state import regular
    value = regular(path, directory=True)
    return (value.st_dev, value.st_ino, value.st_mode, value.st_nlink,
            getattr(value, 'st_file_attributes', 0))


def _selected(root, relative):
    from ..durable_state import local_path, regular
    raw = os.fspath(relative)
    if not isinstance(raw, str) or "\\" in raw or ":" in raw or "\x00" in raw:
        raise IntakeError("unsafe relative path")
    parts = raw.split("/")
    if any(p in ("", ".", "..") or p.rstrip(" .") != p
           or any(ord(c) < 32 for c in p) or PureWindowsPath(p).is_reserved() for p in parts):
        raise IntakeError("unsafe relative path")
    root = local_path(root, exists=True)
    regular(root, directory=True)
    path = local_path(root.joinpath(*parts), root=root, exists=True)
    info = regular(path)
    return path, info


@contextmanager
def _opened(path, identity, *, cancelled=lambda: False):
    """Open the exact checked descriptor before reading even one source byte."""
    from ..durable_state import _open_json_file
    if cancelled():
        raise InterruptedError('Source verification cancelled.')
    path, before = _selected(Path(path).parent, Path(path).name)
    parent = _parent_stamp(path.parent)
    if _stamp(before) != identity:
        raise IntakeError('Source changed before reading.')
    with _open_json_file(path) as stream:
        opened = os.fstat(stream.fileno())
        if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                or getattr(opened, 'st_file_attributes', 0) & 0x400
                or _stamp(opened) != identity):
            raise IntakeError('Source changed while opening.')
        _, named = _selected(path.parent, path.name)
        if _stamp(named) != identity or _parent_stamp(path.parent) != parent:
            raise IntakeError('Source ancestry changed while opening.')
        try:
            yield stream
        finally:
            _, named = _selected(path.parent, path.name)
            if (_stamp(os.fstat(stream.fileno())) != identity
                    or _stamp(named) != identity or _parent_stamp(path.parent) != parent):
                raise IntakeError('Source changed while reading.')
    if cancelled():
        raise InterruptedError('Source verification cancelled.')


def _hash_stream(stream, *, cancelled, maximum, writer=None, inspect=None):
    if type(maximum) is not int or not 0 <= maximum <= MAX_STREAM_BYTES:
        raise IntakeError('Invalid captured source size.')
    hashed, size = sha256(), 0
    while True:
        if cancelled():
            raise InterruptedError('Source verification cancelled.')
        # One extra byte detects growth; an appending file cannot extend this loop.
        block = stream.read(min(_BLOCK, maximum - size + 1))
        if cancelled():
            raise InterruptedError('Source verification cancelled.')
        if not block:
            break
        size += len(block)
        if size > maximum:
            raise IntakeError('Source exceeds its captured byte allowance.')
        hashed.update(block)
        if writer is not None and writer.write(block) != len(block):
            raise IntakeError('Source copy was incomplete.')
        if inspect is not None:
            inspect(block)
    return hashed.hexdigest(), size


def snapshot(root, relative, *, max_bytes=MAX_BYTES, cancelled=lambda: False):
    """Capture immutable bytes for existing bounded decoders."""
    if type(max_bytes) is not int or not 0 < max_bytes <= MAX_BYTES:
        raise IntakeError("invalid size limit")
    path, info = _selected(root, relative)
    if not 0 < info.st_size <= max_bytes:
        raise IntakeError("asset too large or empty")
    data = BytesIO()
    with _opened(path, _stamp(info), cancelled=cancelled) as stream:
        digest, size = _hash_stream(stream, cancelled=cancelled,
                                   maximum=info.st_size, writer=data)
    if size != info.st_size:
        raise IntakeError("asset changed during read")
    raw = data.getvalue()
    return AssetSnapshot(digest, classify(raw, path.suffix), size, raw)


@dataclass(frozen=True)
class FileSnapshot:
    """Verified complete bytes with one exact local file identity.

    A saved filename is not authority. A current scoped resolver or reviewed
    intake creates this live object, and consumers recheck bytes before commit.
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
        if (self.mime not in STREAM_MIMES or type(self.size) is not int
                or not 0 < self.size <= MAX_STREAM_BYTES):
            raise IntakeError('Invalid file-backed source manifest.')
        with _opened(self.path, self.identity, cancelled=cancelled) as stream:
            if os.fstat(stream.fileno()).st_size != self.size:
                raise IntakeError('File-backed source size changed.')
            yield stream

    def verify(self, *, cancelled=lambda: False):
        with self.open(cancelled=cancelled) as stream:
            digest, size = _hash_stream(stream, cancelled=cancelled, maximum=self.size)
        if size != self.size or digest != self.digest:
            raise IntakeError('File-backed source digest changed.')
        return self

    def copy_to(self, destination, *, cancelled=lambda: False):
        """Create one private copy; no replacement or new source authority."""
        from ..durable_state import local_path, regular
        destination = local_path(destination)
        regular(destination.parent, directory=True)
        with self.open(cancelled=cancelled) as reader, destination.open('xb') as writer:
            digest, size = _hash_stream(reader, cancelled=cancelled,
                                       maximum=self.size, writer=writer)
            writer.flush()
            os.fsync(writer.fileno())
        if size != self.size or digest != self.digest:
            raise IntakeError('Source changed while creating its private copy.')
        # Native Windows metadata can lag a same-size write. Rehash the admitted
        # source after copying, in addition to the exact descriptor/path stamps.
        self.verify(cancelled=cancelled)
        path, info = _selected(destination.parent, destination.name)
        copied = FileSnapshot(self.digest, self.mime, size, path, _stamp(info))
        return copied.verify(cancelled=cancelled)


def file_snapshot(root, relative, *, max_bytes=MAX_BYTES, cancelled=lambda: False):
    """Screen/hash selected TXT or PDF with a caller-reviewed finite allowance."""
    path, before = _selected(root, relative)
    mime = {'.txt': 'text/plain', '.pdf': 'application/pdf'}.get(path.suffix.lower())
    if mime is None or not 0 < before.st_size <= source_limit(mime, max_bytes):
        raise IntakeError('This source exceeds the selected TXT/PDF streaming allowance.')
    decoder = codecs.getincrementaldecoder('utf-8-sig')('strict') if mime == 'text/plain' else None
    first = True
    def inspect(block):
        nonlocal first
        if first:
            head = block[:262]
            if mime == 'application/pdf':
                if not Pdf().match(head) or not head.removeprefix(b'\xef\xbb\xbf').startswith(b'%PDF-'):
                    raise IntakeError('invalid PDF header')
            elif (Exe().match(head) or head.startswith(b'\x7fELF') or Zip().match(head)
                  or Pdf().match(head) or Rtf().match(head)):
                raise IntakeError('unsupported or mismatched file extension')
            first = False
        if decoder is not None:
            decoder.decode(block)
            if _TEXT_CONTROLS.search(block):
                raise IntakeError('binary control character in text')
    candidate = FileSnapshot('', mime, before.st_size, path, _stamp(before))
    try:
        with candidate.open(cancelled=cancelled) as stream:
            digest, size = _hash_stream(stream, cancelled=cancelled,
                                       maximum=before.st_size, inspect=inspect)
        if decoder is not None:
            decoder.decode(b'', final=True)
    except UnicodeError as exc:
        raise IntakeError('unsupported binary or text encoding') from exc
    if size != before.st_size:
        raise IntakeError('Source changed during file-backed intake.')
    return FileSnapshot(digest, mime, size, path, _stamp(before)).verify(cancelled=cancelled)


def source_snapshot(root, relative, *, max_bytes=MAX_BYTES, cancelled=lambda: False,
                    file_backed=False):
    """Keep existing byte decoders; only strict TXT/PDF can use larger sources."""
    source_limit('text/plain', max_bytes)
    path, before = _selected(root, relative)
    if type(file_backed) is not bool:
        raise IntakeError('Invalid source snapshot mode.')
    if before.st_size > max_bytes:
        raise IntakeError('asset too large for the reviewed byte allowance.')
    if (before.st_size > MAX_BYTES or file_backed) and path.suffix.lower() in ('.txt', '.pdf'):
        return file_snapshot(root, relative, max_bytes=max_bytes, cancelled=cancelled)
    return snapshot(root, relative, max_bytes=min(max_bytes, MAX_BYTES), cancelled=cancelled)


@contextmanager
def source_stream(asset, *, cancelled=lambda: False):
    if isinstance(asset, FileSnapshot):
        with asset.open(cancelled=cancelled) as stream:
            yield stream
    elif isinstance(asset, AssetSnapshot):
        if cancelled():
            raise InterruptedError('Source verification cancelled.')
        if (type(asset.data) is not bytes or len(asset.data) != asset.size
                or sha256(asset.data).hexdigest() != asset.digest):
            raise IntakeError('Immutable source snapshot changed.')
        with BytesIO(asset.data) as stream:
            yield stream
    else:
        raise IntakeError('A reviewed source snapshot is required.')


def copy_snapshot(asset, destination, *, cancelled=lambda: False):
    if isinstance(asset, FileSnapshot):
        return asset.copy_to(destination, cancelled=cancelled)
    if not isinstance(asset, AssetSnapshot):
        raise IntakeError('A reviewed source snapshot is required.')
    with source_stream(asset, cancelled=cancelled) as reader, Path(destination).open('xb') as writer:
        digest, size = _hash_stream(reader, cancelled=cancelled, maximum=asset.size, writer=writer)
        writer.flush()
        os.fsync(writer.fileno())
    if size != asset.size or digest != asset.digest:
        raise IntakeError('Snapshot copy changed.')
    return asset


def _text_records(asset, *, cancelled=lambda: False, max_segments=1000,
                  max_segment_bytes=MAX_BYTES):
    """Consume all records before trusting a result; the final hash covers every byte."""
    if (asset.mime != 'text/plain' or type(max_segments) is not int
            or not 0 < max_segments <= 2 ** 31 - 1
            or type(max_segment_bytes) is not int or not 0 < max_segment_bytes <= MAX_BYTES):
        raise IntakeError('Invalid strict TXT segment allowances.')
    count, start, length, pieces, hashed, total = 0, 0, 0, [], sha256(), 0
    def decoded(raw, index):
        text = raw.decode('utf-8-sig' if index == 0 else 'utf-8', errors='strict')
        if _TEXT_CONTROLS.search(raw):
            raise IntakeError('binary control character in text')
        return text
    with source_stream(asset, cancelled=cancelled) as stream:
        while True:
            if cancelled():
                raise InterruptedError('TXT source verification cancelled.')
            block = stream.read(min(_BLOCK, asset.size - total + 1))
            if not block:
                break
            total += len(block)
            if total > asset.size:
                raise IntakeError('TXT source grew during verification.')
            hashed.update(block)
            parts = block.split(b'\f')
            for position, part in enumerate(parts):
                length += len(part)
                if length > max_segment_bytes:
                    raise IntakeError('One original TXT segment exceeds the selected allowance.')
                pieces.append(part)
                if position < len(parts) - 1:
                    if count >= max_segments:
                        raise IntakeError('TXT source exceeds the selected original segment allowance.')
                    yield start, length, decoded(b''.join(pieces), count)
                    count += 1
                    start += length + 1
                    pieces, length = [], 0
        if count >= max_segments:
            raise IntakeError('TXT source exceeds the selected original segment allowance.')
        yield start, length, decoded(b''.join(pieces), count)
    if total != asset.size or hashed.hexdigest() != asset.digest:
        raise IntakeError('TXT source digest changed during verification.')
    if cancelled():
        raise InterruptedError('TXT source verification cancelled.')


def text_segments(asset, *, cancelled=lambda: False, max_segments=1000,
                  max_segment_bytes=MAX_BYTES):
    for _, _, text in _text_records(asset, cancelled=cancelled,
            max_segments=max_segments, max_segment_bytes=max_segment_bytes):
        yield text


class TextSegments:
    """Packed exact byte ranges; source form-feeds alone define logical segments."""
    def __init__(self, asset, *, cancelled=lambda: False, max_segments=1000,
                 max_segment_bytes=MAX_BYTES):
        ranges, nonempty = array('Q'), False
        for offset, length, text in _text_records(asset, cancelled=cancelled,
                max_segments=max_segments, max_segment_bytes=max_segment_bytes):
            ranges.extend((offset, length))
            nonempty = nonempty or bool(text.strip())
        if not nonempty:
            raise IntakeError('empty text')
        self.asset, self.ranges, self.cancelled = asset, ranges, cancelled

    def __len__(self):
        return len(self.ranges) // 2

    def rebind(self, asset):
        if asset.manifest() != self.asset.manifest():
            raise IntakeError('Staged TXT differs from its source manifest.')
        self.asset = asset

    def _read_at(self, stream, index):
        if type(index) is not int or not 0 <= index < len(self):
            raise IndexError(index)
        offset, size = self.ranges[index * 2:index * 2 + 2]
        if self.cancelled():
            raise InterruptedError('TXT source verification cancelled.')
        stream.seek(offset)
        blocks, remaining = [], size
        while remaining:
            if self.cancelled():
                raise InterruptedError('TXT source verification cancelled.')
            block = stream.read(min(_BLOCK, remaining))
            if not block:
                break
            blocks.append(block)
            remaining -= len(block)
        raw = b''.join(blocks)
        if self.cancelled():
            raise InterruptedError('TXT source verification cancelled.')
        if len(raw) != size:
            raise IntakeError('Staged TXT segment was truncated.')
        return raw.decode('utf-8-sig' if index == 0 else 'utf-8', errors='strict')

    @contextmanager
    def reader(self):
        """Keep one checked descriptor for the bounded private parsing slice."""
        with source_stream(self.asset, cancelled=self.cancelled) as stream:
            yield lambda index: self._read_at(stream, index)

    def __getitem__(self, index):
        with self.reader() as read:
            return read(index)

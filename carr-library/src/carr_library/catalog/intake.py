"""Read-only, bounded catalog intake candidate; never extracts archives or executes data."""
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PureWindowsPath
import os
import stat

from ._filetype.archive import Exe, Pdf, Rtf, Zip
from .office_archive import validate_office

MAX_BYTES = 64 * 1024 * 1024


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


def snapshot(root, relative, *, max_bytes=MAX_BYTES):
    """Read a selected relative file under a trusted local root; return immutable bytes.

    Reject symlinks/reparse points and changed files. Concurrent hostile directory
    replacement is outside this candidate's threat model; use a trusted staging root.
    Callers must parse the returned bytes, not reopen the original path.
    """
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
    if len(data) > max_bytes or len(data) != before.st_size or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        raise IntakeError("asset too large or changed during read")
    mime = classify(data, path.suffix)
    return AssetSnapshot(sha256(data).hexdigest(), mime, len(data), data)

"""Bounded crash-recovery copies of notes; never approved evidence or autosave.

The UI must freshly authorize an evidence record before offering its recovery
copy. This store deliberately does not read, authorize or modify the research
database. A checkpoint keeps the exact source snapshot and saved-note baseline
so a later editor cannot silently attach an old draft to changed evidence.
"""
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat
from tempfile import NamedTemporaryFile
from uuid import uuid4

from .ingest.writer_guard import _private_root, _regular_if_present, exclusive_writer
from .research.anchored_export import SavedEvidence


FILENAME = 'evidence-drafts.json'
MAX_BYTES = 8 * 1024 * 1024
MAX_DRAFTS = 128
# A recovery copy can preserve a draft that exceeds the saved-note limit. Its
# bounded size is separate from the database's 100,000-character note limit.
MAX_NOTE_BYTES = 1024 * 1024
MAX_NOTE_CHARS = 400000


class DraftConflict(ValueError):
    """Another editor changed this recovery copy; neither copy was replaced."""


def _identity(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 256 or '\x00' in value:
        raise ValueError('Invalid recovery-draft identity.')
    return value


def _note(value):
    if not isinstance(value, str) or len(value) > MAX_NOTE_CHARS:
        raise ValueError('Recovery draft is too large; copy it to a separate document before closing.')
    try:
        raw = value.encode('utf-8')
    except UnicodeError as exc:
        raise ValueError('Recovery draft contains invalid Unicode; the editor text was retained.') from exc
    if len(raw) > MAX_NOTE_BYTES:
        raise ValueError('Recovery draft is too large; copy it to a separate document before closing.')
    return value


def _digest(value):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value):
        raise ValueError('Invalid recovery-draft digest.')
    return value


def _revision(value):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{32}', value):
        raise ValueError('Invalid recovery-draft revision.')
    return value


def _binding(row):
    if not isinstance(row, SavedEvidence):
        raise ValueError('An authenticated saved evidence record is required.')
    workspace = _identity(row.anchor.identity.workspace)
    key = workspace, _identity(row.topic_id), _identity(row.evidence_id)
    if any(anchor.identity.workspace != workspace for anchor in row.anchors):
        raise ValueError('Recovery evidence belongs to another library.')
    # Include every historical anchor and its locator/metadata, not just a
    # mutable database ID. Rows supplied by test/adapters need not cache checksum.
    raw = json.dumps([asdict(anchor) for anchor in row.anchors], ensure_ascii=False,
                     sort_keys=True, separators=(',', ':')).encode('utf-8')
    return key, sha256(raw).hexdigest(), sha256(row.note.encode('utf-8')).hexdigest()


@dataclass(frozen=True)
class EvidenceDraft:
    workspace: str
    topic: str
    evidence: str
    source_sha256: str
    baseline_sha256: str
    text: str
    revision: str
    updated_at: str

    def __post_init__(self):
        for value in self.key:
            _identity(value)
        _digest(self.source_sha256)
        _digest(self.baseline_sha256)
        _note(self.text)
        _revision(self.revision)
        try:
            datetime.strptime(self.updated_at, '%Y-%m-%dT%H:%M:%S.%fZ')
        except (ValueError, TypeError) as exc:
            raise ValueError('Invalid recovery-draft timestamp.') from exc

    @property
    def key(self):
        return self.workspace, self.topic, self.evidence

    def source_matches(self, row):
        key, source, _ = _binding(row)
        return self.key == key and self.source_sha256 == source

    def matches(self, row):
        key, source, baseline = _binding(row)
        return (self.key == key and self.source_sha256 == source
                and self.baseline_sha256 == baseline)


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate recovery-draft field.')
        result[key] = value
    return result


def validate_evidence_drafts(raw):
    """Validate backup bytes without revealing them or consulting the database.

    Returns immutable records. Restoring this file never authorizes recovery;
    the native editor still requires the exact freshly authorized evidence.
    """
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_BYTES:
        raise ValueError('Recovery-draft file is empty or exceeds its size limit.')
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Invalid number.')))
        if (type(value) is not dict or set(value) != {'kind', 'version', 'drafts'}
                or value['kind'] != 'carr-evidence-drafts'
                or type(value['version']) is not int or value['version'] != 1
                or type(value['drafts']) is not list or len(value['drafts']) > MAX_DRAFTS):
            raise ValueError('Unsupported recovery-draft file.')
        fields = set(EvidenceDraft.__dataclass_fields__)
        rows = []
        keys = set()
        for data in value['drafts']:
            if type(data) is not dict or set(data) != fields:
                raise ValueError('Invalid recovery-draft fields.')
            draft = EvidenceDraft(**data)
            if draft.key in keys:
                raise ValueError('Duplicate scoped recovery draft.')
            keys.add(draft.key)
            rows.append(draft)
        return tuple(rows)
    except (ValueError, UnicodeError, RecursionError, TypeError) as exc:
        raise ValueError('Recovery drafts are invalid; the existing file was kept.') from exc


def _file_identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


class EvidenceDrafts:
    """Compare-and-save checkpoints under the library's existing writer lock."""
    def __init__(self, root):
        self.root = _private_root(root)
        self.path = self.root / FILENAME
        self.last_warning = ''

    def _read(self):
        _regular_if_present(self.path)
        try:
            before = self.path.lstat()
        except FileNotFoundError:
            return {}, None
        if not 0 < before.st_size <= MAX_BYTES:
            raise ValueError('Recovery-draft file is empty or exceeds its size limit.')
        flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
        with os.fdopen(os.open(self.path, flags), 'rb') as stream:
            opened = os.fstat(stream.fileno())
            if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                    or _file_identity(opened) != _file_identity(before)):
                raise ValueError('Recovery-draft file changed while opening.')
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) != before.st_size or _file_identity(self.path.lstat()) != _file_identity(before):
            raise ValueError('Recovery-draft file changed while reading.')
        rows = validate_evidence_drafts(raw)
        return {row.key: row for row in rows}, _file_identity(before)

    def _publish(self, rows, previous):
        self.last_warning = ''
        if len(rows) > MAX_DRAFTS:
            raise ValueError('Recovery-draft limit reached. Recover or discard older drafts before closing.')
        raw = (json.dumps(dict(kind='carr-evidence-drafts', version=1,
                               drafts=[asdict(row) for _, row in sorted(rows.items())]),
                          ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n').encode('utf-8')
        if len(raw) > MAX_BYTES:
            raise ValueError('Recovery storage is full. Recover or discard older drafts before closing.')
        temporary = None
        try:
            with NamedTemporaryFile(mode='wb', prefix='.evidence-drafts-', suffix='.tmp',
                                    dir=self.root, delete=False) as output:
                temporary = Path(output.name)
                output.write(raw)
                output.flush()
                os.fsync(output.fileno())
            _regular_if_present(self.path)
            try:
                current = _file_identity(self.path.lstat())
            except FileNotFoundError:
                current = None
            if current != previous:
                raise DraftConflict('Recovery storage changed before saving; retry without closing the editor.')
            os.replace(temporary, self.path)
            temporary = None
            # Persist the directory entry where the platform supports it.
            # Replacement has already committed. A directory-fsync failure
            # cannot be reported as an uncommitted checkpoint: the caller must
            # receive its revision so its next write does not conflict with its
            # own successfully published recovery copy.
            if os.name == 'posix':
                try:
                    descriptor = os.open(self.root, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
                    try:
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
                except OSError:
                    self.last_warning = ('Recovery file was published, but directory durability could not be confirmed. '
                                         'Save or copy your note before closing.')
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def get(self, workspace, topic, evidence):
        key = tuple(_identity(value) for value in (workspace, topic, evidence))
        rows, _ = self._read()
        return rows.get(key)

    def checkpoint(self, row, text, *, expected=None):
        key, source, baseline = _binding(row)
        _note(text)
        if expected is not None:
            _revision(expected)
        with exclusive_writer(self.root):
            rows, previous = self._read()
            current = rows.get(key)
            if (current.revision if current else None) != expected:
                raise DraftConflict('Another recovery copy exists. Recover or discard it before replacing it.')
            draft = EvidenceDraft(*key, source, baseline, text, uuid4().hex,
                                  datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ'))
            rows[key] = draft
            self._publish(rows, previous)
            return draft

    def discard(self, draft):
        """Delete only this exact accepted/saved revision; retain newer edits."""
        if not isinstance(draft, EvidenceDraft):
            raise ValueError('A recovery-draft revision is required.')
        with exclusive_writer(self.root):
            rows, previous = self._read()
            if rows.get(draft.key) != draft:
                return False
            del rows[draft.key]
            self._publish(rows, previous)
            return True

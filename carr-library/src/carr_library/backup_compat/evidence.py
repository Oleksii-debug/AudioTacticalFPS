"""Read-only compatibility data validation from checkpoint5580d4d.

No legacy engine, UI, provider transport or credential access is installed here.
The original formats remain inert until an explicit validated import exists.
"""
from dataclasses import asdict, dataclass
from datetime import datetime
from hashlib import sha256
import json
import re
from ..research.anchored_export import SavedEvidence


FILENAME = 'evidence-drafts.json'


MAX_BYTES = 8 * 1024 * 1024


MAX_DRAFTS = 128


MAX_NOTE_BYTES = 1024 * 1024


MAX_NOTE_CHARS = 400000


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

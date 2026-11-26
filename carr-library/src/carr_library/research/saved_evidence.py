"""Single-anchor evidence persistence candidate; no implicit migration or commit."""
from dataclasses import asdict
import json

from ..citations.anchors import Anchor, Identity, Locator, Metadata, digest, resolve_span
from .topic_tree import TopicTree, _id
from .anchored_export import SavedEvidence


def _encode(anchor):
    return json.dumps(asdict(anchor), ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _decode(payload):
    data = json.loads(payload)
    data['identity'] = Identity(**data['identity'])
    data['locator'] = Locator(**data['locator'])
    data['metadata'] = Metadata(**data['metadata'])
    return Anchor(**data)


def _note(value):
    if not isinstance(value, str) or len(value) > 100000 or '\x00' in value:
        raise ValueError('invalid note')
    return value


class SavedEvidenceStore:
    """Authorized workspace, caller-owned transaction and trusted attestors.

    Metadata attestation must independently authorize the historical snapshot.
    The payload checksum detects corruption, not adversarial database rewriting.
    """
    def __init__(self, db, workspace, load_page, attest_metadata):
        self.tree = TopicTree(db, workspace)
        self.db = db
        self.workspace = workspace
        if not callable(load_page) or not callable(attest_metadata):
            raise ValueError('trusted source and metadata readers required')
        self.load_page = load_page
        self.attest_metadata = attest_metadata

    def _verify(self, anchor):
        if not isinstance(anchor, Anchor) or anchor.identity.workspace != self.workspace:
            raise ValueError('cross-workspace evidence')
        if self.attest_metadata(anchor.identity, anchor.metadata) is not True:
            raise ValueError('unattested metadata')
        resolve_span((anchor,), self.load_page)

    def save(self, topic, evidence, anchor, note=''):
        self.tree._visible(topic)
        _id(evidence)
        _note(note)
        self._verify(anchor)
        payload = _encode(anchor)
        self.db.execute('INSERT INTO research_evidence(workspace,topic,id,payload,checksum,note) VALUES(?,?,?,?,?,?)',
                        (self.workspace, topic, evidence, payload, digest(payload), note))

    def edit_note(self, topic, evidence, note):
        self.tree._visible(topic)
        _id(evidence)
        if self.db.execute('UPDATE research_evidence SET note=? WHERE workspace=? AND topic=? AND id=?',
                           (_note(note), self.workspace, topic, evidence)).rowcount != 1:
            raise ValueError('unknown evidence')

    def list_saved(self, topic):
        self.tree._ready()
        _id(topic)
        if not self.db.execute('SELECT 1 FROM research_topic WHERE workspace=? AND id=?',
                               (self.workspace, topic)).fetchone():
            raise ValueError('unknown topic')
        rows = []
        for evidence, payload, checksum, note in self.db.execute(
                'SELECT id,payload,checksum,note FROM research_evidence WHERE workspace=? AND topic=? ORDER BY id',
                (self.workspace, topic)):
            if digest(payload) != checksum:
                raise ValueError('evidence snapshot drift')
            anchor = _decode(payload)
            if _encode(anchor) != payload:
                raise ValueError('invalid snapshot encoding')
            self._verify(anchor)
            rows.append(SavedEvidence(topic, _id(evidence), anchor, _note(note)))
        return tuple(rows)

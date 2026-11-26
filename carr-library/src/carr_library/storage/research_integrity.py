"""Structural V3 snapshot validation, separate from citation authorization."""
from ..citations.anchors import Identity, Locator, resolve
from ..research.saved_evidence import _note
from ..research.saved_topic_query import SavedTopicQueries
from ..research.span_snapshot import decode_saved_span
from ..research.topic_tree import _id, _name, _position
from .attestations import MetadataSnapshots, decode_metadata
from .workspace import _text


def check_research(db):
    """Reject corrupted research data before checked backup publication.

    Archived workspaces/topics and revoked metadata remain valid historical
    records. This check preserves them; it does not grant permission to read,
    copy or export evidence. Runtime attestors still check original source
    bytes/review evidence and current authorization within their transaction.
    """
    if db.execute('SELECT 1 FROM workspace w LEFT JOIN workspace_profile p '
                  'ON p.workspace=w.id WHERE p.workspace IS NULL LIMIT 1').fetchone():
        raise ValueError('workspace profile missing')
    for workspace, name, description, archived in db.execute(
            'SELECT workspace,name,description,archived FROM workspace_profile'):
        _id(workspace)
        _text(name, 'name', 512, required=True)
        _text(description, 'description', 100000)
        if type(archived) is not int or archived not in (0, 1):
            raise ValueError('invalid workspace archive state')

    # UNION terminates even on a corrupt cycle; every node must be reachable
    # from one root in its own workspace, including archived branches.
    if db.execute('''WITH RECURSIVE reachable(workspace,id) AS (
        SELECT workspace,id FROM research_topic WHERE parent IS NULL
        UNION SELECT t.workspace,t.id FROM research_topic t JOIN reachable r
          ON t.workspace=r.workspace AND t.parent=r.id
    ) SELECT 1 FROM research_topic t WHERE NOT EXISTS(
        SELECT 1 FROM reachable r WHERE r.workspace=t.workspace AND r.id=t.id
    ) LIMIT 1''').fetchone():
        raise ValueError('unreachable or cyclic research topic')
    for workspace, topic, parent, name, position, archived in db.execute(
            'SELECT workspace,id,parent,name,position,archived FROM research_topic'):
        _id(workspace)
        _id(topic)
        if parent is not None:
            _id(parent)
        _name(name)
        _position(position)
        if type(archived) is not int or archived not in (0, 1):
            raise ValueError('invalid topic archive state')

    for workspace, edition, asset, revision, payload, checksum, active in db.execute(
            'SELECT workspace,edition,asset,revision,payload,checksum,active FROM metadata_snapshot'):
        Identity(workspace, edition, asset, revision)
        decode_metadata(payload, checksum)
        if type(active) is not int or active not in (0, 1):
            raise ValueError('invalid metadata approval state')

    for workspace, topic, evidence, payload, checksum, note in db.execute(
            'SELECT workspace,topic,id,payload,checksum,note FROM research_evidence'):
        _id(topic)
        _id(evidence)
        _note(note)
        anchors = decode_saved_span(payload, checksum)
        metadata = MetadataSnapshots(db, workspace)
        for anchor in anchors:
            identity, locator = anchor.identity, anchor.locator
            if identity.workspace != workspace:
                raise ValueError('cross-workspace saved evidence')
            if metadata.load(identity, include_revoked=True) != anchor.metadata:
                raise ValueError('saved metadata snapshot drift')
            rows = db.execute('''SELECT p.physical,p.ordinal,p.printed,p.verified,p.raw
                FROM revision r JOIN sealed s ON s.revision=r.id JOIN page p ON p.revision=r.id
                WHERE r.workspace=? AND r.edition=? AND r.asset=? AND r.id=?
                  AND ((?='physical_page' AND p.physical=?) OR
                       (?='logical_segment' AND p.physical IS NULL AND p.ordinal=?))''',
                (workspace, identity.edition_id, identity.asset_sha256, identity.revision,
                 locator.kind, locator.index, locator.kind, locator.index)).fetchmany(2)
            if len(rows) != 1:
                raise ValueError('unbound saved evidence')
            physical, ordinal, printed, verified, raw = rows[0]
            actual_locator = (Locator('physical_page', physical, printed, bool(verified))
                              if physical is not None else Locator('logical_segment', ordinal))
            resolve(anchor, identity, actual_locator, raw)

    for row in db.execute('SELECT id,name,query,field,result_limit,archived FROM research_saved_query'):
        SavedTopicQueries._row(row)

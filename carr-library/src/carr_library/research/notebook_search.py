"""Bounded phrase search across one library's authenticated research evidence.

The disposable FTS index contains separate quotation pages and researcher notes.
It grants no source authority and never changes the persisted research schema.
"""
from dataclasses import dataclass
import sqlite3
from time import monotonic
import unicodedata

from .anchored_export import SavedEvidence
from .topic_tree import _VISIBLE, _id, _name

MAX_ROWS = 5000
MAX_BYTES = 16 * 1024 * 1024
MAX_TOPICS = 10000


@dataclass(frozen=True)
class NotebookHit:
    evidence: SavedEvidence
    topic_path: tuple[str, ...]
    tags: tuple[str, ...]
    archived: bool
    matched_fields: tuple[str, ...]


@dataclass(frozen=True)
class NotebookSearchResult:
    hits: tuple[NotebookHit, ...]
    total_matches: int
    evidence_scanned: int
    topics_scanned: int
    truncated: bool


class _Budget:
    def __init__(self, timeout, cancelled):
        if (type(timeout) not in (int, float) or not 0 < timeout <= 60
                or not callable(cancelled)):
            raise ValueError('invalid notebook search budget')
        self.deadline = monotonic() + timeout
        self.cancelled = cancelled
        self.latched = False

    def stopped(self):
        self.latched = self.latched or bool(self.cancelled()) or monotonic() >= self.deadline
        return self.latched

    def check(self):
        if self.stopped():
            raise TimeoutError('notebook search cancelled or timed out')


class _Topics:
    """Read only ancestors needed for returned hits, within the same snapshot."""
    def __init__(self, store, budget):
        self.store, self.budget = store, budget
        self.nodes = {}
        self.size = 0

    def describe(self, topic):
        names, seen, archived = [], set(), False
        while topic is not None:
            self.budget.check()
            _id(topic)
            if topic in seen:
                raise ValueError('invalid topic ancestry')
            seen.add(topic)
            if topic not in self.nodes:
                if len(self.nodes) >= MAX_TOPICS:
                    raise ValueError('topic paths exceed notebook search budget')
                node = self.store.db.execute('SELECT parent,name,archived FROM research_topic '
                    'WHERE workspace=? AND id=?', (self.store.workspace, topic)).fetchone()
                self.budget.check()
                if node is None or type(node[2]) is not int or node[2] not in (0, 1):
                    raise ValueError('unknown or invalid topic')
                parent, name, own_archived = node
                if parent is not None:
                    _id(parent)
                _name(name)
                self.size += len((topic + (parent or '') + name).encode('utf-8'))
                if self.size > MAX_BYTES:
                    raise ValueError('topic paths exceed notebook search budget')
                self.nodes[topic] = parent, name, own_archived
            parent, name, own_archived = self.nodes[topic]
            names.append(name)
            archived = archived or bool(own_archived)
            topic = parent
        self.budget.check()
        return tuple(reversed(names)), archived


def _scope(store, workspace, include_archived, budget):
    if _id(workspace) != store.workspace:
        raise ValueError('workspace mismatch')
    if type(include_archived) is not bool:
        raise ValueError('explicit archive flag required')
    budget.check()
    store.tree._ready()


def _tags(store, topic, evidence, budget):
    budget.check()
    rows = store.db.execute('SELECT t.name FROM research_evidence_tag e '
        'JOIN research_tag t ON t.workspace=e.workspace AND t.id=e.tag '
        'WHERE e.workspace=? AND e.topic=? AND e.evidence=? ORDER BY t.name,t.id LIMIT 101',
        (store.workspace, topic, evidence)).fetchall()
    budget.check()
    if len(rows) > 100:
        raise ValueError('evidence tags exceed notebook search budget')
    return tuple(_name(row[0]) for row in rows)


def _hit(store, row, fields, topics, include_archived, budget):
    path, archived = topics.describe(row.topic_id)
    if archived and not include_archived:
        raise ValueError('unknown or archived topic')
    tags = _tags(store, row.topic_id, row.evidence_id, budget)
    budget.check()
    return NotebookHit(row, path, tags, archived, fields)


def get_notebook_hit(store, workspace, topic, evidence, *, include_archived=False,
                     timeout=60.0, cancelled=lambda: False):
    """Fresh source, metadata, note, tag and ancestry verification for one hit."""
    budget = _Budget(timeout, cancelled)
    _scope(store, workspace, include_archived, budget)
    _id(topic)
    _id(evidence)
    topics = _Topics(store, budget)
    _, archived = topics.describe(topic)
    if archived and not include_archived:
        raise ValueError('unknown or archived topic')
    row = store.get_saved(topic, evidence, cancelled=budget.stopped)
    return _hit(store, row, (), topics, include_archived, budget)


def search_notebook(store, workspace, query, *, field='both', tag=None,
                    include_archived=False, limit=50, timeout=60.0,
                    cancelled=lambda: False):
    """Search every eligible record before publishing a complete, bounded result.

    Text is a case-insensitive NFC token phrase, never an FTS operator. Quotes
    are indexed one anchor at a time; no match crosses a page or note boundary.
    An exact tag filters the verification scope before any source is loaded.
    Counts describe all authenticated eligible evidence and its distinct topics,
    including nonmatches. Empty topics are not counted. The result limit caps
    returned hits, not verification or the exact total match count.

    Caller owns the source transaction and its SQLite progress handler. The
    application supplies one outer deadline for transaction and source reads;
    native readers cooperate and may finish their current call before stopping.
    """
    budget = _Budget(timeout, cancelled)
    _scope(store, workspace, include_archived, budget)
    if field not in ('quote', 'note', 'both') or type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('invalid notebook search options')
    if not isinstance(query, str) or len(query) > 500 or any(
            unicodedata.category(c) in ('Cc', 'Cs') and c not in '\t\n\r' for c in query):
        raise ValueError('invalid query')
    if tag is not None:
        _name(tag)
    query = unicodedata.normalize('NFC', query).strip()
    if not query and tag is None:
        raise ValueError('enter a phrase or an exact tag')

    prefix, params = ('', []) if include_archived else (_VISIBLE, [workspace, workspace])
    where = ' FROM research_evidence e WHERE e.workspace=?'
    params.append(workspace)
    if not include_archived:
        where += ' AND e.topic IN (SELECT id FROM visible)'
    if tag is not None:
        where += (' AND EXISTS(SELECT 1 FROM research_evidence_tag et '
            'JOIN research_tag t ON t.workspace=et.workspace AND t.id=et.tag '
            'WHERE et.workspace=e.workspace AND et.topic=e.topic AND et.evidence=e.id AND t.name=?)')
        params.append(tag)
    count, size, topic_count = store.db.execute(prefix + 'SELECT count(*), '
        'coalesce(sum(length(CAST(e.payload AS BLOB))+length(CAST(e.note AS BLOB))),0), '
        'count(DISTINCT e.topic)' + where, params).fetchone()
    budget.check()
    if count > MAX_ROWS or size > MAX_BYTES:
        raise ValueError('notebook exceeds search budget; narrow it with an exact tag')

    rows, documents = [], {}
    source = store.db.execute(prefix + 'SELECT e.topic,e.id' + where + ' ORDER BY e.topic,e.id', params)
    index = sqlite3.connect(':memory:')
    try:
        index.set_progress_handler(lambda: int(budget.stopped()), 1000)
        index.execute('PRAGMA temp_store=MEMORY')
        index.execute('''CREATE VIRTUAL TABLE saved_fts USING fts5(text,
            tokenize="unicode61 remove_diacritics 0 categories 'L* N* Co M*'")''')
        for topic, evidence in source:
            budget.check()
            if len(rows) >= count:
                raise ValueError('inconsistent saved evidence snapshot')
            row = store.get_saved(topic, evidence, cancelled=budget.stopped)
            if (not isinstance(row, SavedEvidence) or row.topic_id != topic or row.evidence_id != evidence
                    or not row.anchors or row.anchors[0] != row.anchor
                    or any(a.identity.workspace != workspace for a in row.anchors)):
                raise ValueError('invalid or cross-scope saved evidence')
            rows.append(row)
            if query:
                texts = ([(a.quote, 'quote') for a in row.anchors] if field in ('quote', 'both') else [])
                if field in ('note', 'both'):
                    texts.append((row.note, 'note'))
                for text, matched_field in texts:
                    budget.check()
                    document = len(documents) + 1
                    documents[document] = len(rows) - 1, matched_field
                    index.execute('INSERT INTO saved_fts(rowid,text) VALUES(?,?)',
                                  (document, unicodedata.normalize('NFC', text)))
        budget.check()
        if len(rows) != count:
            raise ValueError('inconsistent saved evidence snapshot')
        found = {}
        if query:
            expression = '"' + query.replace('"', '""') + '"'
            matches = index.execute('SELECT rowid FROM saved_fts WHERE saved_fts MATCH ? '
                                    'ORDER BY bm25(saved_fts),rowid', (expression,))
            for (document,) in matches:
                budget.check()
                position, matched_field = documents[document]
                found.setdefault(position, set()).add(matched_field)
        else:
            found = {position: set() for position in range(len(rows))}
        budget.check()
        topics = _Topics(store, budget)
        hits = []
        for position, fields in found.items():
            if len(hits) == limit:
                break
            hits.append(_hit(store, rows[position], tuple(f for f in ('quote', 'note') if f in fields),
                             topics, include_archived, budget))
        budget.check()
        return NotebookSearchResult(tuple(hits), len(found), count, topic_count, len(found) > limit)
    except sqlite3.OperationalError as exc:
        if getattr(exc, 'sqlite_errorcode', None) == sqlite3.SQLITE_INTERRUPT or budget.stopped():
            raise TimeoutError('notebook search cancelled or timed out') from exc
        raise
    finally:
        source.close()
        index.close()

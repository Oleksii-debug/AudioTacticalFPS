"""Token-phrase search inside one topic's verified saved evidence.

Native SQLite FTS5 is a disposable query index, not a citation authority.
No source mutation, persistent index, schema migration, or generated snippets.
"""
import sqlite3
import unicodedata
from dataclasses import dataclass
from time import monotonic

MAX_ROWS = 5000
MAX_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class TopicHit:
    evidence: object
    matched_field: str


def search_topic(store, workspace, topic, query, *, field='quote', limit=50,
                 timeout=5.0, cancelled=lambda: False):
    """Return original verified saved evidence, never a note as a quotation.

    Caller supplies a workspace-authorized SavedEvidenceStore and one consistent
    transaction. FTS token phrases are not byte-exact substring matches. Search
    notes only when explicitly requested; archive reads follow list_saved.
    A timeout or cancellation never returns partial results. Source readers are
    cooperative; an in-flight native read can exceed the deadline, and the
    caller owns any progress handler on the source database connection.
    """
    if not isinstance(workspace, str) or not workspace.strip() or workspace != store.workspace:
        raise ValueError('workspace mismatch')
    if not isinstance(topic, str) or not topic.strip() or len(topic) > 256 or '\x00' in topic:
        raise ValueError('invalid topic')
    if field not in ('quote', 'note') or type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('invalid search options')
    if (type(timeout) not in (int, float) or not 0 < timeout <= 60
            or not callable(cancelled)):
        raise ValueError('invalid search budget')
    if not isinstance(query, str) or len(query) > 500 or any(
            unicodedata.category(c) in ('Cc', 'Cs') and c not in '\t\n\r' for c in query):
        raise ValueError('invalid query')
    deadline = monotonic() + timeout
    latched = False

    def stopped():
        nonlocal latched
        latched = latched or bool(cancelled()) or monotonic() >= deadline
        return latched

    def check():
        if stopped():
            raise TimeoutError('topic search cancelled or timed out')

    check()
    store.tree._ready()
    query = unicodedata.normalize('NFC', query).strip()
    if not query:
        return ()
    count, size = store.db.execute('''SELECT count(*), coalesce(sum(length(CAST(payload AS BLOB)) +
        length(CAST(note AS BLOB))),0) FROM research_evidence WHERE workspace=? AND topic=?''',
        (workspace, topic)).fetchone()
    check()
    if count > MAX_ROWS or size > MAX_BYTES:
        raise ValueError('topic exceeds search budget')
    # Reuse historical verification, yielding between source reads. The fallback
    # preserves the existing caller-supplied store interface.
    stream = getattr(store, 'iter_saved', None)
    source_rows = (stream(topic, cancelled=stopped) if callable(stream)
                   else iter(store.list_saved(topic)))
    rows = []
    seen = set()
    index = sqlite3.connect(':memory:')
    try:
        index.set_progress_handler(lambda: int(stopped()), 1000)
        index.execute('PRAGMA temp_store=MEMORY')
        index.execute('''CREATE VIRTUAL TABLE saved_fts USING fts5(text,
            tokenize="unicode61 remove_diacritics 0 categories 'L* N* Co M*'")''')
        documents = {}
        for row in source_rows:
            check()
            if len(rows) >= count:
                raise ValueError('inconsistent saved evidence snapshot')
            anchors = getattr(row, 'anchors', (row.anchor,))
            if (row.topic_id != topic or row.anchor.identity.workspace != workspace
                    or row.evidence_id in seen
                    or not anchors or anchors[0] != row.anchor
                    or any(a.identity.workspace != workspace for a in anchors)):
                raise ValueError('invalid or cross-scope saved evidence')
            seen.add(row.evidence_id)
            rows.append(row)
            # Each page is a separate FTS document. Joining page strings would
            # create false phrase matches at a source boundary.
            texts = tuple(a.quote for a in anchors) if field == 'quote' else (row.note,)
            for text in texts:
                check()
                document_id = len(documents) + 1
                documents[document_id] = len(rows) - 1
                index.execute('INSERT INTO saved_fts(rowid,text) VALUES(?,?)',
                              (document_id, unicodedata.normalize('NFC', text)))
        check()
        if len(rows) != count:
            raise ValueError('inconsistent saved evidence snapshot')
        # Parameterized SQL plus FTS5 phrase quoting: user text is never an operator.
        expression = '"' + query.replace('"', '""') + '"'
        result = index.execute('SELECT rowid FROM saved_fts WHERE saved_fts MATCH ? '
                               'ORDER BY bm25(saved_fts), rowid', (expression,))
        found = set()
        hits = []
        for (document_id,) in result:
            check()
            i = documents[document_id]
            if i not in found:
                found.add(i)
                hits.append(TopicHit(rows[i], field))
                if len(hits) == limit:
                    break
        check()
        return tuple(hits)
    except sqlite3.OperationalError as exc:
        if getattr(exc, 'sqlite_errorcode', None) == sqlite3.SQLITE_INTERRUPT or stopped():
            raise TimeoutError('topic search cancelled or timed out') from exc
        raise
    finally:
        close = getattr(source_rows, 'close', None)
        if callable(close):
            close()
        index.close()

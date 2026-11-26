"""Token-phrase search inside one topic's verified saved evidence.

Native SQLite FTS5 is a disposable query index, not a citation authority.
No source mutation, persistent index, schema migration, or generated snippets.
"""
import sqlite3
import unicodedata
from dataclasses import dataclass

MAX_ROWS = 5000
MAX_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class TopicHit:
    evidence: object
    matched_field: str


def search_topic(store, workspace, topic, query, *, field='quote', limit=50):
    """Return original verified saved evidence, never a note as a quotation.

    Caller supplies a workspace-authorized SavedEvidenceStore and one consistent
    transaction. FTS token phrases are not byte-exact substring matches. Search
    notes only when explicitly requested; archive reads follow list_saved.
    """
    if not isinstance(workspace, str) or not workspace.strip() or workspace != store.workspace:
        raise ValueError('workspace mismatch')
    if not isinstance(topic, str) or not topic.strip() or len(topic) > 256 or '\x00' in topic:
        raise ValueError('invalid topic')
    if field not in ('quote', 'note') or type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('invalid search options')
    if not isinstance(query, str) or len(query) > 500 or any(
            unicodedata.category(c) in ('Cc', 'Cs') and c not in '\t\n\r' for c in query):
        raise ValueError('invalid query')
    store.tree._ready()
    query = unicodedata.normalize('NFC', query).strip()
    if not query:
        return ()
    count, size = store.db.execute('''SELECT count(*), coalesce(sum(length(CAST(payload AS BLOB)) +
        length(CAST(note AS BLOB))),0) FROM research_evidence WHERE workspace=? AND topic=?''',
        (workspace, topic)).fetchone()
    if count > MAX_ROWS or size > MAX_BYTES:
        raise ValueError('topic exceeds search budget')
    # Reuse the historical source + metadata verification in PR54 before indexing.
    rows = store.list_saved(topic)
    if len(rows) != count:
        raise ValueError('inconsistent saved evidence snapshot')
    seen = set()
    index = sqlite3.connect(':memory:')
    try:
        index.execute('PRAGMA temp_store=MEMORY')
        index.execute("CREATE VIRTUAL TABLE saved_fts USING fts5(text, tokenize='unicode61 remove_diacritics 0')")
        for i, row in enumerate(rows, 1):
            if (row.topic_id != topic or row.anchor.identity.workspace != workspace
                    or row.evidence_id in seen):
                raise ValueError('invalid or cross-scope saved evidence')
            seen.add(row.evidence_id)
            text = row.anchor.quote if field == 'quote' else row.note
            index.execute('INSERT INTO saved_fts(rowid,text) VALUES(?,?)',
                          (i, unicodedata.normalize('NFC', text)))
        # Parameterized SQL plus FTS5 phrase quoting: user text is never an operator.
        expression = '"' + query.replace('"', '""') + '"'
        result = index.execute('SELECT rowid FROM saved_fts WHERE saved_fts MATCH ? '
                               'ORDER BY bm25(saved_fts), rowid LIMIT ?', (expression, limit))
        return tuple(TopicHit(rows[i-1], field) for (i,) in result)
    finally:
        index.close()

"""A source-backed literature-review matrix across visible research topics.

The caller owns one authorized read transaction and the historical attestors.
Source quotations come only from SavedEvidenceStore, never mutable catalog text.
Each page/segment remains a separate row; research notes occupy their own column.
"""
import csv
import io

from ..catalog.membership import Catalog
from ..catalog.read_views import _snapshot
from ..citations.anchors import format_citation
from .evidence_export import _encoded
from .saved_evidence import SavedEvidenceStore
from .topic_tree import TopicTree


MAX_TOPICS = 10000
MAX_TOPIC_DEPTH = 256
MAX_EVIDENCE = 50000
MAX_SOURCE_SLICES = 100000
MAX_BYTES = 64 * 1024 * 1024

HEADERS = (
    'text_encoding', 'workspace_id_json', 'collection_id_json',
    'topic_id_json', 'topic_path_json', 'evidence_id_json',
    'title_json', 'author_json', 'edition_json', 'volume_json',
    'source_slice', 'source_slices', 'citation_json', 'quotation_json',
    'research_note_json', 'tags_json', 'edition_id_json', 'revision_id_json',
    'asset_sha256_json', 'raw_text_sha256_json', 'locator_kind',
    'physical_page', 'logical_segment', 'printed_page_json',
    'printed_page_verified', 'start', 'end',
)


class _BoundedOutput(io.StringIO):
    def __init__(self):
        super().__init__(newline='')
        self.bytes_written = 0

    def write(self, value):
        self.bytes_written += len(value.encode('utf-8'))
        if self.bytes_written > MAX_BYTES:
            raise ValueError('The review matrix is too large. Export a smaller collection.')
        return super().write(value)


def _topic_paths(topics, check):
    """Retain a path as separate literal names, including names containing '/'."""
    by_id = {topic.id: topic for topic in topics}
    result = {}
    for topic in topics:
        check()
        names, seen, node = [], set(), topic
        while node is not None:
            check()
            if node.id in seen:
                raise ValueError('The topic hierarchy contains a cycle.')
            seen.add(node.id)
            names.append(node.name)
            if len(names) > MAX_TOPIC_DEPTH:
                raise ValueError('The topic hierarchy is too deep for a review matrix.')
            if node.parent is not None and node.parent not in by_id:
                raise ValueError('A visible topic has no visible parent.')
            node = by_id.get(node.parent)
        result[topic.id] = tuple(reversed(names))
    return result


def export_review_matrix(db, workspace, load_page, attest_metadata, *,
                         collection=None, cancelled=lambda: False):
    """Return complete CSV, or fail before a caller publishes any output.

    Includes evidence from visible topics only. A collection is matched against
    current catalog membership, while quotation attribution remains historical.
    All candidate evidence is authenticated before collection filtering, so a
    damaged saved record is never silently bypassed or trusted for its identity.
    Text cells use the existing reversible JSON encoding to protect spreadsheet
    readers from formula execution without altering source text.
    """
    if not callable(cancelled):
        raise ValueError('A cancellation callback is required.')

    def check():
        if cancelled():
            raise TimeoutError('Review matrix export cancelled.')

    check()
    catalog = Catalog(db, workspace)
    _snapshot(catalog)
    if collection is not None:
        catalog._active_collection(collection)
    tree = TopicTree(db, workspace)
    # Bound loading, including archived rows, before list_topics materializes it.
    topic_count = db.execute('SELECT count(*) FROM research_topic WHERE workspace=?',
                             (workspace,)).fetchone()[0]
    if topic_count > MAX_TOPICS:
        raise ValueError('Too many topics for a single review matrix.')
    topics = tree.list_topics()
    paths = _topic_paths(topics, check)
    store = SavedEvidenceStore(db, workspace, load_page, attest_metadata)
    output = _BoundedOutput()
    writer = csv.writer(output, lineterminator='\n')
    writer.writerow(HEADERS)
    evidence_count = slice_count = 0
    for topic in sorted(topics, key=lambda item: (paths[item.id], item.id)):
        check()
        for saved in store.iter_saved(topic.id, cancelled=cancelled):
            check()
            evidence_count += 1
            if evidence_count > MAX_EVIDENCE:
                raise ValueError('Too many saved items for a single review matrix.')
            if collection is not None and not db.execute(
                    'SELECT 1 FROM catalog_membership WHERE workspace=? '
                    'AND collection=? AND edition=?',
                    (workspace, collection, saved.anchor.identity.edition_id)).fetchone():
                continue
            tags = tuple(row[0] for row in db.execute(
                'SELECT t.name FROM research_evidence_tag e JOIN research_tag t '
                'ON t.workspace=e.workspace AND t.id=e.tag '
                'WHERE e.workspace=? AND e.topic=? AND e.evidence=? '
                'ORDER BY t.name COLLATE BINARY,t.id COLLATE BINARY',
                (workspace, topic.id, saved.evidence_id)))
            for position, anchor in enumerate(saved.anchors, 1):
                check()
                slice_count += 1
                if slice_count > MAX_SOURCE_SLICES:
                    raise ValueError('Too many source slices for a single review matrix.')
                identity, locator, metadata = anchor.identity, anchor.locator, anchor.metadata
                writer.writerow((
                    'json-string-v1', _encoded(workspace), _encoded(collection),
                    _encoded(topic.id), _encoded(paths[topic.id]),
                    _encoded(saved.evidence_id), _encoded(metadata.title),
                    _encoded(metadata.author), _encoded(metadata.edition),
                    _encoded(metadata.volume), position, len(saved.anchors),
                    _encoded(format_citation(anchor)), _encoded(anchor.quote),
                    _encoded(saved.note), _encoded(tags), _encoded(identity.edition_id),
                    _encoded(identity.revision), _encoded(identity.asset_sha256),
                    _encoded(anchor.raw_sha256), locator.kind,
                    locator.index if locator.kind == 'physical_page' else '',
                    locator.index if locator.kind == 'logical_segment' else '',
                    _encoded(locator.printed if locator.verified else None),
                    'true' if locator.verified else 'false', anchor.start, anchor.end,
                ))
    check()
    return output.getvalue()

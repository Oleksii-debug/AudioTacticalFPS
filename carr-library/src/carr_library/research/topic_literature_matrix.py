"""Fresh, source-backed comparisons of the evidence in a research topic.

This is a view of the existing research store, not another database or a source
of citation authority. A call uses one authorized read snapshot and verifies
every scoped evidence item before applying an optional exact tag filter.
"""
from collections import defaultdict
from dataclasses import dataclass
import csv
import io
import re
import sqlite3

from ..citations.anchors import Anchor, format_citation
from .evidence_export import _block, _encoded
from .saved_evidence import SavedEvidenceStore
from .topic_tree import TopicTree


MAX_WORKSPACE_TOPICS = 10000
MAX_TOPICS = 1000
MAX_DEPTH = 32
MAX_EVIDENCE = 5000
MAX_SOURCES = 1000
MAX_TAGS_PER_EVIDENCE = 100
MAX_THEMES = 500
MAX_TEXT_BYTES = 16 * 1024 * 1024
MAX_EXPORT_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class MatrixEvidence:
    topic_id: str
    topic_name: str
    evidence_id: str
    anchors: tuple[Anchor, ...]
    note: str
    tags: tuple[str, ...]

    @property
    def quotes(self):
        return tuple(anchor.quote for anchor in self.anchors)

    @property
    def citations(self):
        return tuple(format_citation(anchor) for anchor in self.anchors)

    @property
    def locations(self):
        return tuple(anchor.locator.display() for anchor in self.anchors)


@dataclass(frozen=True)
class TopicSection:
    topic_id: str
    parent_id: str | None
    name: str
    depth: int
    path: tuple[str, ...]
    evidence: tuple[MatrixEvidence, ...]


@dataclass(frozen=True)
class SourceRow:
    edition_id: str
    revision_id: str
    asset_sha256: str
    title: str
    author: str
    edition: str
    volume: str
    evidence: tuple[MatrixEvidence, ...]

    @property
    def evidence_count(self):
        return len(self.evidence)

    @property
    def locations(self):
        return tuple(dict.fromkeys(location for item in self.evidence for location in item.locations))

    @property
    def tags(self):
        return tuple(sorted({tag for item in self.evidence for tag in item.tags}))

    @property
    def notes(self):
        return tuple(item.note for item in self.evidence)

    @property
    def theme_counts(self):
        return tuple((tag, sum(tag in item.tags for item in self.evidence)) for tag in self.tags)


@dataclass(frozen=True)
class LiteratureMatrix:
    workspace: str
    topic_id: str
    topic_name: str
    sections: tuple[TopicSection, ...]
    sources: tuple[SourceRow, ...]
    themes: tuple[str, ...]
    include_descendants: bool
    tag_filter: str | None


def _topics(db, workspace, topic, include_descendants, check):
    # Bound the existing TopicTree traversal before asking it for visible rows.
    count = db.execute('SELECT count(*) FROM (SELECT 1 FROM research_topic '
        'WHERE workspace=? LIMIT ?)', (workspace, MAX_WORKSPACE_TOPICS + 1)).fetchone()[0]
    if count > MAX_WORKSPACE_TOPICS:
        raise ValueError('Too many topics for a research overview.')
    tree = TopicTree(db, workspace)
    tree._visible(topic)
    visible = {row.id: row for row in tree.list_topics()}
    check()
    root = visible[topic]
    children = defaultdict(list)
    for row in visible.values():
        children[row.parent].append(row)
    for siblings in children.values():
        siblings.sort(key=lambda row: (row.position, row.id))
    result, seen = [], set()
    pending = [(root, 0, ())]
    while pending:
        check()
        row, depth, ancestors = pending.pop()
        if row.id in seen:
            raise ValueError('A topic cycle prevents the research overview.')
        if len(result) >= MAX_TOPICS or depth > MAX_DEPTH:
            raise ValueError('Topic count or depth exceeds the research overview limit.')
        seen.add(row.id)
        path = (*ancestors, row.name)
        result.append((row, depth, path))
        if include_descendants:
            pending.extend((child, depth + 1, path) for child in reversed(children[row.id]))
    return result


def build_literature_matrix(app, workspace, topic, *, include_descendants=False,
                            tag_filter=None, cancelled=lambda: False):
    """Compare exact edition/revision snapshots; titles never become identities.

    Descendants follow TopicTree sibling order (position, ID), in pre-order.
    Archived topics and their descendants are excluded. A tag filter is one
    exact, case-sensitive tag name, or None for all evidence. Tags describe the
    user's research; they do not verify a source or imply a model judgment.

    Limits are explicit: 10,000 workspace topics, 1,000 selected topics, depth
    32, 5,000 evidence items, 1,000 source revisions, 500 distinct tags and 16 MiB
    of stored evidence/notes/tag text. Exceeding a limit raises, never truncates.
    """
    if type(include_descendants) is not bool or not callable(cancelled):
        raise ValueError('Explicit descendant selection and cancellation callback required.')
    if tag_filter is not None and (not isinstance(tag_filter, str) or not tag_filter.strip()
            or len(tag_filter) > 512 or '\x00' in tag_filter):
        raise ValueError('Choose one exact tag name or no tag filter.')
    if tag_filter is not None:
        tag_filter.encode('utf-8', errors='strict')
    latched = False

    def stopped():
        nonlocal latched
        latched = latched or bool(cancelled())
        return latched

    def check():
        if stopped():
            raise TimeoutError('Research overview cancelled.')

    check()
    sections, grouped, metadata = [], {}, {}
    themes = set()
    total_items = total_bytes = 0
    with app.read(workspace) as db:
        db.set_progress_handler(stopped, 1000)
        try:
            selected = _topics(db, workspace, topic, include_descendants, check)
            load, attest_metadata, _ = app._loaders(db, workspace, cancelled=stopped)
            store = SavedEvidenceStore(db, workspace, load, attest_metadata)
            for selected_topic, depth, path in selected:
                check()
                count, size = db.execute('SELECT count(*),coalesce(sum(size),0) FROM '
                    '(SELECT length(CAST(payload AS BLOB))+length(CAST(note AS BLOB)) AS size '
                    'FROM research_evidence WHERE workspace=? AND topic=? LIMIT ?)',
                    (workspace, selected_topic.id, MAX_EVIDENCE + 1)).fetchone()
                total_items += count
                total_bytes += size
                if total_items > MAX_EVIDENCE or total_bytes > MAX_TEXT_BYTES:
                    raise ValueError('Evidence count or text exceeds the research overview limit.')
                tags = defaultdict(list)
                cursor = db.execute('SELECT e.evidence,t.name FROM research_evidence_tag e '
                    'JOIN research_tag t ON t.workspace=e.workspace AND t.id=e.tag '
                    'WHERE e.workspace=? AND e.topic=? ORDER BY e.evidence,t.name',
                    (workspace, selected_topic.id))
                try:
                    for evidence_id, tag in cursor:
                        check()
                        tags[evidence_id].append(tag)
                        total_bytes += len(tag.encode('utf-8'))
                        themes.add(tag)
                        if (len(tags[evidence_id]) > MAX_TAGS_PER_EVIDENCE
                                or len(themes) > MAX_THEMES or total_bytes > MAX_TEXT_BYTES):
                            raise ValueError('Tags exceed the research overview limit.')
                finally:
                    cursor.close()
                items = []
                # Even filtered-out evidence is verified: a filter must never
                # turn a damaged or revoked scoped source into a green report.
                for saved in store.iter_saved(selected_topic.id, cancelled=stopped):
                    check()
                    item_tags = tuple(tags.get(saved.evidence_id, ()))
                    if tag_filter is not None and tag_filter not in item_tags:
                        continue
                    item = MatrixEvidence(selected_topic.id, selected_topic.name,
                        saved.evidence_id, saved.anchors, saved.note, item_tags)
                    identity, meta = saved.anchor.identity, saved.anchor.metadata
                    key = (identity.edition_id, identity.revision)
                    source = (identity.asset_sha256, meta.title, meta.author or '',
                              meta.edition or '', meta.volume or '')
                    if key not in grouped:
                        if len(grouped) >= MAX_SOURCES:
                            raise ValueError('Too many source revisions for a research overview.')
                        grouped[key], metadata[key] = [], source
                    elif metadata[key] != source:
                        raise ValueError('Conflicting immutable metadata for one source revision.')
                    grouped[key].append(item)
                    items.append(item)
                sections.append(TopicSection(selected_topic.id, selected_topic.parent,
                    selected_topic.name, depth, path, tuple(items)))
            check()
        except (sqlite3.OperationalError, InterruptedError) as exc:
            if stopped():
                raise TimeoutError('Research overview cancelled.') from exc
            raise
        finally:
            db.set_progress_handler(None, 0)
    check()
    sources = tuple(SourceRow(*key, *metadata[key], tuple(items)) for key, items in grouped.items())
    shown_themes = tuple(sorted({tag for source in sources for tag in source.tags}))
    return LiteratureMatrix(workspace, topic, sections[0].name, tuple(sections), sources,
                             shown_themes, include_descendants, tag_filter)


def _inline(value, *, table_cell=False):
    """A reversible code span; table separators need a separate Markdown escape."""
    value = _encoded(value)
    fence = '`' * (max([0] + [len(run) for run in re.findall(r'`+', value)]) + 1)
    if table_cell:
        value = value.replace('|', '\\|')
    return f'{fence} {value} {fence}'


def _bounded_export(chunks, cancelled):
    result, size = [], 0
    for chunk in chunks:
        if cancelled():
            raise TimeoutError('Research export cancelled.')
        size += len(chunk.encode('utf-8'))
        if size > MAX_EXPORT_BYTES:
            raise ValueError('Research export exceeds the output size limit.')
        result.append(chunk)
    if cancelled():
        raise TimeoutError('Research export cancelled.')
    return ''.join(result)


def export_matrix(app, workspace, topic, *, format='csv', include_descendants=True,
                  tag_filter=None, cancelled=lambda: False):
    """Export a newly verified comparison; no previously displayed rows are used.

    CSV has one row per exact edition/revision. Untrusted text and structured
    cells use reversible JSON, so source strings cannot become spreadsheet
    formulas. Evidence IDs, topic paths, quotation slices, citations and notes
    use the same item order. Quotations stay separate; notes remain user text.
    """
    if format not in ('csv', 'markdown'):
        raise ValueError('Choose CSV or Markdown for a literature matrix.')
    matrix = build_literature_matrix(app, workspace, topic,
        include_descendants=include_descendants, tag_filter=tag_filter, cancelled=cancelled)

    def chunks():
        if format == 'csv':
            buffer = io.StringIO(newline='')
            writer = csv.writer(buffer, lineterminator='\n')
            def row(values):
                buffer.seek(0)
                buffer.truncate()
                writer.writerow(values)
                return buffer.getvalue()
            yield row(('text_encoding', 'workspace_id_json', 'root_topic_id_json',
                'include_descendants', 'tag_filter_json',
                'edition_id_json', 'revision_id_json', 'asset_sha256_json', 'title_json',
                'author_json', 'edition_json', 'volume_json', 'evidence_count',
                'locations_json', 'themes_json', 'theme_counts_json', 'evidence_ids_json',
                'topic_paths_json', 'quotation_slices_json', 'citations_json', 'research_notes_json'))
            paths = {section.topic_id: section.path for section in matrix.sections}
            for source in matrix.sources:
                refs = [(item.topic_id, item.evidence_id) for item in source.evidence]
                yield row(('json-v1', _encoded(workspace), _encoded(topic),
                    'true' if include_descendants else 'false', _encoded(tag_filter),
                    *(_encoded(getattr(source, key)) for key in ('edition_id', 'revision_id',
                        'asset_sha256', 'title', 'author', 'edition', 'volume')),
                    source.evidence_count, _encoded(source.locations), _encoded(source.tags),
                    _encoded(source.theme_counts), _encoded(refs),
                    _encoded([paths[item.topic_id] for item in source.evidence]),
                    _encoded([item.quotes for item in source.evidence]),
                    _encoded([item.citations for item in source.evidence]), _encoded(source.notes)))
            return
        yield '# Literature review matrix\n\n'
        yield ('Text fields use reversible JSON strings. Quotations are exact source slices; '
               'research notes and themes are supplied by the researcher.\n\n')
        for label, value in (('Library ID', workspace), ('Root topic', matrix.topic_name),
                ('Root topic ID', topic), ('Include descendants', include_descendants),
                ('Exact tag filter', tag_filter)):
            yield f'**{label}**\n\n{_block(value)}\n\n'
        yield '| Source | Title | Author | Evidence | Themes |\n| --- | --- | --- | ---: | --- |\n'
        for number, source in enumerate(matrix.sources, 1):
            yield f'| {number} | {_inline(source.title, table_cell=True)} | {_inline(source.author, table_cell=True)} | {source.evidence_count} | {_inline(source.tags, table_cell=True)} |\n'
        if not matrix.sources:
            yield '\nNo matching saved evidence.\n'
        for number, source in enumerate(matrix.sources, 1):
            yield f'\n## Source {number}\n\n'
            for label, value in (('Title', source.title), ('Author', source.author),
                    ('Edition', source.edition), ('Volume', source.volume),
                    ('Edition ID', source.edition_id), ('Revision ID', source.revision_id),
                    ('Asset SHA-256', source.asset_sha256), ('Theme counts', source.theme_counts)):
                yield f'### {label}\n\n{_block(value)}\n\n'
            for item in source.evidence:
                yield f'### Evidence\n\n{_block((item.topic_id, item.evidence_id))}\n\n'
                for index, (quote, citation, location) in enumerate(zip(item.quotes, item.citations, item.locations), 1):
                    yield f'**Source slice {index} of {len(item.anchors)}**\n\n'
                    for label, value in (('Exact quotation', quote), ('Citation', citation), ('Location', location)):
                        yield f'**{label}**\n\n{_block(value)}\n\n'
                for label, value in (('Research note', item.note), ('Researcher tags', item.tags)):
                    yield f'**{label}**\n\n{_block(value)}\n\n'

    return _bounded_export(chunks(), cancelled)

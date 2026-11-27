"""Bounded, read-only Markdown export of one visible research topic branch.

The caller authorizes the workspace and owns one consistent SQLite snapshot.
Existing saved-evidence authentication and Markdown formatting remain the
authority for every quotation, historical locator and metadata snapshot.
"""
import re

from .anchored_export import export_saved
from .evidence_export import _block, _encoded
from .saved_evidence import SavedEvidenceStore
from .topic_tree import Topic, TopicTree, _id, _name, _position
from .topic_literature_matrix import _bounded_export, _inline as _matrix_inline, build_literature_matrix

MAX_TOPICS = 1000
MAX_EVIDENCE = 10000
MAX_BYTES = 16 * 1024 * 1024
MAX_TAGS = 100


def _topic(row):
    identity, parent, name, position, archived = row
    if type(archived) is not int or archived != 0:
        raise ValueError('Research outline contains an archived or invalid topic.')
    return Topic(_id(identity), _id(parent) if parent is not None else None,
                 _name(name), _position(position), False)


def _branch(db, workspace, topic_id, check):
    tree = TopicTree(db, workspace)
    tree._visible(topic_id)
    check()
    selected = _topic(db.execute(
        'SELECT id,parent,name,position,archived FROM research_topic '
        'WHERE workspace=? AND id=?', (workspace, topic_id)).fetchone())
    pending = [(selected, (1,))]
    result, seen = [], set()
    while pending:
        check()
        topic, number = pending.pop()
        if topic.id in seen:
            raise ValueError('Research outline contains a topic cycle.')
        seen.add(topic.id)
        result.append((topic, number))
        remaining = MAX_TOPICS - len(result) - len(pending)
        if remaining < 0:
            raise ValueError('Research outline exceeds the topic limit.')
        children = db.execute(
            'SELECT id,parent,name,position,archived FROM research_topic '
            'WHERE workspace=? AND parent=? AND archived=0 '
            'ORDER BY position,id LIMIT ?',
            (workspace, topic.id, remaining + 1)).fetchall()
        if len(children) > remaining:
            raise ValueError('Research outline exceeds the topic limit.')
        pending.extend(reversed([(_topic(row), (*number, index))
                                 for index, row in enumerate(children, 1)]))
    return result


def _inline(value):
    """Keep topic names on one line and out of Markdown/HTML syntax."""
    text = _encoded(value)
    fence = '`' * (max([0] + [len(run) for run in re.findall(r'`+', text)]) + 1)
    return f'{fence} {text} {fence}'


def _evidence_markdown(workspace, topic, row, number, load_page):
    # The established renderer JSON-encodes all source text inside fenced
    # blocks. Only its fixed structural headings are demoted, so a deep topic
    # does not become a sibling of the document's root. Source bytes stay exact.
    rendered = export_saved(workspace, topic, (row,), load_page, format='markdown')
    lines = []
    for line in rendered.splitlines():
        if line == '# Research evidence':
            continue
        if line.startswith('## '):
            line = '**' + line[3:].replace('Evidence 1', f'Evidence {number}', 1) + '**'
        elif line.startswith('### '):
            line = '**' + line[4:] + '**'
        lines.append(line)
    return '\n'.join(lines).strip() + '\n'


def _tags(db, workspace, topic, evidence):
    rows = db.execute(
        'SELECT e.tag,t.name FROM research_evidence_tag e LEFT JOIN research_tag t '
        'ON t.workspace=e.workspace AND t.id=e.tag '
        'WHERE e.workspace=? AND e.topic=? AND e.evidence=? '
        'ORDER BY t.name,t.id LIMIT ?',
        (workspace, topic, evidence, MAX_TAGS + 1)).fetchall()
    if len(rows) > MAX_TAGS:
        raise ValueError('Research outline evidence exceeds the tag limit.')
    names = []
    for identity, name in rows:
        _id(identity)
        names.append(_name(name))
    if len(set(names)) != len(names):
        raise ValueError('Research outline contains duplicate evidence tags.')
    return names


def export_outline(db, workspace, topic_id, *, load_page, attest_metadata,
                   cancelled=lambda: False):
    """Return one complete Markdown document, or raise without writing anything.

    ``load_page(identity, locator)`` returns the freshly verified historical
    ``(identity, locator, raw_text, True)``. ``attest_metadata(identity, metadata)``
    must return exactly True for the frozen citation snapshot. These are the
    first two values from LibraryApp._loaders in the same authorized read.

    The selected topic must be visible. Archived branches are excluded; sibling
    ordering follows TopicTree's (position, id) convention. Limits apply to the
    selected branch: 1000 topics, 10000 evidence records, 16 MiB of stored
    snapshot/note input and 16 MiB of UTF-8 output. Cancellation raises
    InterruptedError around queries and source reads; no partial text escapes.
    """
    if not all(callable(value) for value in (load_page, attest_metadata, cancelled)):
        raise ValueError('Trusted source readers and cancellation callback are required.')

    def check():
        if cancelled():
            raise InterruptedError('Research outline export cancelled.')
        return False

    def guarded_load(*args):
        check()
        result = load_page(*args)
        check()
        return result

    def guarded_metadata(*args):
        check()
        result = attest_metadata(*args)
        check()
        return result

    check()
    branch = _branch(db, workspace, topic_id, check)
    total, input_bytes = 0, 0
    for topic, _ in branch:
        check()
        sizes = db.execute(
            'SELECT length(CAST(payload AS BLOB))+length(CAST(note AS BLOB)) '
            'FROM research_evidence WHERE workspace=? AND topic=? LIMIT ?',
            (workspace, topic.id, MAX_EVIDENCE - total + 1))
        try:
            for size, in sizes:
                check()
                total += 1
                input_bytes += size
                if total > MAX_EVIDENCE:
                    raise ValueError('Research outline exceeds the evidence limit.')
                if input_bytes > MAX_BYTES:
                    raise ValueError('Research outline source snapshots exceed the byte limit.')
        finally:
            sizes.close()

    parts, output_bytes = [], 0

    def append(value):
        nonlocal output_bytes
        check()
        value = value.rstrip('\n') + '\n\n'
        output_bytes += len(value.encode('utf-8'))
        if output_bytes > MAX_BYTES:
            raise ValueError('Research outline exceeds the UTF-8 output byte limit.')
        parts.append(value)

    append('# Research outline')
    append(f'Topics: {len(branch)}. Evidence records: {total}.\n\n'
           'Only the selected topic and its visible descendants are included; '
           'archived branches are omitted. Section numbers preserve the topic '
           'hierarchy and sibling order. Headings stop at level six; longer '
           'section numbers retain deeper nesting.\n\n'
           'Quoted text, citations, notes and tags use reversible JSON encoding '
           'in fenced blocks. Research notes and tags are separate from source quotations.')
    store = SavedEvidenceStore(db, workspace, guarded_load, guarded_metadata)
    for topic, number in branch:
        check()
        section = '.'.join(str(part) for part in number)
        append(f'{"#" * min(len(number) + 1, 6)} {section} {_inline(topic.name)}')
        append('**Topic ID**\n\n' + _block(topic.id))
        count = 0
        rows = store.iter_saved(topic.id, cancelled=check)
        try:
            for count, row in enumerate(rows, 1):
                check()
                append(_evidence_markdown(workspace, topic.id, row, count, guarded_load))
                append('**Research tags**\n\n' + _block(_tags(db, workspace, topic.id, row.evidence_id)))
        finally:
            rows.close()
        if not count:
            append('No saved evidence in this topic.')
    TopicTree(db, workspace)._ready()
    check()
    result = ''.join(parts)
    check()
    return result


def export_topic_outline(app, workspace, topic, *, format='markdown', include_descendants=True,
                   tag_filter=None, cancelled=lambda: False):
    """Return Markdown/text with topic order, exact slices and separate user notes.

    This always rebuilds the authorized view. It does not accept cached UI rows,
    infer an argument, merge source quotations, or manufacture bibliographic data.
    Numbered section paths retain hierarchy even beyond Markdown's six headings.
    """
    if format not in ('markdown', 'text'):
        raise ValueError('Choose Markdown or plain text for a research outline.')
    matrix = build_literature_matrix(app, workspace, topic,
        include_descendants=include_descendants, tag_filter=tag_filter, cancelled=cancelled)

    def field(label, value):
        return (f'**{label}**\n\n{_block(value)}\n\n' if format == 'markdown'
                else f'{label}: {_encoded(value)}\n\n')

    def chunks():
        yield '# Research outline\n\n' if format == 'markdown' else 'Research outline\n\n'
        yield ('Text fields use reversible JSON strings. Exact quotations, citations, '
               'researcher notes and tags are kept separate.\n\n')
        yield field('Library ID', workspace)
        yield field('Root topic ID', topic)
        yield field('Include descendants', include_descendants)
        if tag_filter is not None:
            yield field('Exact tag filter', tag_filter)
        numbers, siblings = {}, {}
        for section in matrix.sections:
            parent_number = numbers.get(section.parent_id, ())
            siblings[section.parent_id] = siblings.get(section.parent_id, 0) + 1
            number = (*parent_number, siblings[section.parent_id])
            numbers[section.topic_id] = number
            label = '.'.join(map(str, number))
            if format == 'markdown':
                yield f'{"#" * min(section.depth + 2, 6)} Section {label}: {_matrix_inline(section.name)}\n\n'
            else:
                yield f'Section {label}: {_encoded(section.name)}\n\n'
            yield field('Topic path', section.path)
            yield field('Topic ID', section.topic_id)
            if not section.evidence:
                yield 'No matching saved evidence in this section.\n\n'
            for index, item in enumerate(section.evidence, 1):
                yield f'**Evidence {index}**\n\n' if format == 'markdown' else f'Evidence {index}\n\n'
                yield field('Evidence ID', item.evidence_id)
                identity = item.anchors[0].identity
                yield field('Edition ID', identity.edition_id)
                yield field('Revision ID', identity.revision)
                yield field('Asset SHA-256', identity.asset_sha256)
                for position, anchor in enumerate(item.anchors, 1):
                    yield f'Source slice {position} of {len(item.anchors)}\n\n'
                    yield field('Exact quotation', anchor.quote)
                    yield field('Citation', item.citations[position - 1])
                    yield field('Location', item.locations[position - 1])
                    yield field('Raw text SHA-256', anchor.raw_sha256)
                    yield field('Offsets (half-open Unicode)', f'{anchor.start}:{anchor.end}')
                yield field('Research note', item.note)
                yield field('Researcher tags', item.tags)
        if not matrix.sources:
            yield 'No matching saved evidence in the selected topic scope.\n'

    return _bounded_export(chunks(), cancelled)


# Existing callers retain the same freshly authorized renderer.
export_filtered_outline = export_topic_outline

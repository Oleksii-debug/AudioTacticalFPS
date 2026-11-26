"""Hierarchical outlines from freshly authorized, immutable research evidence."""
from .evidence_export import _block, _encoded
from .topic_literature_matrix import _bounded_export, _inline, build_literature_matrix


def export_outline(app, workspace, topic, *, format='markdown', include_descendants=True,
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
                yield f'{"#" * min(section.depth + 2, 6)} Section {label}: {_inline(section.name)}\n\n'
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

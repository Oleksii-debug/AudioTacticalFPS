"""Regenerate a new DOCX from the existing citation snapshot and CSL engine.

The recovery algorithm follows the canonical regeneration contract. Source
metadata and Word text are separate authorities: visible edits are not imported,
and an old or newly supplied locator never becomes verified quotation evidence.
"""
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path

from .csl import (
    CustomStyle, LOCALES, PROCESSOR_PACKAGE_VERSION, PROCESSOR_REPORTED_VERSION,
    RenderBudget, _HASHES, _STYLE_BY_ID, _checked_custom_style, _clusters, _records,
)
from .documents import (
    CitationParagraph, _MAX_DOCUMENT, _read_citation_bytes, _text, citation_document,
)
from ..reader.narration_text import read_document
from ..research.export_file import ExportDestination, capture_export_destination, write_binary_export


def _normalized_clusters(values, rows, identities, budget, *, kind):
    clusters, _, contexts, _ = _clusters(values, rows, identities, budget, note_style=kind == 'note')
    return [dict(id=context['id'], items=cluster, note_index=context['note_index'] or None)
            for cluster, context in zip(clusters, contexts)]


def _body(value, identifiers):
    if (type(value) is not dict or set(value) != {'title', 'paragraphs'}
            or type(value['paragraphs']) is not list or len(value['paragraphs']) > 10_000):
        raise ValueError('The saved document paragraphs are invalid.')
    title = _text(value['title'], 'saved document title', 255)
    if not title.strip():
        raise ValueError('The saved document title is blank.')
    paragraphs, total, sequence = [], 0, []
    for row in value['paragraphs']:
        if (type(row) is not dict or set(row) != {'text', 'citation_ids'}
                or type(row['citation_ids']) is not list or len(row['citation_ids']) > 100
                or any(type(identity) is not str for identity in row['citation_ids'])):
            raise ValueError('The saved citation paragraph is invalid.')
        text = _text(row['text'], 'saved document paragraph', 200_000)
        total += len(text)
        if total > 4_000_000:
            raise ValueError('The saved document text budget was exceeded.')
        sequence.extend(row['citation_ids'])
        paragraphs.append(CitationParagraph(text, tuple(row['citation_ids'])))
    if sequence != identifiers:
        raise ValueError('The saved document citation order is inconsistent.')
    return title, tuple(paragraphs)


def _style(snapshot, selected):
    value = snapshot['style']
    if (type(value) is not dict
            or set(value) != {'id', 'title', 'kind', 'filename', 'sha256', 'custom_source'}):
        raise ValueError('The saved style identity is invalid.')
    if value['custom_source'] is None:
        if selected is not None:
            raise ValueError('This snapshot uses an installed style, not a selected custom file.')
        if not isinstance(value['id'], str) or value['id'] not in _STYLE_BY_ID:
            raise ValueError('The saved style is not installed.')
        info = _STYLE_BY_ID[value['id']]
        expected = {**asdict(info), 'sha256': _HASHES['styles/' + info.filename], 'custom_source': None}
        result = info.id
    else:
        if type(selected) is not CustomStyle:
            raise ValueError('Explicitly select the original local custom CSL file before regeneration.')
        selected = _checked_custom_style(selected)
        expected = {**asdict(selected.info), 'sha256': selected.sha256,
                    'custom_source': selected.source.decode('utf-8')}
        result = selected
    if value != expected:
        raise ValueError('The saved style identity changed; choose the exact original style.')
    return result, value['kind']


def regenerate_citation_docx(source, destination, *, private_root,
                             records=None, citations=None, paragraphs=None,
                             changed_reference_ids=(), custom_style=None,
                             cancelled=lambda: False, approval=None):
    """Reformat the explicit embedded snapshot into a new DOCX, never overwrite.

    Moving, inserting or removing occurrences requires the complete new ordered
    citation list and matching typed paragraphs. Changed references with retained
    locators require a fresh explicit citation request. Supplying that request
    does not verify a locator. Word edits remain in the untouched source file;
    this operation uses only its embedded snapshot.
    """
    if not callable(cancelled):
        raise ValueError('A regeneration cancellation callback is required.')

    def check():
        if cancelled():
            raise TimeoutError('Citation document regeneration cancelled.')

    check()
    current_destination = capture_export_destination(destination, private_root=private_root)
    if approval is None:
        approval = current_destination
    elif type(approval) is not ExportDestination or approval != current_destination:
        raise ValueError('The export destination changed. Choose the destination again.')
    if not approval.require_absent:
        raise FileExistsError('Regeneration requires a new document; the existing file was kept.')
    path = Path(source).absolute()
    original = read_document(path, byte_budget=_MAX_DOCUMENT)
    original_hash = sha256(original).hexdigest()
    snapshot = _read_citation_bytes(original)
    del original
    if snapshot['processor'] != {'package': PROCESSOR_PACKAGE_VERSION,
                                 'reported': PROCESSOR_REPORTED_VERSION}:
        raise ValueError('The saved processor identity changed; format a separately reviewed document.')
    if snapshot['locale'] not in LOCALES:
        raise ValueError('The saved citation locale is unavailable.')
    style, kind = _style(snapshot, custom_style)
    budget = RenderBudget()
    saved, identities, _ = _records(snapshot['records'], budget)
    if saved != snapshot['records']:
        raise ValueError('The saved references are not in the normalized processing format.')
    previous = _normalized_clusters(snapshot['clusters'], saved, identities, budget, kind=kind)
    if previous != snapshot['clusters']:
        raise ValueError('The saved citation context is not in the normalized processing format.')
    identifiers = [row['id'] for row in previous]
    if identifiers != snapshot['citation_ids']:
        raise ValueError('The saved citation occurrence identities changed.')
    title, body = _body(snapshot['document'], identifiers)
    current, current_ids, _ = _records(saved if records is None else records, budget)
    old_by_id, new_by_id = ({row['id']: row for row in rows} for rows in (saved, current))
    changed = {identity for identity in identities if old_by_id[identity] != new_by_id.get(identity)}
    if (type(changed_reference_ids) not in (tuple, list)
            or len(changed_reference_ids) > budget.max_records
            or any(type(identity) is not str or identity not in identities for identity in changed_reference_ids)):
        raise ValueError('Changed source IDs must name bounded saved references.')
    changed.update(changed_reference_ids)
    if citations is None:
        if any('locator' in item and item['id'] in changed
               for cluster in previous for item in cluster['items']):
            raise ValueError('A cited source changed. Review its locators and supply explicit new citations.')
        selected = previous
    else:
        selected = _normalized_clusters(citations, current, current_ids, budget, kind=kind)
    # Reject deleted references even when their old citation has no locator.
    selected = _normalized_clusters(selected, current, current_ids, budget, kind=kind)
    chosen_body = body if paragraphs is None else paragraphs
    check()
    content = citation_document(current, style=style, locale=snapshot['locale'],
        citations=selected, paragraphs=chosen_body, title=title, cancelled=cancelled)

    def source_unchanged():
        check()
        if sha256(read_document(path, byte_budget=_MAX_DOCUMENT)).hexdigest() != original_hash:
            raise ValueError('The source document changed during regeneration; read it again.')
        return False

    source_unchanged()
    write_binary_export(destination, content, private_root=private_root,
        require_absent=True, approval=approval, cancelled=source_unchanged)
    return Path(destination).absolute()

"""Regenerate a new citation document from its explicit embedded snapshot."""
from __future__ import annotations

from hashlib import sha256
import re

from carr_library.durable_state import local_path, regular
from .documents import (
    CitationParagraph, _MAX_DOCUMENT, _read_citation_docx_bytes, export_citation_docx,
)
from .formatting import (
    CitationCancelled, CitationCluster, CitationError, CitationRequest,
    _MAX_CLUSTERS, _MAX_REFERENCES, _clusters, _identifier, _manifest, _reference, _text,
    render_citations,
)


def _references(values):
    if not isinstance(values, (list, tuple)) or not 1 <= len(values) <= _MAX_REFERENCES:
        raise CitationError('The saved references must be a bounded CSL array.')
    try:
        result = [_reference(value) for value in values]
    except (TypeError, KeyError) as exc:
        raise CitationError('The saved reference fields are invalid.') from exc
    if len({value['id'] for value in result}) != len(result):
        raise CitationError('Reference IDs must be unique.')
    return result


def _restore_clusters(values, ids):
    if not isinstance(values, list) or len(values) > _MAX_CLUSTERS:
        raise CitationError('The saved citation cluster budget was exceeded.')
    result = []
    for cluster in values:
        if not isinstance(cluster, dict) or set(cluster) != {'id', 'items', 'note_index', 'locator_evidence'}:
            raise CitationError('The saved citation cluster is invalid.')
        items, evidence = cluster['items'], cluster['locator_evidence']
        if (not isinstance(items, list) or not 1 <= len(items) <= 100
                or not isinstance(evidence, list) or len(evidence) != len(items)):
            raise CitationError('The saved citation items or locator checks are invalid.')
        requests = []
        for item, check in zip(items, evidence):
            if (not isinstance(item, dict) or 'id' not in item
                    or set(item) - {'id', 'locator', 'label', 'prefix', 'suffix', 'suppress-author'}
                    or not isinstance(check, dict)
                    or set(check) != {'reference_id', 'locator_verified'}
                    or check['reference_id'] != item['id']
                    or ('locator' in item and check['locator_verified'] is not True)
                    or ('locator' not in item and check['locator_verified'] is not None)):
                raise CitationError('The saved citation locator evidence is inconsistent; review the source.')
            requests.append(CitationRequest(_identifier(item['id'], 'Saved reference ID'), item.get('locator'),
                _text(item.get('label', 'page'), 'Saved locator label', 200), check['locator_verified'] is True,
                item.get('prefix', ''), item.get('suffix', ''), item.get('suppress-author', False)))
        result.append(CitationCluster(tuple(requests), cluster['id'], cluster['note_index']))
    # Reuse the canonical formatter's identifier, item, note and locator guards.
    # Reject anything that would be dropped or silently normalized on recovery.
    if _clusters(result, ids) != values:
        raise CitationError('The saved citation clusters are not in the supported normalized format.')
    return tuple(result)


def regenerate_citation_docx(source, destination, *, references=None, citations=None,
                             paragraphs=None, changed_reference_ids=(), style_path=None,
                             node_path=None, cancelled=None, timeout=30,
                             expected_source_sha256=None, before_publish=None):
    """Reformat the embedded CARR snapshot into a new, non-overwriting DOCX.

    Visible edits made in Word are not imported. By default the saved title,
    paragraph text, citation IDs and note indices are retained. Explicit
    ``references``, typed ``citations`` and ``paragraphs`` can replace them.
    Declare source changes absent from CSL metadata in ``changed_reference_ids``.
    Retained locators for changed references require explicit newly reviewed
    citation requests. Saved locator checks are prior caller assertions, not
    fresh source verification. The exact saved processor and style are required;
    a custom style additionally needs its explicitly selected local file.
    ``expected_source_sha256`` binds a reviewed document selection across queued
    work. ``before_publish`` can recheck caller authorization after rendering.
    """
    def cancel():
        if cancelled and cancelled():
            raise CitationCancelled('Citation document regeneration was cancelled.')

    cancel()
    if expected_source_sha256 is not None and (
            type(expected_source_sha256) is not str
            or re.fullmatch('[0-9a-f]{64}', expected_source_sha256) is None):
        raise CitationError('The reviewed source digest must be a SHA-256 value.')
    if before_publish is not None and not callable(before_publish):
        raise CitationError('A document publication check must be callable.')
    path = local_path(source, exists=True)
    original_stat = regular(path)
    if original_stat.st_size > _MAX_DOCUMENT:
        raise CitationError('The citation document exceeds its size budget.')
    with path.open('rb') as handle:
        raw = handle.read(_MAX_DOCUMENT + 1)
    if len(raw) > _MAX_DOCUMENT:
        raise CitationError('The citation document exceeds its size budget.')
    if len(raw) != original_stat.st_size:
        raise CitationError('The source document changed during regeneration; read it again.')
    original_hash = sha256(raw).hexdigest()
    if expected_source_sha256 is not None and original_hash != expected_source_sha256:
        raise CitationError('The selected source document changed. Open and review it again.')
    snapshot = _read_citation_docx_bytes(raw)
    del raw
    if type(snapshot.get('schema')) is not int or snapshot['schema'] != 1:
        raise CitationError('The saved citation document uses an unsupported schema.')
    if snapshot.get('processor') != _manifest()['processor']:
        raise CitationError('The saved processor identity changed; review and explicitly format a new document.')
    saved_refs = _references(snapshot.get('references'))
    if saved_refs != snapshot['references']:
        raise CitationError('The saved reference metadata is not the supported processing copy.')
    saved_ids = {row['id'] for row in saved_refs}
    saved_clusters = _restore_clusters(snapshot.get('clusters'), saved_ids)
    document, style = snapshot.get('document'), snapshot.get('style')
    if (not isinstance(document, dict) or set(document) != {'title', 'paragraphs'}
            or not isinstance(document['paragraphs'], list) or len(document['paragraphs']) > 10_000
            or not isinstance(style, dict) or not {'id', 'sha256', 'kind'} <= set(style)):
        raise CitationError('The saved citation document or style metadata is invalid.')
    if not _text(document['title'], 'Saved document title', 255).strip():
        raise CitationError('The saved document title is blank.')
    body = []
    body_size = 0
    for paragraph in document['paragraphs']:
        if (not isinstance(paragraph, dict) or set(paragraph) != {'text', 'citation_ids'}
                or not isinstance(paragraph['citation_ids'], list) or len(paragraph['citation_ids']) > 100):
            raise CitationError('The saved citation paragraph is invalid.')
        body_size += len(_text(paragraph['text'], 'Saved document paragraph', 200_000))
        if body_size > 4_000_000:
            raise CitationError('The saved document text budget was exceeded.')
        body.append(CitationParagraph(paragraph['text'], tuple(paragraph['citation_ids'])))
    # Even an explicit edit cannot disguise a malformed source snapshot.
    if [identifier for paragraph in body for identifier in paragraph.citation_ids] != [c.id for c in saved_clusters]:
        raise CitationError('The saved document citation order is inconsistent.')
    current_refs = saved_refs if references is None else _references(references)
    old_by_id, new_by_id = ({row['id']: row for row in rows} for rows in (saved_refs, current_refs))
    changed = {key for key in saved_ids if old_by_id[key] != new_by_id.get(key)}
    for index, identifier in enumerate(changed_reference_ids):
        if index >= _MAX_REFERENCES or _identifier(identifier, 'Changed reference ID') not in saved_ids:
            raise CitationError('Changed source IDs must name bounded saved references.')
        changed.add(identifier)
    if citations is None:
        if any(item.locator is not None and item.reference_id in changed
               for cluster in saved_clusters for item in cluster.items):
            raise CitationError('A cited source changed. Review its locators and supply explicit newly checked citations.')
        selected = saved_clusters
    else:
        # Capture a processing copy so mutable caller containers cannot change
        # the reviewed request while the worker runs.
        selected = _restore_clusters(_clusters(citations, set(new_by_id)), set(new_by_id))
    cancel()
    result = render_citations(current_refs, style=style['id'], locale=snapshot.get('locale'),
        citations=selected, style_path=style_path, node_path=node_path, cancelled=cancelled, timeout=timeout)
    if (result.style.sha256 != style['sha256'] or result.style.kind != style['kind']
            or result.style.id != style['id']):
        raise CitationError('The saved style identity changed; select the exact original style before regenerating.')
    def final_check():
        cancel()
        current = regular(path)
        if (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns, current.st_ctime_ns) != (
                original_stat.st_dev, original_stat.st_ino, original_stat.st_size,
                original_stat.st_mtime_ns, original_stat.st_ctime_ns):
            raise CitationError('The source document changed during regeneration; read it again.')
        return False

    def verify_source():
        # Do full content verification once after rendering and once after DOCX
        # assembly, rather than re-reading a large document for every paragraph.
        final_check()
        digest, size = sha256(), 0
        with path.open('rb') as handle:
            while True:
                cancel()
                block = handle.read(min(1024 * 1024, original_stat.st_size + 1 - size))
                if not block:
                    break
                size += len(block)
                if size > original_stat.st_size:
                    raise CitationError('The source document changed during regeneration; read it again.')
                digest.update(block)
        if size != original_stat.st_size or digest.hexdigest() != original_hash:
            raise CitationError('The source document changed during regeneration; read it again.')
        final_check()

    verify_source()
    def publication_check():
        verify_source()
        if before_publish is not None:
            before_publish()
        cancel()

    return export_citation_docx(result, destination, title=document['title'],
        paragraphs=body if paragraphs is None else paragraphs, cancelled=final_check,
        before_publish=publication_check)


"""Retained EPUB XHTML mapping over the existing source-bound translation jobs.

One whole canonical spine range is partitioned without changing its text or
source positions. Parsing, resource ownership and user-file publication reuse
the current DOCX format boundary and the existing export service.
"""
from dataclasses import dataclass
import json
from pathlib import Path, PurePosixPath

from .models import canonical
from .translation_documents import SelectedDocument, review_documents, prepare_documents
from .translation_partition import make_partition, partition_ranges, partition_chunk_groups
from .translation_epub_layout_worker import (
    MAX_ARCHIVE, MAX_CHARACTERS, MAX_UNITS, NOTICE, PROFILE, _canonical_text)
from .translation_layout import (
    _root_identity, _bound_cancel, _source_cancel, _snapshot, _invoke,
    _hash, _same_layout_anchor)
from ..ingest.writer_guard import exclusive_writer
from ..reader.reading_desk import ReadingDesk
from ..research.export_file import write_binary_export
from ..speech._io import check_cancelled
from ..speech.source import read_source_range

_MIME = 'application/epub+zip'


def _member(name):
    return (type(name) is str and 0 < len(name) <= 4096 and not name.startswith('/')
            and '\\' not in name and ':' not in name and str(PurePosixPath(name)) == name
            and not any(part in ('.', '..') for part in PurePosixPath(name).parts)
            and not any(ord(c) < 32 or ord(c) == 127 for c in name))


def _digest(value):
    return (type(value) is str and len(value) == 64
            and all(c in '0123456789abcdef' for c in value))


def _mapping(value):
    if (type(value) is not dict or set(value) != {'schema', 'source_sha256', 'source_size',
            'package', 'records', 'characters', 'selected', 'notice', 'quotation_authority'}
            or value['schema'] != PROFILE or value['notice'] != NOTICE
            or value['quotation_authority'] is not False
            or type(value['source_size']) is not int or not 0 < value['source_size'] <= MAX_ARCHIVE
            or not _digest(value['source_sha256']) or not _member(value['package'])
            or type(value['records']) is not list or not 1 <= len(value['records']) <= 1000):
        raise ValueError('The EPUB layout inventory is invalid.')
    selected = characters = 0
    paths = set()
    for ordinal, row in enumerate(value['records'], 1):
        if (type(row) is not dict or set(row) != {'ordinal', 'locator', 'text', 'sha256', 'units'}
                or type(row['ordinal']) is not int or row['ordinal'] != ordinal
                or type(row['text']) is not str or '\0' in row['text']
                or row['sha256'] != _hash(row['text'].encode('utf-8'))
                or type(row['locator']) is not dict or set(row['locator']) != {
                    'spine_index', 'manifest_id', 'href', 'linear', 'title'}
                or type(row['units']) is not list):
            raise ValueError('An EPUB chapter mapping is invalid.')
        locator = row['locator']
        if (type(locator['spine_index']) is not int or locator['spine_index'] != ordinal
                or type(locator['manifest_id']) is not str or not 1 <= len(locator['manifest_id']) <= 512
                or not _member(locator['href']) or locator['href'] in paths
                or type(locator['linear']) is not bool
                or type(locator['title']) is not str or len(locator['title']) > 512):
            raise ValueError('The EPUB reading order differs from its unique local XHTML resources.')
        paths.add(locator['href'])
        previous = 0
        for unit in row['units']:
            selected += 1
            if (type(unit) is not dict or set(unit) != {
                    'ordinal', 'locator', 'start', 'end', 'sha256', 'preformatted', 'inline_mode'}
                    or type(unit['ordinal']) is not int or unit['ordinal'] != selected
                    or type(unit['locator']) is not list or not 1 <= len(unit['locator']) <= 128
                    or any(type(n) is not int or not 0 <= n <= 100000 for n in unit['locator'])
                    or type(unit['start']) is not int or type(unit['end']) is not int
                    or not previous <= unit['start'] < unit['end'] <= len(row['text'])
                    or row['text'][previous:unit['start']].strip()
                    or not row['text'][unit['start']:unit['end']].strip()
                    or unit['sha256'] != _hash(row['text'][unit['start']:unit['end']].encode('utf-8'))
                    or type(unit['preformatted']) is not bool
                    or unit['inline_mode'] not in ('first-textual-inline-style', 'one-whole-block-hyperlink')):
                raise ValueError('An EPUB text block is not an exact nonoverlapping source range.')
            previous = unit['end']
        if row['text'][previous:].strip():
            raise ValueError('The EPUB has source text outside its approved layout units.')
        characters += len(row['text'])
    if (type(value['characters']) is not int or value['characters'] != characters
            or characters > MAX_CHARACTERS or type(value['selected']) is not int
            or value['selected'] != selected or not 1 <= selected <= MAX_UNITS
            or value['package'] in paths):
        raise ValueError('The EPUB exceeds its existing source/partition budget.')
    return value


def _partition(mapping, selected, mapping_sha256):
    if not isinstance(selected, SelectedDocument):
        raise ValueError('Capture the complete retained EPUB spine range.')
    document = selected.document
    anchor = document.source_anchor
    if (anchor is None or len(anchor['positions']) != len(mapping['records'])
            or document.text != document.source_text
            or document.source_text != '\n\n'.join(row['text'] for row in mapping['records'])):
        raise ValueError('The EPUB partition differs from its unchanged canonical source.')
    boundaries, previous = [], 0
    for row, position in zip(mapping['records'], anchor['positions']):
        if (position['ordinal'] != row['ordinal'] or position['text_sha256'] != row['sha256']
                or document.source_text[position['start']:position['end']] != row['text']):
            raise ValueError('The EPUB chapter source positions changed.')
        for unit in row['units']:
            for last in (position['start'] + unit['start'], position['start'] + unit['end']):
                if last > previous:
                    boundaries.append(last)
                    previous = last
    if previous < len(document.source_text):
        boundaries.append(len(document.source_text))
    return make_partition(document.source_text, PROFILE, mapping_sha256, boundaries)


@dataclass(frozen=True)
class EpubLayoutDocument:
    library_root: str
    root_identity: tuple
    workspace: str
    edition: str
    revision: str
    asset_sha256: str
    title: str
    topic: str | None
    mapping_json: str
    selections: tuple

    @property
    def mapping(self):
        value = _mapping(json.loads(self.mapping_json))
        if canonical(value) != self.mapping_json:
            raise ValueError('The captured EPUB mapping is not canonical.')
        return value

    @property
    def mapping_sha256(self):
        return _hash(self.mapping_json.encode('utf-8'))

    @property
    def source_partition(self):
        if len(self.selections) != 1:
            raise ValueError('The EPUB layout needs one complete retained range.')
        return _partition(self.mapping, self.selections[0], self.mapping_sha256)

    @property
    def source_partitions(self):
        return (self.source_partition,)

    def summary(self):
        mapping = self.mapping
        return {'schema': PROFILE, 'title': self.title, 'workspace': self.workspace,
            'edition': self.edition, 'revision': self.revision,
            'source_sha256': self.asset_sha256, 'mapping_sha256': self.mapping_sha256,
            'paragraphs': mapping['selected'], 'chapters': len(mapping['records']),
            'characters': len(self.selections[0].document.source_text),
            'partitioned_jobs': 1, 'source_partition': self.source_partition,
            'notice': NOTICE, 'quotation_authority': False}


def _checked_desk(library, layout, *, cancelled, guard):
    if library is None or not isinstance(layout, EpubLayoutDocument):
        raise ValueError('Capture an explicitly selected retained EPUB first.')
    base = _bound_cancel(library, (layout.library_root, layout.root_identity), cancelled, guard)
    check_cancelled(base)
    desk = ReadingDesk(library, layout.workspace, layout.edition, topic=layout.topic,
                       revision=layout.revision, cancelled=base)
    mapping = layout.mapping
    if (desk.identity.asset_sha256 != layout.asset_sha256
            or mapping['source_sha256'] != layout.asset_sha256
            or len(mapping['records']) != len(desk.pages)):
        raise ValueError('The EPUB original or reading order changed.')
    for row, page in zip(mapping['records'], desk.pages):
        if page.kind != 'logical_segment' or page.ordinal != row['ordinal'] or page.text_sha256 != row['sha256']:
            raise ValueError('The EPUB mapping differs from its retained canonical chapter.')
    selected = layout.selections[0] if len(layout.selections) == 1 else None
    partition = layout.source_partition
    anchor = selected.document.source_anchor
    if (selected.topic != layout.topic or anchor['workspace'] != layout.workspace
            or anchor['edition'] != layout.edition or anchor['revision'] != layout.revision
            or anchor['asset_sha256'] != layout.asset_sha256):
        raise ValueError('The EPUB retained-source anchor changed.')
    partition_ranges(selected.document.source_text, partition)
    return desk, base


def capture_layout(library, workspace, edition, *, topic=None, cancelled=lambda: False,
                   guard=lambda: None, broker=None):
    guard()
    expected = _root_identity(library.root)
    base = _bound_cancel(library, expected, cancelled, guard)
    check_cancelled(base)
    desk = ReadingDesk(library, workspace, edition, topic=topic, cancelled=base)
    stopped = _source_cancel(desk, base)
    mapping = _invoke(library, desk, action='describe', cancelled=stopped, broker=broker, kind='epub')
    if len(mapping['records']) != len(desk.pages):
        raise ValueError('The EPUB differs from its retained chapter inventory.')
    for row, page in zip(mapping['records'], desk.pages):
        if page.kind != 'logical_segment' or page.ordinal != row['ordinal'] or page.text_sha256 != row['sha256']:
            raise ValueError('The EPUB mapping differs from its retained canonical chapter.')
    selected = read_source_range(desk, 1, len(desk.pages), cancelled=base, guard=guard,
                                 max_characters=MAX_CHARACTERS)
    document = EpubLayoutDocument(expected[0], expected[1], workspace, edition, desk.revision,
        desk.identity.asset_sha256, desk.title, topic, canonical(mapping),
        (SelectedDocument(selected.reading_document(), topic),))
    document.source_partition
    check_cancelled(stopped)
    return document


def review_layout(service, profile, layout, source, target, budget, *, glossary=None,
                  rate_per_million=None, cancelled=lambda: False, guard=lambda: None):
    desk, base = _checked_desk(service.library, layout, cancelled=cancelled, guard=guard)
    stopped = _source_cancel(desk, base)
    actual = _invoke(service.library, desk, action='describe', cancelled=stopped, kind='epub')
    if canonical(actual) != layout.mapping_json:
        raise ValueError('The EPUB mapping changed before translation review.')
    report = review_documents(service, profile, layout.selections, source, target, budget,
        glossary=glossary, rate_per_million=rate_per_million, cancelled=stopped, guard=guard,
        source_partitions=layout.source_partitions)
    payload = {'schema': 'carr-epub-translation-review/1', 'layout': layout.summary(),
               'documents': report, 'provider_calls_started': False}
    return {**payload, 'review_sha256': _hash(canonical(payload).encode('utf-8'))}


def prepare_layout(service, profile, layout, source, target, budget, *, approved_review,
                   glossary=None, rate_per_million=None, cancelled=lambda: False, guard=lambda: None):
    review = review_layout(service, profile, layout, source, target, budget,
        glossary=glossary, rate_per_million=rate_per_million, cancelled=cancelled, guard=guard)
    if approved_review != review['review_sha256']:
        raise PermissionError('Review and approve this exact EPUB mapping, limitations and budget.')
    desk, base = _checked_desk(service.library, layout, cancelled=cancelled, guard=guard)
    return prepare_documents(service, profile, layout.selections, source, target, budget,
        approved_review=review['documents']['review_sha256'], glossary=glossary,
        rate_per_million=rate_per_million, cancelled=_source_cancel(desk, base), guard=guard,
        source_partitions=layout.source_partitions)


def _job(service, layout, identity, *, cancelled, guard):
    job = service.get(identity)
    binding = service._binding(job)
    selected = layout.selections[0]
    if (binding is None or binding['topic'] != layout.topic
            or job.get('source_partition') != layout.source_partition
            or job['source_text'] != selected.document.source_text
            or not _same_layout_anchor(binding['anchor'], selected.document.source_anchor)):
        raise ValueError('The saved job differs from the exact reviewed EPUB source partition.')
    service._source_context(job, cancelled=cancelled, guard=guard)
    return job


def order_layout_jobs(service, layout, identities, *, cancelled=lambda: False, guard=lambda: None):
    desk, base = _checked_desk(service.library, layout, cancelled=cancelled, guard=guard)
    if (type(identities) not in (tuple, list) or len(identities) != 1
            or type(identities[0]) is not str):
        raise ValueError('Explicitly choose the one complete saved EPUB partition job.')
    _job(service, layout, identities[0], cancelled=_source_cancel(desk, base), guard=guard)
    return tuple(identities)


def _completed(service, layout, identity, *, cancelled, guard):
    from .translation import _assemble_translation
    check_cancelled(cancelled)
    guard()
    job = _job(service, layout, identity, cancelled=cancelled, guard=guard)
    if job['status'] != 'complete' or len(job['receipts']) != len(job['chunks']):
        raise ValueError('Finish every reviewed XHTML partition before EPUB export.')
    service._output_budget(job)
    service._text_binding(job)
    groups = partition_chunk_groups(job['source_text'], job['source_partition'])
    intervals = partition_ranges(job['source_text'], job['source_partition'])
    translated, index = {}, 0
    for interval, chunks in zip(intervals, groups):
        rows = job['receipts'][index:index + len(chunks)]
        if len(rows) != len(chunks):
            raise ValueError('The accepted EPUB receipt inventory is incomplete.')
        translated[interval] = (_assemble_translation(chunks, rows),
                                tuple(range(index, index + len(chunks))))
        index += len(chunks)
    if index != len(job['chunks']) or index != len(job['receipts']):
        raise ValueError('The accepted EPUB receipt inventory changed.')
    replacements, receipts = [], []
    for row, position in zip(layout.mapping['records'],
                             layout.selections[0].document.source_anchor['positions']):
        for unit in row['units']:
            key = (position['start'] + unit['start'], position['start'] + unit['end'])
            if key not in translated:
                raise ValueError('An XHTML block is not an exact approved source partition.')
            value, indices = translated[key]
            replacements.append({'ordinal': unit['ordinal'], 'source_sha256': unit['sha256'], 'text': value})
            receipts.append({'id': identity, 'ordinal': unit['ordinal'],
                'source_position': row['ordinal'], 'source_sha256': unit['sha256'],
                'translation_sha256': _hash(value.encode('utf-8')),
                'xhtml_text_sha256': _hash(_canonical_text(value, unit['preformatted']).encode('utf-8')),
                'receipt_indices': list(indices)})
    return replacements, receipts, job['target_language']


def export_layout(service, layout, identities, destination, *, approval=None,
                  cancelled=lambda: False, guard=lambda: None, broker=None):
    desk, base = _checked_desk(service.library, layout, cancelled=cancelled, guard=guard)
    stopped = _source_cancel(desk, base)
    requested = Path(destination)
    if requested.suffix.lower() != '.epub':
        raise ValueError('Choose a new EPUB output filename.')
    identities = order_layout_jobs(service, layout, identities, cancelled=stopped, guard=guard)
    with service.jobs.lock(identities[0]):
        replacements, receipts, language = _completed(service, layout, identities[0],
                                                       cancelled=stopped, guard=guard)
        data, result = _invoke(service.library, desk, action='render', replacements=replacements,
            cancelled=stopped, broker=broker, kind='epub', target_language=language)
        expected_members = sorted({layout.mapping['package'],
                                   *(row['locator']['href'] for row in layout.mapping['records'])})
        if (result['mapping_sha256'] != layout.mapping_sha256
                or result['changed_members'] != expected_members):
            raise ValueError('The completed EPUB mapping differs from the captured source review.')
        with exclusive_writer(service.library.root):
            def publishing_cancelled():
                if stopped():
                    return True
                _snapshot(service.library, desk, base, mime=_MIME)
                return stopped()
            check_cancelled(publishing_cancelled)
            exported = write_binary_export(requested, data, private_root=service.library.root,
                cancelled=publishing_cancelled, require_absent=True, approval=approval)
            return {'status': 'complete', 'path': str(exported.path),
                'cleanup_warning': exported.cleanup_warning, 'format': 'epub',
                'source_sha256': layout.asset_sha256, 'source_revision': layout.revision,
                'mapping_sha256': layout.mapping_sha256, 'jobs': receipts,
                'output_sha256': result['output_sha256'], 'output_bytes': len(data),
                'changed_members': result['changed_members'],
                'preserved_member_count': len(result['preserved_members']),
                'quotation_authority': False, 'notice': NOTICE}

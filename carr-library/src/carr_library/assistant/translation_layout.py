"""Source-bound DOCX derivatives over the existing translation jobs.

This is a format adapter, not a provider, job store or execution queue. Parsing
runs in the existing owned process/resource boundary. Complete stored jobs can
export offline after restore; further provider requests retain their original
current-account, original-root and approval rules.
"""
from contextlib import ExitStack
from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from uuid import uuid4

from .models import canonical
from .translation_documents import (SelectedDocument, review_documents, prepare_documents,
                                    MAX_DOCUMENTS)
from .translation_partition import make_partition, partition_ranges, partition_chunk_groups
from .translation_layout_worker import MAX_ARCHIVE, MAX_CHARACTERS, MAX_REPLY, MAX_REQUEST, MAX_UNITS, NOTICE, PROFILE
from ..audio.models import AudioBudget, AudioCleanupPending
from ..audio.process import run_process
from ..catalog.intake import AssetSnapshot, source_snapshot, copy_snapshot
from ..durable_state import atomic_json, encode, local_path, read_json, regular
from ..ingest.writer_guard import exclusive_writer
from ..operations.broker import ResourceBroker
from ..operations.models import ResourceRequest
from ..reader.reading_desk import ReadingDesk
from ..research.export_file import write_binary_export
from ..speech._io import check_cancelled
from ..speech.source import read_source_range

_DOCX = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'


def _hash(value):
    return sha256(value).hexdigest()


def _root_identity(root):
    path = local_path(root, exists=True)
    info = regular(path, directory=True)
    return str(path), (info.st_dev, info.st_ino)


def _bound_cancel(library, expected, cancelled, guard):
    def stopped():
        guard()
        if _root_identity(library.root) != expected:
            raise ValueError('The selected library directory changed. Reopen the formatted document.')
        return bool(cancelled())
    return stopped


def _mapping(value):
    if (type(value) is not dict or set(value) != {'schema', 'source_sha256', 'source_size',
            'records', 'characters', 'selected', 'notice', 'quotation_authority'}
            or value['schema'] != PROFILE or value['notice'] != NOTICE
            or value['quotation_authority'] is not False
            or type(value['source_size']) is not int or not 0 < value['source_size'] <= MAX_ARCHIVE
            or type(value['source_sha256']) is not str or len(value['source_sha256']) != 64
            or any(c not in '0123456789abcdef' for c in value['source_sha256'])
            or type(value['records']) is not list or not 1 <= len(value['records']) <= 10000):
        raise ValueError('The DOCX format inventory is invalid.')
    characters = selected = 0
    for ordinal, row in enumerate(value['records'], 1):
        if (type(row) is not dict or set(row) != {'ordinal', 'locator', 'text', 'sha256', 'selected', 'inline_mode'}
                or type(row['ordinal']) is not int or row['ordinal'] != ordinal
                or type(row['locator']) is not list or len(row['locator']) not in (1, 4)
                or any(type(n) is not int or not 1 <= n <= 10000 for n in row['locator'])
                or type(row['text']) is not str or '\0' in row['text']
                or type(row['selected']) is not bool or row['selected'] != bool(row['text'].strip())
                or row['sha256'] != _hash(row['text'].encode('utf-8', 'strict'))
                or row['inline_mode'] not in ('first-textual-run-style', 'one-whole-paragraph-hyperlink')):
            raise ValueError('A DOCX paragraph mapping is invalid.')
        characters += len(row['text'])
        selected += int(row['selected'])
    if (type(value['characters']) is not int or value['characters'] != characters
            or characters > MAX_CHARACTERS or type(value['selected']) is not int
            or value['selected'] != selected or not 1 <= selected <= MAX_UNITS):
        raise ValueError('The DOCX translation selection exceeds its existing batch budget.')
    return value


@dataclass(frozen=True)
class LayoutDocument:
    """A passive in-memory selection; it grants no source or provider authority."""
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
    partition_json: str | None = None

    @property
    def mapping(self):
        value = _mapping(json.loads(self.mapping_json))
        if canonical(value) != self.mapping_json:
            raise ValueError('The captured DOCX mapping is not canonical.')
        return value

    @property
    def mapping_sha256(self):
        return _hash(self.mapping_json.encode('utf-8'))

    @property
    def source_partition(self):
        if self.partition_json is None:
            return None
        if len(self.selections) != 1:
            raise ValueError('A combined DOCX partition needs one complete retained source.')
        expected = _combined_partition(self.mapping, self.selections[0], self.mapping_sha256)
        if canonical(expected) != self.partition_json:
            raise ValueError('The captured DOCX partition differs from its exact source mapping.')
        return expected

    @property
    def source_partitions(self):
        value = self.source_partition
        return None if value is None else (value,)

    def summary(self):
        data = self.mapping
        return {'schema': PROFILE, 'title': self.title, 'workspace': self.workspace,
            'edition': self.edition, 'revision': self.revision,
            'source_sha256': self.asset_sha256, 'mapping_sha256': self.mapping_sha256,
            'paragraphs': data['selected'], 'characters': data['characters'],
            'notice': NOTICE, 'quotation_authority': False,
            **({'partitioned_jobs': 1, 'source_partition': self.source_partition}
               if self.partition_json is not None else {})}


def _combined_partition(mapping, selection, mapping_sha256):
    if not isinstance(selection, SelectedDocument):
        raise ValueError('Capture the complete unchanged DOCX range before partitioning it.')
    document = selection.document
    anchor = document.source_anchor
    rows = mapping['records']
    if (anchor is None or len(anchor['positions']) != len(rows)
            or document.text != document.source_text
            or document.source_text != '\n\n'.join(row['text'] for row in rows)):
        raise ValueError('The combined DOCX source differs from its canonical reading range.')
    boundaries, previous = [], 0
    for row, position in zip(rows, anchor['positions']):
        first, last = position['start'], position['end']
        if (position['ordinal'] != row['ordinal']
                or position['text_sha256'] != row['sha256']
                or document.source_text[first:last] != row['text']):
            raise ValueError('The DOCX partition source positions changed.')
        for end in (first, last):
            if end > previous:
                boundaries.append(end)
                previous = end
    if previous < len(document.source_text):
        boundaries.append(len(document.source_text))
    return make_partition(document.source_text, PROFILE, mapping_sha256, boundaries)


def _same_layout_anchor(actual, expected):
    if (set(actual) != set(expected)
            or any(actual[key] != expected[key] for key in actual if key != 'positions')
            or len(actual['positions']) != len(expected['positions'])):
        return False
    return all(set(a) == set(b) and all(a[key] == b[key] for key in a
                                      if key != 'text_verified_at_capture')
               for a, b in zip(actual['positions'], expected['positions']))


def _partition_job(service, layout, identity, *, cancelled, guard):
    job = service.get(identity)
    binding = service._binding(job)
    selection = layout.selections[0]
    if (binding is None or binding['topic'] != layout.topic
            or job.get('source_partition') != layout.source_partition
            or job['source_text'] != selection.document.source_text
            or not _same_layout_anchor(binding['anchor'], selection.document.source_anchor)):
        raise ValueError('The saved job differs from the exact reviewed DOCX source partition.')
    service._source_context(job, cancelled=cancelled, guard=guard)
    return job


def _checked_desk(library, layout, *, cancelled, guard):
    if library is None:
        raise ValueError('Open the retained source library before formatted translation.')
    if not isinstance(layout, LayoutDocument):
        raise ValueError('Capture an explicitly selected retained DOCX first.')
    base = _bound_cancel(library, (layout.library_root, layout.root_identity), cancelled, guard)
    check_cancelled(base)
    desk = ReadingDesk(library, layout.workspace, layout.edition, topic=layout.topic,
                       revision=layout.revision, cancelled=base)
    if desk.identity.asset_sha256 != layout.asset_sha256:
        raise ValueError('The selected DOCX original changed.')
    mapping = layout.mapping
    if mapping['source_sha256'] != layout.asset_sha256 or len(mapping['records']) != len(desk.pages):
        raise ValueError('The DOCX mapping no longer matches its retained revision.')
    for row, page in zip(mapping['records'], desk.pages):
        if page.kind != 'logical_segment' or page.ordinal != row['ordinal'] or page.text_sha256 != row['sha256']:
            raise ValueError('The DOCX mapping differs from its canonical source segments.')
    if layout.partition_json is not None:
        partition = layout.source_partition
        selection = layout.selections[0]
        anchor = selection.document.source_anchor
        if (selection.topic != layout.topic or anchor['workspace'] != layout.workspace
                or anchor['edition'] != layout.edition or anchor['revision'] != layout.revision
                or anchor['asset_sha256'] != layout.asset_sha256):
            raise ValueError('The combined DOCX source anchor changed.')
        partition_ranges(selection.document.source_text, partition)
        return desk, base
    rows = [row for row in mapping['records'] if row['selected']]
    if len(rows) > MAX_DOCUMENTS or len(rows) != len(layout.selections):
        raise ValueError('The DOCX source selection is incomplete.')
    for row, selection in zip(rows, layout.selections):
        if (not isinstance(selection, SelectedDocument) or selection.topic != layout.topic
                or selection.document.source_text != row['text']
                or selection.document.text != row['text']):
            raise ValueError('A formatted translation source was edited.')
        anchor = selection.document.source_anchor
        if (anchor is None or anchor['workspace'] != layout.workspace
                or anchor['edition'] != layout.edition or anchor['revision'] != layout.revision
                or anchor['asset_sha256'] != layout.asset_sha256
                or len(anchor['positions']) != 1
                or anchor['positions'][0]['ordinal'] != row['ordinal']
                or anchor['positions'][0]['text_sha256'] != row['sha256']):
            raise ValueError('The DOCX paragraph source anchor changed.')
    return desk, base


def _source_cancel(desk, base):
    from .translation import TranslationService
    def stopped():
        if base():
            return True
        # Only the lightweight current scope/revision check is polled. Full
        # retained-byte authentication occurs at snapshot/publication boundaries.
        TranslationService._source_current(desk, cancelled=base)
        return False
    return stopped


def _snapshot(library, desk, cancelled):
    check_cancelled(cancelled)
    path = library.assets.resolve(desk.identity.asset_sha256, cancelled=cancelled)
    asset = source_snapshot(path.parent, path.name, cancelled=cancelled)
    if (not isinstance(asset, AssetSnapshot) or asset.mime != _DOCX
            or asset.digest != desk.identity.asset_sha256 or not 0 < asset.size <= MAX_ARCHIVE):
        raise ValueError('Choose the retained ordinary DOCX original for this source revision.')
    check_cancelled(cancelled)
    return asset


def _command(request, digest, timeout):
    from . import translation_layout_worker
    package_root = Path(__file__).resolve().parents[2]
    roots = [str(package_root)]
    if (package_root.parent / 'bootstrap/vendor/cairn_extractors/hasher.py').is_file():
        roots.append(str(package_root.parent))
    bootstrap = (
        "import json,runpy,sys; roots=json.loads(sys.argv.pop(1)); "
        "sys.path[:0]=roots; runpy.run_module("
        "'carr_library.assistant.translation_layout_worker',run_name='__main__')")
    executable = sys.executable
    environment = {key: value for key, value in os.environ.items()
                   if key.upper() not in ('PYTHONEXECUTABLE', '__PYVENV_LAUNCHER__')}
    environment.update(PYTHONDONTWRITEBYTECODE='1', PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
    if (os.name == 'nt' and sys.prefix != sys.base_prefix
            and getattr(sys, '_base_executable', None)
            and os.path.normcase(sys._base_executable) != os.path.normcase(executable)):
        environment['__PYVENV_LAUNCHER__'] = executable
        executable = sys._base_executable
    return [str(Path(executable).absolute()), '-I', '-B', '-X', 'utf8', '-c', bootstrap,
            json.dumps(roots), str(request), digest,
            str(Path(translation_layout_worker.__file__).resolve()), str(timeout)], environment


def _invoke(library, desk, *, action, replacements=None, cancelled=lambda: False,
            broker=None, timeout=120):
    if type(timeout) not in (int, float) or not 1 <= timeout <= 120:
        raise ValueError('Choose a formatted-document deadline between 1 and 120 seconds.')
    check_cancelled(cancelled)
    broker = broker or ResourceBroker.for_app(library.root, cancelled=cancelled)
    runtime = local_path(library.root, exists=True) / 'runtime'
    runtime.mkdir(mode=0o700, exist_ok=True)
    runtime = local_path(runtime, exists=True)
    regular(runtime, directory=True)
    request = ResourceRequest(cpu_slots=1, memory_bytes=3 * 1024**3,
                              disk_bytes=256 * 1024**2, disk_path=runtime)
    directory = cleanup = None
    try:
        with broker.reserve(request, operation_id='translation.layout.' + uuid4().hex,
                            cancelled=cancelled, run_timeout=timeout) as lease:
            def stopped():
                return bool(cancelled()) or lease.cancelled()
            lease.checkpoint()
            check_cancelled(stopped)
            asset = _snapshot(library, desk, stopped)
            directory = Path(tempfile.mkdtemp(prefix='.docx-layout-', dir=runtime))
            info = regular(directory, directory=True)
            expected = (info.st_dev, info.st_ino)
            def remove():
                if not os.path.lexists(directory):
                    return
                current = regular(local_path(directory, exists=True), directory=True)
                if (current.st_dev, current.st_ino) != expected:
                    raise RuntimeError('The private formatting directory changed; cleanup requires review.')
                shutil.rmtree(directory)
            cleanup = remove
            copy_snapshot(asset, directory / 'source.docx', cancelled=stopped)
            payload = {'schema': 'carr-docx-layout-request/1', 'action': action,
                       'source_sha256': asset.digest, 'replacements': replacements}
            raw = encode(payload)
            if len(raw) > MAX_REQUEST:
                raise ValueError('The formatting request exceeds its bounded IPC budget.')
            request_path = directory / 'request.json'
            atomic_json(request_path, payload)
            args, environment = _command(request_path, _hash(raw), timeout)
            try:
                result = run_process(args, env=environment, cwd=directory,
                    budget=AudioBudget(timeout=timeout, max_output_bytes=256 * 1024**2,
                        max_log_bytes=64 * 1024, reserve_disk_bytes=0, threads=1),
                    cancelled=stopped, watch=(directory,), owned_windows_job=os.name == 'nt',
                    allow_failure=True)
            except AudioCleanupPending as error:
                error.cleanup_owner.add_quiescent_callback(cleanup)
                cleanup = None
                raise
            check_cancelled(stopped)
            if result.returncode:
                try:
                    diagnostic = json.loads(result.stdout.decode('utf-8', 'strict'))
                    message = diagnostic['message']
                    if type(message) is not str or len(message) > 1024:
                        raise ValueError
                except (UnicodeError, ValueError, KeyError, TypeError):
                    message = 'The DOCX format worker refused the source or could not complete safely.'
                raise ValueError(message)
            if json.loads(result.stdout.decode('utf-8', 'strict')) != {
                    'schema': 'carr-docx-layout-worker/1', 'status': 'complete'}:
                raise ValueError('The DOCX worker returned an invalid completion record.')
            response = read_json(directory / 'response.json', maximum=MAX_REPLY)
            _snapshot(library, desk, stopped)
            if action == 'describe':
                value = _mapping(response)
                if value['source_sha256'] != asset.digest or value['source_size'] != asset.size:
                    raise ValueError('The DOCX worker described another source.')
                return value
            output = source_snapshot(directory, 'output.docx', cancelled=stopped)
            if (not isinstance(output, AssetSnapshot) or output.mime != _DOCX
                    or type(response) is not dict
                    or set(response) != {'schema', 'source_sha256', 'output_sha256', 'output_bytes',
                        'changed_members', 'preserved_members', 'paragraphs', 'quotation_authority', 'notice', 'mapping_sha256'}
                    or response['schema'] != 'carr-docx-layout-output/1'
                    or response['source_sha256'] != asset.digest or response['output_sha256'] != output.digest
                    or type(response['mapping_sha256']) is not str or len(response['mapping_sha256']) != 64
                    or any(c not in '0123456789abcdef' for c in response['mapping_sha256'])
                    or response['output_bytes'] != output.size or type(response['output_bytes']) is not int
                    or response['changed_members'] != ['word/document.xml']
                    or response['quotation_authority'] is not False or response['notice'] != NOTICE
                    or type(response['paragraphs']) is not int or response['paragraphs'] != len(replacements)
                    or type(response['preserved_members']) is not dict
                    or len(response['preserved_members']) > 10000
                    or any(type(name) is not str or type(value) is not str or len(value) != 64
                           or any(c not in '0123456789abcdef' for c in value)
                           for name, value in response['preserved_members'].items())):
                raise ValueError('The completed DOCX output differs from its worker receipt.')
            return output.data, response
    finally:
        if cleanup is not None:
            cleanup()


def capture_layout(library, workspace, edition, *, topic=None, cancelled=lambda: False,
                   guard=lambda: None, broker=None):
    guard()
    expected = _root_identity(library.root)
    base = _bound_cancel(library, expected, cancelled, guard)
    check_cancelled(base)
    desk = ReadingDesk(library, workspace, edition, topic=topic, cancelled=base)
    stopped = _source_cancel(desk, base)
    mapping = _invoke(library, desk, action='describe', cancelled=stopped, broker=broker)
    if len(mapping['records']) != len(desk.pages):
        raise ValueError('The DOCX body differs from its retained source inventory.')
    selected = []
    for row, page in zip(mapping['records'], desk.pages):
        check_cancelled(stopped)
        if page.kind != 'logical_segment' or page.ordinal != row['ordinal'] or page.text_sha256 != row['sha256']:
            raise ValueError('The DOCX paragraph differs from its retained source segment.')
        if row['selected'] and mapping['selected'] <= MAX_DOCUMENTS:
            source = read_source_range(desk, page.ordinal, page.ordinal,
                                       cancelled=base, guard=guard)
            if source.text != row['text']:
                raise ValueError('The DOCX text differs from its current retained source.')
            selected.append(SelectedDocument(source.reading_document(), topic))
    partition_json = None
    if mapping['selected'] > MAX_DOCUMENTS:
        source = read_source_range(desk, 1, len(desk.pages), cancelled=base, guard=guard,
                                   max_characters=MAX_CHARACTERS)
        selected = [SelectedDocument(source.reading_document(), topic)]
        partition_json = canonical(_combined_partition(mapping, selected[0],
            _hash(canonical(mapping).encode('utf-8'))))
    check_cancelled(stopped)
    return LayoutDocument(expected[0], expected[1], workspace, edition, desk.revision,
        desk.identity.asset_sha256, desk.title, topic, canonical(mapping), tuple(selected), partition_json)


def review_layout(service, profile, layout, source, target, budget, *, glossary=None,
                  rate_per_million=None, cancelled=lambda: False, guard=lambda: None):
    desk, base = _checked_desk(service.library, layout, cancelled=cancelled, guard=guard)
    stopped = _source_cancel(desk, base)
    actual = _invoke(service.library, desk, action='describe', cancelled=stopped)
    if canonical(actual) != layout.mapping_json:
        raise ValueError('The DOCX mapping changed before translation review.')
    report = review_documents(service, profile, layout.selections, source, target, budget,
        glossary=glossary, rate_per_million=rate_per_million, cancelled=stopped, guard=guard,
        source_partitions=layout.source_partitions)
    payload = {'schema': 'carr-docx-translation-review/1', 'layout': layout.summary(),
               'documents': report, 'provider_calls_started': False}
    return {**payload, 'review_sha256': _hash(canonical(payload).encode('utf-8'))}


def prepare_layout(service, profile, layout, source, target, budget, *, approved_review,
                   glossary=None, rate_per_million=None, cancelled=lambda: False, guard=lambda: None):
    review = review_layout(service, profile, layout, source, target, budget,
        glossary=glossary, rate_per_million=rate_per_million, cancelled=cancelled, guard=guard)
    if approved_review != review['review_sha256']:
        raise PermissionError('Review and explicitly approve this exact DOCX mapping, limitations and budget.')
    desk, base = _checked_desk(service.library, layout, cancelled=cancelled, guard=guard)
    return prepare_documents(service, profile, layout.selections, source, target, budget,
        approved_review=review['documents']['review_sha256'], glossary=glossary,
        rate_per_million=rate_per_million, cancelled=_source_cancel(desk, base), guard=guard,
        source_partitions=layout.source_partitions)



def order_layout_jobs(service, layout, identities, *, cancelled=lambda: False, guard=lambda: None):
    """Order an explicit complete paragraph selection without sending any text.

    Selection is still user-owned: this never searches for or automatically
    chooses a substitute job, profile, language or approval.
    """
    desk, base = _checked_desk(service.library, layout, cancelled=cancelled, guard=guard)
    stopped = _source_cancel(desk, base)
    if (type(identities) not in (tuple, list) or len(identities) != len(layout.selections)
            or any(type(value) is not str for value in identities)
            or len(set(identities)) != len(identities)):
        raise ValueError('Explicitly choose one distinct saved job per selected DOCX paragraph.')
    if layout.partition_json is not None:
        _partition_job(service, layout, identities[0], cancelled=stopped, guard=guard)
        return tuple(identities)
    rows = {row['ordinal']: (row, selection) for row, selection in zip(
        (row for row in layout.mapping['records'] if row['selected']), layout.selections)}
    ordered, settings = {}, None
    for identity in identities:
        check_cancelled(stopped)
        job = service.get(identity)
        binding = service._binding(job)
        if binding is None or binding['topic'] != layout.topic:
            raise ValueError('A saved job belongs to a different source selection.')
        anchor = binding['anchor']
        if len(anchor['positions']) != 1:
            raise ValueError('Choose the individual DOCX paragraph jobs, not a combined text range.')
        position = anchor['positions'][0]
        ordinal = position['ordinal']
        if ordinal not in rows or ordinal in ordered:
            raise ValueError('The selected jobs repeat or omit a DOCX paragraph.')
        row, selection = rows[ordinal]
        expected = selection.document.source_anchor
        if (job['source_text'] != row['text']
                or any(anchor[key] != expected[key] for key in anchor if key != 'positions')
                or any(position[key] != expected['positions'][0][key]
                       for key in position if key != 'text_verified_at_capture')):
            raise ValueError('A saved job belongs to a different DOCX source paragraph.')
        service._source_context(job, cancelled=stopped, guard=guard)
        current = canonical({key: job.get(key) for key in
                             ('profile', 'source_language', 'target_language', 'glossary')})
        if settings is not None and settings != current:
            raise ValueError('Choose paragraph jobs with the same reviewed profile, languages and glossary.')
        settings = current
        ordered[ordinal] = identity
    return tuple(ordered[ordinal] for ordinal in rows)


def _completed(service, layout, identities, *, cancelled, guard):
    from .translation import _assemble_translation
    if (type(identities) not in (tuple, list) or len(identities) != len(layout.selections)
            or any(type(identity) is not str for identity in identities)
            or len(set(identities)) != len(identities)):
        raise ValueError('Select exactly one distinct completed job for each DOCX paragraph in order.')
    if layout.partition_json is not None:
        return _completed_partition(service, layout, identities[0], cancelled=cancelled, guard=guard)
    replacements, receipts, settings = [], [], None
    for row, selection, identity in zip(
            (row for row in layout.mapping['records'] if row['selected']), layout.selections, identities):
        check_cancelled(cancelled)
        guard()
        job = service.get(identity)
        if job.get('kind') != 'translation/2' or job['status'] != 'complete' or len(job['receipts']) != len(job['chunks']):
            raise ValueError('Finish the existing source-bound paragraph jobs before formatted export.')
        service._output_budget(job)
        service._text_binding(job)
        desk = service._source_context(job, cancelled=cancelled, guard=guard)
        anchor = job['source_binding']['anchor']
        expected = selection.document.source_anchor
        if (job['source_binding']['topic'] != layout.topic or job['source_text'] != row['text']
                or any(anchor[key] != expected[key] for key in anchor if key != 'positions')
                or len(anchor['positions']) != 1
                or any(anchor['positions'][0][key] != expected['positions'][0][key]
                       for key in anchor['positions'][0] if key != 'text_verified_at_capture')):
            raise ValueError('A completed translation belongs to a different DOCX source paragraph.')
        current_settings = canonical({key: job.get(key) for key in
                                      ('profile', 'source_language', 'target_language', 'glossary')})
        if settings is not None and current_settings != settings:
            raise ValueError('Choose paragraph jobs with the same reviewed profile, languages and glossary.')
        settings = current_settings
        value = _assemble_translation(job['chunks'], job['receipts'])
        replacements.append({'ordinal': row['ordinal'], 'source_sha256': row['sha256'], 'text': value})
        receipts.append({'id': identity, 'ordinal': row['ordinal'], 'source_sha256': row['sha256'],
                         'translation_sha256': _hash(value.encode('utf-8'))})
    return replacements, receipts


def _completed_partition(service, layout, identity, *, cancelled, guard):
    from .translation import _assemble_translation
    check_cancelled(cancelled)
    guard()
    job = _partition_job(service, layout, identity, cancelled=cancelled, guard=guard)
    if job['status'] != 'complete' or len(job['receipts']) != len(job['chunks']):
        raise ValueError('Finish every reviewed DOCX partition before formatted export.')
    service._output_budget(job)
    service._text_binding(job)
    groups = partition_chunk_groups(job['source_text'], job['source_partition'])
    ranges = partition_ranges(job['source_text'], job['source_partition'])
    translated, index = {}, 0
    for interval, group in zip(ranges, groups):
        rows = job['receipts'][index:index + len(group)]
        if len(rows) != len(group):
            raise ValueError('The completed layout receipt inventory is incomplete.')
        translated[interval] = (_assemble_translation(group, rows),
                                tuple(range(index, index + len(group))))
        index += len(group)
    if index != len(job['chunks']) or index != len(job['receipts']):
        raise ValueError('The completed layout receipt inventory changed.')
    replacements, receipts = [], []
    for row, position in zip(layout.mapping['records'],
                             layout.selections[0].document.source_anchor['positions']):
        if not row['selected']:
            continue
        key = (position['start'], position['end'])
        if key not in translated:
            raise ValueError('A source paragraph is not an exact approved layout partition.')
        value, indices = translated[key]
        replacements.append({'ordinal': row['ordinal'], 'source_sha256': row['sha256'], 'text': value})
        receipts.append({'id': identity, 'ordinal': row['ordinal'], 'source_sha256': row['sha256'],
                         'translation_sha256': _hash(value.encode('utf-8')),
                         'receipt_indices': list(indices)})
    return replacements, receipts


def export_layout(service, layout, identities, destination, *, approval=None,
                  cancelled=lambda: False, guard=lambda: None, broker=None):
    desk, base = _checked_desk(service.library, layout, cancelled=cancelled, guard=guard)
    stopped = _source_cancel(desk, base)
    requested = Path(destination)
    if requested.suffix.lower() != '.docx':
        raise ValueError('Choose a new DOCX output filename.')
    # Sorted existing job locks preserve the ordinary per-job mutation boundary.
    if (type(identities) not in (tuple, list) or not 1 <= len(identities) <= MAX_DOCUMENTS
            or any(type(identity) is not str for identity in identities)
            or len(set(identities)) != len(identities)):
        raise ValueError('Select distinct existing DOCX paragraph jobs.')
    with ExitStack() as locks:
        for identity in sorted(identities):
            locks.enter_context(service.jobs.lock(identity))
        replacements, receipts = _completed(service, layout, identities, cancelled=stopped, guard=guard)
        data, result = _invoke(service.library, desk, action='render', replacements=replacements,
                               cancelled=stopped, broker=broker)
        if result['mapping_sha256'] != layout.mapping_sha256:
            raise ValueError('The completed DOCX mapping differs from the captured source review.')
        with exclusive_writer(service.library.root):
            def publishing_cancelled():
                if stopped():
                    return True
                # Fresh immutable-byte admission is deliberately at the existing
                # exporter's final checkpoints, not a post-commit callback.
                _snapshot(service.library, desk, base)
                return stopped()
            check_cancelled(publishing_cancelled)
            exported = write_binary_export(requested, data, private_root=service.library.root,
                cancelled=publishing_cancelled, require_absent=True, approval=approval)
            # No source/cancellation observer runs after the actual publisher
            # returns its committed receipt.
            return {'status': 'complete', 'path': str(exported.path),
                'cleanup_warning': exported.cleanup_warning, 'format': 'docx',
                'source_sha256': layout.asset_sha256, 'source_revision': layout.revision,
                'mapping_sha256': layout.mapping_sha256, 'jobs': receipts,
                'output_sha256': result['output_sha256'], 'output_bytes': len(data),
                'changed_members': result['changed_members'],
                'preserved_member_count': len(result['preserved_members']),
                'quotation_authority': False, 'notice': NOTICE}

"""Reviewed DOCX/EPUB derivatives through the existing translation service.

The original source binding, explicit provider approval and accepted usage
receipts remain authoritative. Layout mappings grant no transfer authority.
Fixed offline workers reuse the canonical extractors and only replace approved
text blocks. No document parser, provider, store or process supervisor is added.
"""
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sys
import tempfile
from time import monotonic
from uuid import uuid4

from .models import canonical
from .library_translation import (
    LibrarySelection, capture_library_sources, validate_source_binding,
    verify_library_sources)
from .translation import translation_approval, _assemble_translation
from .translation_partition import make_partition, partition_ranges, partition_chunk_groups
from .translation_layout_worker import (
    MAX_ARCHIVE, MAX_CHARACTERS, MAX_REPLY, MAX_REQUEST, MAX_UNITS,
    PROFILE as DOCX_PROFILE, NOTICE as DOCX_NOTICE)
from .translation_epub_layout_worker import PROFILE as EPUB_PROFILE, NOTICE as EPUB_NOTICE
from ..audio.cleanup import retained_context
from ..audio.models import AudioBudget
from ..audio.process import run_process
from ..catalog.intake import AssetSnapshot, snapshot
from ..durable_state import atomic_json, encode, local_path, read_json_snapshot, regular
from ..ingest.writer_guard import exclusive_writer
from ..operations.broker import ResourceBroker
from ..operations.models import ResourceRequest
from ..reader.reading_desk import ReadingDesk
from ..research.export_file import write_binary_export
from ..speech._io import check_cancelled

_FORMATS = {
    'docx': (DOCX_PROFILE, 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', DOCX_NOTICE),
    'epub': (EPUB_PROFILE, 'application/epub+zip', EPUB_NOTICE),
}


def _hash(raw):
    return sha256(raw).hexdigest()


def _format(kind):
    if type(kind) is not str or kind not in _FORMATS:
        raise ValueError('Choose the supported DOCX or EPUB structure profile.')
    return _FORMATS[kind]


def _root_identity(root):
    path = local_path(root, exists=True)
    info = regular(path, directory=True)
    return str(path), (info.st_dev, info.st_ino)


def _bound_cancel(library, expected, cancelled, guard):
    def stopped():
        guard()
        if _root_identity(library.root) != expected:
            raise ValueError('The selected library changed. Reopen the formatted translation.')
        return bool(cancelled())
    return stopped


def _docx_mapping(value):
    PROFILE, NOTICE = DOCX_PROFILE, DOCX_NOTICE
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


def _member(name):
    return (type(name) is str and 0 < len(name) <= 4096 and not name.startswith('/')
            and '\\' not in name and ':' not in name and str(PurePosixPath(name)) == name
            and not any(part in ('.', '..') for part in PurePosixPath(name).parts)
            and not any(ord(c) < 32 or ord(c) == 127 for c in name))


def _digest(value):
    return (type(value) is str and len(value) == 64
            and all(c in '0123456789abcdef' for c in value))


def _epub_mapping(value):
    PROFILE, NOTICE = EPUB_PROFILE, EPUB_NOTICE
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


def _mapping(value, kind):
    _format(kind)
    return _docx_mapping(value) if kind == 'docx' else _epub_mapping(value)


def _units(mapping, binding, original, kind):
    sources = binding['sources']
    if len(sources) != 1 or sources[0]['selection'] != 'document':
        raise ValueError('Structure preservation needs one complete retained document.')
    anchor = sources[0]['anchor']
    records = mapping['records']
    if (mapping['source_sha256'] != anchor['asset_sha256']
            or len(anchor['positions']) != len(records)
            or original != '\n\n'.join(row['text'] for row in records)):
        raise ValueError('The layout differs from the complete unchanged source text.')
    result = []
    for row, position in zip(records, anchor['positions']):
        first, last = position['start'], position['end']
        if (position['ordinal'] != row['ordinal']
                or position['text_sha256'] != row['sha256']
                or original[first:last] != row['text']):
            raise ValueError('The layout source positions changed.')
        selected = ([{'ordinal': row['ordinal'], 'start': 0, 'end': len(row['text']),
                      'sha256': row['sha256'], 'preformatted': False}]
                    if kind == 'docx' and row['selected'] else
                    [] if kind == 'docx' else row['units'])
        for unit in selected:
            start, end = first + unit['start'], first + unit['end']
            if _hash(original[start:end].encode('utf-8')) != unit['sha256']:
                raise ValueError('A layout block differs from its original characters.')
            result.append({'ordinal': unit['ordinal'], 'start': start, 'end': end,
                           'sha256': unit['sha256'], 'preformatted': unit['preformatted']})
    return tuple(result)


@dataclass(frozen=True)
class LayoutDocument:
    """Immutable in-memory capture; all operations reacquire current authority."""
    library_root: str
    root_identity: tuple
    kind: str
    source_text: str
    binding_json: str
    mapping_json: str

    @property
    def binding(self):
        value = validate_source_binding(json.loads(self.binding_json), self.source_text)
        if canonical(value) != self.binding_json:
            raise ValueError('The captured source binding is not canonical.')
        return value

    @property
    def mapping(self):
        value = _mapping(json.loads(self.mapping_json), self.kind)
        if canonical(value) != self.mapping_json:
            raise ValueError('The captured layout mapping is not canonical.')
        return value

    @property
    def mapping_sha256(self):
        return _hash(self.mapping_json.encode('utf-8'))

    @property
    def source_partition(self):
        boundaries, previous = [], 0
        for unit in _units(self.mapping, self.binding, self.source_text, self.kind):
            for last in (unit['start'], unit['end']):
                if last > previous:
                    boundaries.append(last)
                    previous = last
        if previous < len(self.source_text):
            boundaries.append(len(self.source_text))
        return make_partition(self.source_text, _format(self.kind)[0],
                              self.mapping_sha256, boundaries)

    def summary(self):
        binding, mapping = self.binding, self.mapping
        source = binding['sources'][0]
        self.source_partition
        return {'format': self.kind, 'profile': mapping['schema'], 'title': source['title'],
                'source_sha256': mapping['source_sha256'],
                'source_revision': source['anchor']['revision'],
                'mapping_sha256': self.mapping_sha256, 'text_blocks': mapping['selected'],
                'characters': len(self.source_text), 'notice': mapping['notice'],
                'quotation_authority': False}


@contextmanager
def _stage(runtime):
    directory = Path(tempfile.mkdtemp(prefix='.translation-layout-', dir=runtime))
    info = regular(directory, directory=True)
    identity = (info.st_dev, info.st_ino)
    try:
        yield directory
    finally:
        if os.path.lexists(directory):
            current = regular(local_path(directory, exists=True), directory=True)
            if (current.st_dev, current.st_ino) != identity:
                raise RuntimeError('The private formatting directory changed; cleanup requires review.')
            shutil.rmtree(directory)


@contextmanager
def _operation(library, *, cancelled, guard, broker=None):
    if library is None:
        raise ValueError('Open the retained source library before formatted translation.')
    expected = _root_identity(library.root)
    base = _bound_cancel(library, expected, cancelled, guard)
    check_cancelled(base)
    runtime = local_path(library.root, exists=True) / 'runtime'
    runtime.mkdir(mode=0o700, exist_ok=True)
    regular(local_path(runtime, exists=True), directory=True)
    check_cancelled(base)
    broker = broker if broker is not None else ResourceBroker.for_app(library.root)
    request = ResourceRequest(cpu_slots=1, memory_bytes=3 * 1024**3,
                              disk_bytes=256 * 1024**2, disk_path=runtime)
    # One admitted lease and the exact stage survive every causal pending owner.
    with retained_context(ExitStack()) as stack:
        lease = stack.enter_context(broker.reserve(
            request, operation_id='translation.layout.' + uuid4().hex,
            cancelled=base, run_timeout=120))
        deadline = monotonic() + 120
        def stopped():
            lease.checkpoint()
            return base()
        check_cancelled(stopped)
        directory = stack.enter_context(_stage(runtime))
        yield directory, stopped, deadline


def _checked_desk(library, layout, *, cancelled, guard):
    if not isinstance(layout, LayoutDocument):
        raise ValueError('Capture an explicitly selected DOCX or EPUB first.')
    if _root_identity(library.root) != (layout.library_root, layout.root_identity):
        raise ValueError('The selected library directory changed.')
    binding, mapping = layout.binding, layout.mapping
    layout.source_partition
    verify_library_sources(library, binding, layout.source_text,
                           cancelled=cancelled, guard=guard)
    source = binding['sources'][0]
    anchor = source['anchor']
    desk = ReadingDesk(library, binding['workspace'], anchor['edition'],
                       topic=binding['topic'], revision=anchor['revision'],
                       cancelled=cancelled)
    if (desk.identity.asset_sha256 != mapping['source_sha256']
            or len(mapping['records']) != len(desk.pages)):
        raise ValueError('The original or its canonical structure changed.')
    for row, page in zip(mapping['records'], desk.pages):
        check_cancelled(cancelled)
        if (page.kind != 'logical_segment' or page.ordinal != row['ordinal']
                or page.text_sha256 != row['sha256']):
            raise ValueError('The format mapping differs from the canonical source inventory.')
    return desk


def _snapshot(library, desk, kind, cancelled):
    check_cancelled(cancelled)
    path = library.assets.resolve(desk.identity.asset_sha256, cancelled=cancelled)
    value = snapshot(path.parent, path.name)
    if (not isinstance(value, AssetSnapshot) or value.mime != _format(kind)[1]
            or value.digest != desk.identity.asset_sha256
            or not 0 < value.size <= MAX_ARCHIVE):
        raise ValueError('Choose the retained original in the selected DOCX or EPUB format.')
    check_cancelled(cancelled)
    return value


def _copy_snapshot(value, destination, cancelled):
    with destination.open('xb') as stream:
        for start in range(0, value.size, 1024 * 1024):
            check_cancelled(cancelled)
            stream.write(value.data[start:start + 1024 * 1024])
        stream.flush()
        os.fsync(stream.fileno())
    check_cancelled(cancelled)


def _command(request, checksum, timeout, kind):
    if kind == 'docx':
        from . import translation_layout_worker as worker
    else:
        from . import translation_epub_layout_worker as worker
    package_root = Path(__file__).resolve().parents[2]
    roots = [str(package_root)]
    if (package_root.parent / 'bootstrap/vendor/cairn_extractors/hasher.py').is_file():
        roots.append(str(package_root.parent))
    bootstrap = (
        "import json,runpy,sys; roots=json.loads(sys.argv.pop(1)); "
        "sys.path[:0]=roots; runpy.run_module("
        + repr(worker.__name__) + ",run_name='__main__')")
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
            json.dumps(roots), str(request), checksum,
            str(Path(worker.__file__).resolve()), str(timeout)], environment


def _invoke(library, desk, directory, kind, *, action, cancelled, deadline,
            replacements=None, target_language=None):
    _format(kind)
    check_cancelled(cancelled)
    original = _snapshot(library, desk, kind, cancelled)
    _copy_snapshot(original, directory / ('source.' + kind), cancelled)
    payload = {'schema': 'carr-' + kind + '-layout-request/1', 'action': action,
               'source_sha256': original.digest, 'replacements': replacements}
    if kind == 'epub':
        payload['target_language'] = target_language
    raw = encode(payload)
    if len(raw) > MAX_REQUEST:
        raise ValueError('The formatting request exceeds its bounded IPC budget.')
    request_path = directory / 'request.json'
    atomic_json(request_path, payload)
    timeout = min(120, deadline - monotonic())
    if timeout < 1:
        raise TimeoutError('The admitted formatted-document deadline expired before worker launch.')
    args, environment = _command(request_path, _hash(raw), timeout, kind)
    result = run_process(args, env=environment, cwd=directory,
        budget=AudioBudget(timeout=timeout, max_output_bytes=256 * 1024**2,
            max_log_bytes=64 * 1024, reserve_disk_bytes=0, threads=1),
        cancelled=cancelled, watch=(directory,), owned_tree=True, allow_failure=True)
    check_cancelled(cancelled)
    if result.returncode:
        try:
            diagnostic = json.loads(result.stdout.decode('utf-8', 'strict'))
            message = diagnostic['message']
            if type(message) is not str or len(message) > 1024:
                raise ValueError
        except (UnicodeError, ValueError, KeyError, TypeError):
            message = 'The format worker refused the source or could not complete safely.'
        raise ValueError(message)
    if json.loads(result.stdout.decode('utf-8', 'strict')) != {
            'schema': 'carr-' + kind + '-layout-worker/1', 'status': 'complete'}:
        raise ValueError('The format worker returned an invalid completion record.')
    response, _ = read_json_snapshot(directory / 'response.json', max_bytes=MAX_REPLY)
    _snapshot(library, desk, kind, cancelled)
    if action == 'describe':
        value = _mapping(response, kind)
        if value['source_sha256'] != original.digest or value['source_size'] != original.size:
            raise ValueError('The format worker described another original.')
        return value
    output = snapshot(directory, 'output.' + kind)
    if (output.mime != _format(kind)[1] or type(response) is not dict
            or set(response) != {'schema', 'source_sha256', 'output_sha256', 'output_bytes',
                'changed_members', 'preserved_members', 'paragraphs', 'quotation_authority',
                'notice', 'mapping_sha256'}
            or response['schema'] != 'carr-' + kind + '-layout-output/1'
            or response['source_sha256'] != original.digest
            or response['output_sha256'] != output.digest
            or type(response['output_bytes']) is not int or response['output_bytes'] != output.size
            or not _digest(response['mapping_sha256'])
            or type(response['changed_members']) is not list
            or not 1 <= len(response['changed_members']) <= 1001
            or response['changed_members'] != sorted(set(response['changed_members']))
            or any(not _member(name) for name in response['changed_members'])
            or response['quotation_authority'] is not False
            or response['notice'] != _format(kind)[2]
            or type(response['paragraphs']) is not int or response['paragraphs'] != len(replacements)
            or type(response['preserved_members']) is not dict
            or len(response['preserved_members']) > 10000
            or any(not _member(name) or not _digest(value)
                   for name, value in response['preserved_members'].items())):
        raise ValueError('The completed format output differs from its worker receipt.')
    return output.data, response


def capture_layout(library, workspace, edition, *, kind, topic=None, collection=None,
                   cancelled=lambda: False, guard=lambda: None, broker=None):
    """Capture one whole current source; no credentials or provider is accessed."""
    _format(kind)
    expected = _root_identity(library.root)
    with _operation(library, cancelled=cancelled, guard=guard, broker=broker) as (directory, stopped, deadline):
        desk = ReadingDesk(library, workspace, edition, topic=topic, cancelled=stopped)
        mapping = _invoke(library, desk, directory, kind, action='describe',
                          cancelled=stopped, deadline=deadline)
        captured = capture_library_sources(library, workspace, (LibrarySelection(edition),),
            topic=topic, collection=collection, cancelled=stopped, guard=guard)
        layout = LayoutDocument(expected[0], expected[1], kind, captured.text,
                                canonical(captured.binding), canonical(mapping))
        _checked_desk(library, layout, cancelled=stopped, guard=guard)
        check_cancelled(stopped)
        return layout


def _review_payload(service, profile, layout, source, target, budget, *, glossary,
                    rate_per_million, cancelled):
    from .providers import money
    preview = service.preview(profile, layout.source_text, source, target,
        source_binding=layout.binding, source_partition=layout.source_partition,
        glossary=glossary, rate_per_million=rate_per_million, cancelled=cancelled)
    budget = str(money(budget))
    if preview['estimated_cost'] is not None and money(preview['estimated_cost']) > money(budget):
        raise ValueError('The entered price estimate exceeds the approved total budget.')
    return {'schema': 'carr-formatted-translation-review/1', 'layout': layout.summary(),
            'translation': preview, 'profile': profile.to_dict(), 'budget': budget,
            'rate_per_million': str(money(rate_per_million)) if rate_per_million is not None else None,
            'provider_calls_started': False}


def review_layout(service, profile, layout, source, target, budget, *, glossary=None,
                  rate_per_million=None, cancelled=lambda: False, guard=lambda: None, broker=None):
    with _operation(service.library, cancelled=cancelled, guard=guard, broker=broker) as (directory, stopped, deadline):
        desk = _checked_desk(service.library, layout, cancelled=stopped, guard=guard)
        current = _invoke(service.library, desk, directory, layout.kind, action='describe',
                          cancelled=stopped, deadline=deadline)
        if canonical(current) != layout.mapping_json:
            raise ValueError('The layout changed before translation review.')
        payload = _review_payload(service, profile, layout, source, target, budget,
            glossary=glossary, rate_per_million=rate_per_million, cancelled=stopped)
        check_cancelled(stopped)
        return {**payload, 'review_sha256': _hash(canonical(payload).encode('utf-8'))}


def prepare_layout(service, profile, layout, source, target, budget, *, approved_review,
                   title=None, glossary=None, rate_per_million=None,
                   cancelled=lambda: False, guard=lambda: None, broker=None):
    review = review_layout(service, profile, layout, source, target, budget,
        glossary=glossary, rate_per_million=rate_per_million,
        cancelled=cancelled, guard=guard, broker=broker)
    if approved_review != review['review_sha256']:
        raise PermissionError('Review and explicitly approve this exact layout, source and budget.')
    guard()
    check_cancelled(cancelled)
    partition, binding = layout.source_partition, layout.binding
    approval, cost = translation_approval(profile, layout.source_text, source, target, budget,
        source_binding=binding, source_partition=partition, glossary=glossary)
    return service.submit(profile, layout.source_text, source, target,
        title=title or binding['sources'][0]['title'], authorization=approval, per_call_cost=cost,
        source_binding=binding, source_partition=partition, glossary=glossary, cancelled=cancelled)


def _job_layout(service, job, layout, cancelled):
    if (job.get('source_binding') != layout.binding
            or job.get('source_partition') != layout.source_partition
            or job['source_text'] != layout.source_text):
        raise ValueError('The saved job differs from the reviewed original and layout partition.')
    service._check_source(job, cancelled=cancelled)
    if job['status'] != 'complete' or len(job['receipts']) != len(job['chunks']):
        raise ValueError('Finish every accepted translation block before formatted export.')


def _replacements(job, layout):
    groups = partition_chunk_groups(layout.source_text, layout.source_partition)
    ranges = partition_ranges(layout.source_text, layout.source_partition)
    by_span, index = {}, 0
    for span, chunks in zip(ranges, groups):
        stop = index + len(chunks)
        if tuple(job['chunks'][index:stop]) != chunks:
            raise ValueError('The accepted translation chunks differ from the original layout blocks.')
        by_span[span] = (chunks, job['receipts'][index:stop], tuple(range(index, stop)))
        index = stop
    if index != len(job['receipts']):
        raise ValueError('Unbound accepted receipts follow the layout partition.')
    replacements, receipts = [], []
    for unit in _units(layout.mapping, layout.binding, layout.source_text, layout.kind):
        chunks, accepted, indices = by_span[(unit['start'], unit['end'])]
        value = _assemble_translation(chunks, accepted)
        replacements.append({'ordinal': unit['ordinal'], 'source_sha256': unit['sha256'], 'text': value})
        rendered = value
        if layout.kind == 'epub':
            from .translation_epub_layout_worker import _canonical_text
            rendered = _canonical_text(value, unit['preformatted'])
        receipts.append({'ordinal': unit['ordinal'], 'source_sha256': unit['sha256'],
                         'translation_sha256': _hash(value.encode('utf-8')),
                         'rendered_text_sha256': _hash(rendered.encode('utf-8')),
                         'receipt_indices': list(indices)})
    return replacements, receipts


def export_layout(service, identity, destination, *, approval=None,
                  cancelled=lambda: False, guard=lambda: None, broker=None):
    """Publish one new format derivative from existing verified completed receipts."""
    with service.jobs.lock(identity):
        job = service._load(identity)
        partition = job.get('source_partition')
        if partition is None:
            raise ValueError('This saved job has no reviewed structure-preservation partition.')
        kind = next((name for name, value in _FORMATS.items() if value[0] == partition['profile']), None)
        _format(kind)
        binding = job['source_binding']
        if len(binding['sources']) != 1 or binding['sources'][0]['selection'] != 'document':
            raise ValueError('Formatted export needs one explicitly selected whole source.')
        source = binding['sources'][0]
        requested = Path(destination)
        if requested.suffix.lower() != '.' + kind:
            raise ValueError('Choose a new output filename in the reviewed ' + kind.upper() + ' format.')
        # Capture is a separate completed local operation, not nested admission.
        layout = capture_layout(service.library, binding['workspace'], source['anchor']['edition'],
            kind=kind, topic=binding['topic'], collection=binding['collection'],
            cancelled=cancelled, guard=guard, broker=broker)
        with _operation(service.library, cancelled=cancelled, guard=guard, broker=broker) as (directory, stopped, deadline):
            desk = _checked_desk(service.library, layout, cancelled=stopped, guard=guard)
            _job_layout(service, job, layout, stopped)
            replacements, receipts = _replacements(job, layout)
            data, result = _invoke(service.library, desk, directory, kind, action='render',
                replacements=replacements, target_language=job['target_language'] if kind == 'epub' else None,
                cancelled=stopped, deadline=deadline)
            expected_members = (['word/document.xml'] if kind == 'docx' else
                                sorted([layout.mapping['package']] +
                                       [row['locator']['href'] for row in layout.mapping['records']]))
            if (result['mapping_sha256'] != layout.mapping_sha256
                    or result['changed_members'] != expected_members):
                raise ValueError('The rendered format differs from the original reviewed mapping.')
            with exclusive_writer(service.library.root):
                def publishing_cancelled():
                    if stopped():
                        return True
                    _checked_desk(service.library, layout, cancelled=stopped, guard=guard)
                    _snapshot(service.library, desk, kind, stopped)
                    return stopped()
                check_cancelled(publishing_cancelled)
                exported = write_binary_export(requested, data, private_root=service.library.root,
                    cancelled=publishing_cancelled, require_absent=True, approval=approval)
                # No source/cancellation observer may turn committed success into failure.
                return {'status': 'complete', 'path': str(exported.path),
                    'cleanup_warning': exported.cleanup_warning, 'format': kind,
                    'job_id': identity, 'source_sha256': layout.mapping['source_sha256'],
                    'source_revision': source['anchor']['revision'],
                    'mapping_sha256': layout.mapping_sha256, 'blocks': receipts,
                    'output_sha256': result['output_sha256'], 'output_bytes': len(data),
                    'changed_members': result['changed_members'],
                    'preserved_member_count': len(result['preserved_members']),
                    'quotation_authority': False, 'notice': layout.mapping['notice']}

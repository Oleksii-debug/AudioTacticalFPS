"""Resumable document plans composed from the existing conversion and Studio services.

This is a typed document manifest, not another scheduler. The native host runs
one explicit operation on its existing executor. Existing JobStore identities
and locks, durable JSON, no-clobber exports and Studio jobs retain ownership.
"""
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import stat
from uuid import uuid4

from ..assistant.state import JobStore, now
from ..audio.models import AudioCancelled
from ..durable_state import atomic_bytes, atomic_json, encode, local_path, read_json, regular
from ..ingest.writer_guard import exclusive_writer
from ..research.export_file import ExportDestination, _destination, write_export
from ..speech._io import check_cancelled
from ..speech.reading import ReadingDocument
from ..state_backup import relative_name
from .models import ConversionBudget
from .planning import (SplitOptions, SplitPlan, TextBoundary, filename_key, plan_split,
                       reading_identity, studio_project, verify_plan)
from .service import BUILTIN, IMAGES, ConversionService

_KIND = 'document-batch/1'
_FORMATS = ('wav', 'flac', 'mp3', 'ogg', 'opus', 'm4a', 'm4b', 'aiff', 'wma')
_STATES = {'preparing', 'prepared', 'reviewed', 'running', 'paused', 'cancelled', 'failed', 'complete'}
_DIGEST = re.compile('[0-9a-f]{64}\\Z')
MAX_LIST_BYTES = 32 * 1024**2


def _studio_job(identity, index, plan, voice, language, format, destination):
    # An explicit new destination after restore is a new admission. Reuse the
    # original job only when its full reviewed output scope is unchanged.
    return sha256(encode([identity, index, plan.fingerprint, voice, language,
                          format, str(destination)])).hexdigest()[:32]


def _studio_project_id(identity, index, plan, voice, language):
    # A changed reviewed narration is a new derivative project, preserving the
    # earlier editable Studio project and its revision history.
    digest = sha256(encode([identity, index, plan.fingerprint, voice, language])).hexdigest()
    return 'batch-' + digest[:40]


@dataclass(frozen=True)
class BatchBudget:
    max_files: int = 256
    max_scan_entries: int = 20000
    max_source_bytes: int = 1024 * 1024 * 1024
    max_text_characters: int = 8_000_000

    def __post_init__(self):
        limits = {'max_files': 10000, 'max_scan_entries': 100000,
                  'max_source_bytes': 32 * 1024**3, 'max_text_characters': 16_000_000}
        if any(type(value) is not int or not 1 <= value <= limits[key] for key, value in vars(self).items()):
            raise ValueError('Choose positive bounded document-batch resource settings.')


@dataclass(frozen=True)
class BatchPreview:
    job_id: str
    source_fingerprint: str
    destination: str
    destination_identity: tuple[int, int]
    action: str
    format: str
    voice_id: str
    language: str
    combined: bool
    plans: tuple[dict, ...]
    outputs: tuple[str, ...]
    conflicts: tuple[str, ...]

    def to_dict(self):
        return json.loads(json.dumps(asdict(self), ensure_ascii=False, allow_nan=False))

    @property
    def fingerprint(self):
        return sha256(encode(self.to_dict())).hexdigest()

    @classmethod
    def from_dict(cls, value):
        if (type(value) is not dict or set(value) != set(cls.__dataclass_fields__)
                or not isinstance(value['job_id'], str) or not re.fullmatch('[0-9a-f]{32}', value['job_id'])
                or not isinstance(value['source_fingerprint'], str) or not _DIGEST.fullmatch(value['source_fingerprint'])
                or type(value['destination_identity']) is not list or len(value['destination_identity']) != 2
                or any(type(item) is not int or item < 0 for item in value['destination_identity'])
                or not isinstance(value['destination'], str) or not 1 <= len(value['destination']) <= 32767
                or type(value['combined']) is not bool or value['action'] not in ('text', 'studio')
                or value['format'] not in _FORMATS
                or not isinstance(value['voice_id'], str) or len(value['voice_id']) > 512
                or not isinstance(value['language'], str) or len(value['language']) > 80
                or type(value['plans']) is not list or not 1 <= len(value['plans']) <= 10000
                or type(value['outputs']) is not list or not 1 <= len(value['outputs']) <= 10000
                or type(value['conflicts']) is not list or len(value['conflicts']) > len(value['outputs'])):
            raise ValueError('Invalid saved document output preview.')
        for plan in value['plans']:
            SplitPlan.from_dict(plan)
        names = [filename_key(name) for name in value['outputs']]
        if len(names) != len(set(names)) or any(name not in value['outputs'] for name in value['conflicts']):
            raise ValueError('Invalid or colliding document output preview names.')
        return cls(**{**value, 'destination_identity': tuple(value['destination_identity']),
                      'plans': tuple(value['plans']), 'outputs': tuple(value['outputs']),
                      'conflicts': tuple(value['conflicts'])})


def _notify(progress, message):
    if progress is not None:
        try:
            progress(message)
        except Exception:
            pass  # observers cannot undo a committed receipt; cancellation is explicit


def _stamp(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _file(path, maximum, cancelled, *, expected=None, payload=False):
    path = local_path(path, exists=True)
    before = regular(path)
    if not 0 <= before.st_size <= maximum:
        raise ValueError('A document artifact exceeds the selected byte budget.')
    digest, count, chunks = sha256(), 0, []
    with path.open('rb') as stream:
        if _stamp(os.fstat(stream.fileno())) != _stamp(before):
            raise ValueError('Document artifact changed while opening it.')
        while block := stream.read(64 * 1024):
            check_cancelled(cancelled)
            count += len(block)
            if count > maximum:
                raise ValueError('A document artifact grew beyond its byte budget.')
            digest.update(block)
            if payload:
                chunks.append(block)
        if _stamp(os.fstat(stream.fileno())) != _stamp(before):
            raise ValueError('Document artifact changed while reading it.')
    if count != before.st_size or _stamp(regular(path)) != _stamp(before):
        raise ValueError('Document artifact changed during verification.')
    value = {'sha256': digest.hexdigest(), 'bytes': count}
    if expected is not None and value != expected:
        raise ValueError('A retained source or completed artifact changed; it was not silently reused.')
    return (value, b''.join(chunks)) if payload else value


def _selected(paths, budget, cancelled):
    """Finite explicit file/folder selection, with no links or hidden watcher."""
    selected, seen, skipped = [], set(), []
    entries = 0
    supported = set((*BUILTIN, '.rtf', *IMAGES))

    def visit(value, depth=0, explicit=True):
        nonlocal entries
        check_cancelled(cancelled)
        entries += 1
        if entries > budget.max_scan_entries or depth > 64:
            raise ValueError('The selected folder scan exceeds its entry/depth budget.')
        path = Path(value).absolute()
        info = path.lstat()
        is_directory = stat.S_ISDIR(info.st_mode)
        regular(path, directory=is_directory)  # classify without following links
        path = local_path(path, exists=True)
        key = os.path.normcase(str(path))
        if key in seen:
            return
        seen.add(key)
        if is_directory:
            before = _stamp(info)
            children = []
            with os.scandir(path) as scan:
                for child in scan:
                    check_cancelled(cancelled)
                    if len(children) + entries > budget.max_scan_entries:
                        raise ValueError('The selected folder exceeds its scan-entry budget.')
                    children.append(child.name)
            if _stamp(regular(path, directory=True)) != before:
                raise ValueError('The selected folder changed during its scan.')
            for name in sorted(children, key=lambda value: (value.casefold(), value)):
                visit(path / name, depth + 1, False)
            # A directory can acquire different entries without exposing new
            # native timestamps immediately. Recheck the finite reviewed name
            # set after traversing children, as the audio input selector does.
            remaining = set(children)
            with os.scandir(path) as scan:
                for child in scan:
                    check_cancelled(cancelled)
                    if child.name not in remaining:
                        raise ValueError('The selected folder changed during its scan.')
                    remaining.remove(child.name)
            check_cancelled(cancelled)
            local_path(path, exists=True)
            if remaining or _stamp(regular(path, directory=True)) != before:
                raise ValueError('The selected folder changed during its scan.')
        elif path.suffix.casefold() in supported:
            if len(selected) >= budget.max_files:
                raise ValueError('The selection exceeds the explicit document count budget.')
            selected.append(path)
        elif explicit:
            raise ValueError('The selected document format is not enabled by the existing converter.')
        elif len(skipped) < 64:
            skipped.append(path.name)

    if isinstance(paths, (str, bytes, os.PathLike)):
        raise ValueError('Choose an explicit sequence of documents or folders.')
    for count, path in enumerate(paths, 1):
        if count > budget.max_files:
            raise ValueError('Too many explicitly selected input roots.')
        visit(path)
    if not selected:
        raise ValueError('No supported documents were found in the explicit selection.')
    return tuple(selected), tuple(skipped)


class DocumentBatches:
    def __init__(self, library_root, *, budget=None, conversion_budget=None,
                 conversion_factory=ConversionService, production=None):
        self.library = local_path(library_root, exists=True)
        info = regular(self.library, directory=True)
        self.library_identity = info.st_dev, info.st_ino
        self.root = local_path(self.library / 'state' / 'conversion')
        self.store = JobStore(self.root / 'batches')
        self.budget = budget or BatchBudget()
        self.conversion_budget = conversion_budget or ConversionBudget()
        if not isinstance(self.budget, BatchBudget) or not isinstance(self.conversion_budget, ConversionBudget):
            raise ValueError('Typed conversion and batch budgets are required.')
        self.conversion_factory, self.production = conversion_factory, production

    def _scope(self):
        path = local_path(self.library, exists=True)
        info = regular(path, directory=True)
        if path != self.library or (info.st_dev, info.st_ino) != self.library_identity:
            raise ValueError('The selected library folder changed. Reopen document conversion.')

    def _save(self, record):
        self._scope()
        record['updated'] = now()
        # Reuse canonical JobStore identity/lock/discovery with the common
        # Windows snapshot reader/writer, avoiding ordinary open-target rename.
        atomic_json(self.store.path(record['id']), record, replace=True)

    def _load(self, identity):
        self._scope()
        value = read_json(self.store.path(identity))
        if (type(value) is not dict or value.get('schema') != 'carr-assistant-job/1'
                or value.get('kind') != _KIND or value.get('id') != identity
                or value.get('status') not in _STATES or type(value.get('sources')) is not list
                or not 1 <= len(value['sources']) <= self.budget.max_files
                or type(value.get('outputs')) is not list or len(value['outputs']) > 10000):
            raise ValueError('Invalid document-batch checkpoint.')
        if (type(value.get('ocr_languages')) is not list or len(value['ocr_languages']) > 16
                or any(not isinstance(x, str) or not re.fullmatch(r'(?:script/)?[A-Za-z][A-Za-z0-9_]{0,79}', x)
                       for x in value['ocr_languages'])):
            raise ValueError('Invalid saved OCR choices.')
        for index, row in enumerate(value['sources'], 1):
            if (type(row) is not dict or row.get('id') != f'{index:04d}'
                    or not isinstance(row.get('name'), str) or len(row['name']) > 512
                    or not _DIGEST.fullmatch(str(row.get('sha256', '')))
                    or type(row.get('bytes')) is not int or not 0 < row['bytes'] <= self.conversion_budget.max_source_bytes
                    or row.get('kind') not in ('document', 'reading-copy')):
                raise ValueError('Invalid retained document identity.')
            self._managed(value, row['path'], exists=True)
        return value

    def _managed(self, record, relative, *, exists=False):
        path = relative_name(relative)
        if path.parts[0] != record['id'] or len(path.parts) < 3:
            raise ValueError('A document checkpoint escaped its managed job directory.')
        return local_path(self.store.directory.joinpath(*path.parts), root=self.store.directory, exists=exists)

    def _control(self, identity, command=None):
        path = self.store.path(identity).with_suffix('.control.json')
        if command is not None:
            if command not in ('run', 'pause', 'cancel'):
                raise ValueError('Invalid explicit document-batch control.')
            atomic_json(path, {'command': command}, replace=True)
        if not path.exists():
            return 'run'
        value = read_json(path)
        if type(value) is not dict or set(value) != {'command'} or value['command'] not in ('run', 'pause', 'cancel'):
            raise ValueError('Invalid document control checkpoint.')
        return value['command']

    def pause(self, identity):
        self._load(identity)
        self._control(identity, 'pause')

    def cancel(self, identity):
        self._load(identity)
        self._control(identity, 'cancel')

    def get(self, identity):
        record = self._load(identity)
        if record['library_root'] != str(self.library) and record.get('review') is not None:
            record['status'] = 'waiting-input'
            record['message'] = 'Restored job: explicitly preview and approve a new output directory before running.'
        return record

    def jobs(self, *, cancelled=lambda: False):
        values, inspected = [], 0
        for identity in self.store.identities():
            check_cancelled(cancelled)
            if len(values) >= 1000:
                raise ValueError('Document job listing reached its 1,000-job budget.')
            inspected += regular(self.store.path(identity)).st_size
            if inspected > MAX_LIST_BYTES:
                raise ValueError('Document job listing reached its aggregate metadata byte budget.')
            row = self.get(identity)
            values.append({'id': identity, 'status': row['status'], 'files': len(row['sources']),
                           'outputs': len(row['outputs']), 'message': row['message']})
        return tuple(values)

    def create(self, paths, *, ocr_languages=(), cancelled=lambda: False):
        sources, skipped = _selected(paths, self.budget, cancelled)
        return self._capture([(path, 'document') for path in sources], ocr_languages, skipped, cancelled)

    def create_reading_copies(self, paths, *, cancelled=lambda: False):
        if isinstance(paths, (str, bytes, os.PathLike)):
            raise ValueError('Choose an explicit sequence of saved reading copies.')
        selected = []
        for path in paths:
            check_cancelled(cancelled)
            if len(selected) >= self.budget.max_files:
                raise ValueError('Too many selected reading copies.')
            path = local_path(path, exists=True)
            regular(path)
            ReadingDocument.load(path)  # explicit bounded data import, no converter execution
            selected.append((path, 'reading-copy'))
        if not selected:
            raise ValueError('Choose at least one saved reading copy.')
        return self._capture(selected, (), (), cancelled)

    def _capture(self, selected, languages, skipped, cancelled):
        if (not isinstance(languages, (tuple, list)) or len(languages) > 16
                or any(not isinstance(x, str) or not re.fullmatch(r'(?:script/)?[A-Za-z][A-Za-z0-9_]{0,79}', x)
                       for x in languages)):
            raise ValueError('Choose explicit installed OCR language identifiers.')
        self._scope()
        sizes = [regular(path).st_size for path, _ in selected]
        if (any(not 0 < size <= self.conversion_budget.max_source_bytes for size in sizes)
                or sum(sizes) > self.budget.max_source_bytes):
            raise ValueError('Selected source bytes exceed the document-batch budget.')
        if shutil.disk_usage(self.store.directory).free < sum(sizes) + 32 * 1024**2:
            raise ValueError('Not enough free disk to retain the explicit document snapshots.')
        identity = uuid4().hex
        pending = self.store.directory / ('.pending-batch-' + identity)
        destination = self.store.directory / identity
        rows = []
        with exclusive_writer(self.library):
            if len(self.store.identities()) >= 1000:
                raise ValueError('Document batches reached the 1,000-job storage budget.')
            pending.mkdir(mode=0o700)
            (pending / 'inputs').mkdir()
            try:
                for index, ((source, kind), size) in enumerate(zip(selected, sizes), 1):
                    check_cancelled(cancelled)
                    before = regular(source)
                    suffix = source.suffix.casefold() if kind == 'document' else '.reading.json'
                    name = f'{index:04d}' + suffix
                    target = pending / 'inputs' / name
                    digest, count = sha256(), 0
                    with source.open('rb') as read, target.open('xb') as write:
                        if _stamp(os.fstat(read.fileno())) != _stamp(before):
                            raise ValueError('A selected source changed while opening it.')
                        while block := read.read(64 * 1024):
                            check_cancelled(cancelled)
                            count += len(block)
                            if count > size:
                                raise ValueError('A selected source grew during capture.')
                            digest.update(block)
                            write.write(block)
                        write.flush()
                        os.fsync(write.fileno())
                        if _stamp(os.fstat(read.fileno())) != _stamp(before):
                            raise ValueError('A selected source changed during capture.')
                    expected = {'sha256': digest.hexdigest(), 'bytes': count}
                    if count != size or _stamp(regular(source)) != _stamp(before):
                        raise ValueError('A selected source changed during capture.')
                    _file(source, self.conversion_budget.max_source_bytes, cancelled, expected=expected)
                    rows.append({'id': f'{index:04d}', 'name': source.name, 'kind': kind,
                                 'path': f'{identity}/inputs/{name}', **expected,
                                 'prepared': None, 'attempt': None})
                record = {'schema': 'carr-assistant-job/1', 'kind': _KIND, 'id': identity,
                          'library_root': str(self.library), 'status': 'preparing', 'created': now(),
                          'sources': rows, 'ocr_languages': list(languages), 'skipped': list(skipped),
                          'review': None, 'review_sha256': None, 'outputs': [], 'history': [],
                          'message': 'Explicit source snapshots retained; prepare text before reviewing output.'}
                check_cancelled(cancelled)
                os.rename(pending, destination)
                self._save(record)
                return identity
            finally:
                if pending.exists():
                    shutil.rmtree(pending)

    def _verify_sources(self, record, cancelled):
        total = 0
        for row in record['sources']:
            check_cancelled(cancelled)
            total += row['bytes']
            if total > self.budget.max_source_bytes:
                raise ValueError('Retained source bytes exceed the batch budget.')
            _file(self._managed(record, row['path'], exists=True), self.conversion_budget.max_source_bytes,
                  cancelled, expected={key: row[key] for key in ('sha256', 'bytes')})

    def _record_failure(self, record, error):
        if record['status'] == 'complete':
            return  # an observer or later verification does not rewrite historical completion
        interrupted = isinstance(error, (InterruptedError, TimeoutError, AudioCancelled))
        record['status'] = ('cancelled' if self._control(record['id']) == 'cancel' else 'paused') if interrupted else 'failed'
        if interrupted:
            record['message'] = (f'Document batch {record["status"]}. Retained sources and '
                                 f'{len(record["outputs"])} completed item receipts remain available for explicit resume.')
        else:
            record['message'] = str(error)[:2000]
        self._save(record)
        if interrupted:
            # The shared desktop owner recognizes InterruptedError. An engine's
            # typed AudioCancelled must not be presented as an ordinary failure.
            raise InterruptedError(record['message']) from error

    def _document(self, record, row, cancelled):
        prepared = row['prepared']
        if type(prepared) is not dict or set(prepared) != {'reading', 'reading_sha256', 'artifacts'}:
            raise ValueError('Prepare every selected document before planning outputs.')
        if type(prepared['artifacts']) is not list or not 1 <= len(prepared['artifacts']) <= 3:
            raise ValueError('Invalid prepared document receipt graph.')
        names = set()
        for artifact in prepared['artifacts']:
            if type(artifact) is not dict or set(artifact) != {'path', 'sha256', 'bytes'}:
                raise ValueError('Invalid prepared artifact receipt.')
            if artifact['path'] in names:
                raise ValueError('Duplicate prepared artifact receipt.')
            names.add(artifact['path'])
            _file(self._managed(record, artifact['path'], exists=True), 32 * 1024**2, cancelled,
                  expected={key: artifact[key] for key in ('sha256', 'bytes')})
        if prepared['reading'] not in names:
            raise ValueError('The reading copy is missing from its prepared receipt graph.')
        path = self._managed(record, prepared['reading'], exists=True)
        document = ReadingDocument.from_dict(read_json(path))
        if reading_identity(document) != prepared['reading_sha256']:
            raise ValueError('The retained reading copy changed after preparation.')
        return document

    def prepare(self, identity, *, cancelled=lambda: False, progress=None):
        # The existing library writer boundary also excludes backup while
        # converter outputs and their completion receipts are being published.
        with exclusive_writer(self.library), self.store.lock(identity):
            record = self._load(identity)
            self._control(identity, 'run')
            stopped = lambda: bool(cancelled()) or self._control(identity) != 'run'
            try:
                self._verify_sources(record, stopped)
                total = 0
                for index, row in enumerate(record['sources'], 1):
                    check_cancelled(stopped)
                    if row['prepared'] is not None:
                        document = self._document(record, row, stopped)
                    else:
                        _notify(progress, f'Preparing document {index} of {len(record["sources"])}: {row["name"]}')
                        source = self._managed(record, row['path'], exists=True)
                        artifacts = []
                        if row['kind'] == 'reading-copy':
                            document = ReadingDocument.load(source)
                        else:
                            folder = self._managed(record, f'{identity}/derived/{row["id"]}')
                            folder.mkdir(parents=True, exist_ok=True)
                            converter = self.conversion_factory(folder, input_roots=(source,), budget=self.conversion_budget)
                            attempt = row['attempt']
                            target = folder / (attempt + '.txt') if isinstance(attempt, str) and re.fullmatch('[0-9a-f]{32}', attempt) else None
                            receipt = target.with_name(target.name + '.provenance.json') if target else None
                            if target is None or not target.exists() or not receipt.exists():
                                # An earlier uncommitted attempt is never called complete.
                                # New unique private names avoid overwriting an orphan.
                                # At most one incomplete, unreceipted private attempt
                                # is retained. Never delete a published conversion pair.
                                if target is not None:
                                    for orphan in (target, receipt):
                                        if orphan.exists():
                                            regular(orphan)
                                            orphan.unlink()
                                row['attempt'] = uuid4().hex
                                self._save(record)
                                target = folder / (row['attempt'] + '.txt')
                                result = converter.extract(source, target, cancelled=stopped,
                                                           ocr_languages=record['ocr_languages'])
                                target, receipt = result.path, result.report_path
                            report = read_json(receipt)
                            if (report.get('schema') != 'carr-reading-derivative/1'
                                    or report.get('source_sha256') != row['sha256']
                                    or report.get('source_size') != row['bytes']
                                    or report.get('verified_for_quotation') is not False):
                                raise ValueError('The prepared conversion receipt does not match its retained source.')
                            _, raw_text = _file(target, self.conversion_budget.max_text_bytes, stopped,
                                  expected={'sha256': report['text_sha256'], 'bytes': report['text_size']}, payload=True)
                            text = raw_text.decode('utf-8', errors='strict')
                            document = ReadingDocument(text, title=Path(row['name']).stem)
                            for artifact in (target, receipt):
                                artifacts.append({'path': artifact.relative_to(self.store.directory).as_posix(),
                                                  **_file(artifact, 32 * 1024**2, stopped)})
                        target = self._managed(record, f'{identity}/documents/{row["id"]}.json')
                        raw = encode(document.to_dict())
                        if target.exists():
                            _file(target, 32 * 1024**2, stopped,
                                  expected={'sha256': sha256(raw).hexdigest(), 'bytes': len(raw)})
                        else:
                            atomic_bytes(target, raw)
                        artifacts.append({'path': target.relative_to(self.store.directory).as_posix(),
                                          'sha256': sha256(raw).hexdigest(), 'bytes': len(raw)})
                        row['prepared'] = {'reading': target.relative_to(self.store.directory).as_posix(),
                                           'reading_sha256': reading_identity(document), 'artifacts': artifacts}
                        self._save(record)
                    total += len(document.text)
                    if total > self.budget.max_text_characters:
                        raise ValueError('Prepared text exceeds the batch character budget; choose fewer files.')
                self._verify_sources(record, stopped)
                record['status'] = 'prepared' if record['review'] is None else record['status']
                record['message'] = 'Text retained and verified. Review exact boundaries and names before exporting or rendering.'
                self._save(record)
                return self.get(identity)
            except BaseException as exc:
                self._record_failure(record, exc)
                raise

    def _documents(self, record, combined, cancelled):
        documents, total = [], 0
        for row in record['sources']:
            document = self._document(record, row, cancelled)
            total += len(document.text)
            if total > self.budget.max_text_characters:
                raise ValueError('Prepared text exceeds the explicit batch budget.')
            boundaries = []
            if row['kind'] == 'document':
                receipt = next((item for item in row['prepared']['artifacts']
                                if item['path'].endswith('.provenance.json')), None)
                if receipt is None:
                    raise ValueError('The converted document is missing its provenance receipt.')
                report = read_json(self._managed(record, receipt['path'], exists=True))
                for section in report.get('sections', ()):
                    if ('start' not in section or 'end' not in section):
                        continue  # older receipts have no exact boundary offsets
                    start, end = section['start'], section['end']
                    if (type(start) is not int or type(end) is not int or not 0 <= start <= end <= len(document.text)
                            or sha256(document.text[start:end].encode()).hexdigest() != section.get('text_sha256')):
                        raise ValueError('Converted section boundaries no longer match their exact text.')
                    if start < len(document.text):
                        boundaries.append(TextBoundary(start, section['label'], 'section'))
                        if section.get('physical_index') is not None:
                            boundaries.append(TextBoundary(start, section['label'], 'page'))
            documents.append((row['name'], document, tuple(boundaries)))
        if not combined:
            return tuple(documents)
        text, marks, offset = [], [], 0
        for name, document, _ in documents:
            if text:
                text.append('\n\n'); offset += 2
            marks.append(TextBoundary(offset, name, 'section'))
            text.append(document.text); offset += len(document.text)
        if offset > 2_000_000:
            raise ValueError('Combined text exceeds the existing reading-copy limit; keep separate documents.')
        return (('combined', ReadingDocument(''.join(text), title='Combined reading copy'), tuple(marks)),)

    @staticmethod
    def _source_fingerprint(record):
        return sha256(encode([{'sha256': row['sha256'], 'bytes': row['bytes'], 'name': row['name'],
                               'reading_sha256': row['prepared']['reading_sha256']} for row in record['sources']])).hexdigest()

    def preview(self, identity, destination, options=None, *, template='{index:04d}-{source}-{part:03d}',
                combined=False, action='text', voice_id='', language='', format='wav', cancelled=lambda: False):
        if type(combined) is not bool or action not in ('text', 'studio') or format not in _FORMATS:
            raise ValueError('Choose text export or an existing supported Studio format.')
        if (not isinstance(voice_id, str) or len(voice_id) > 512 or '\x00' in voice_id
                or not isinstance(language, str) or len(language) > 80 or '\x00' in language):
            raise ValueError('Choose bounded explicit voice and language identifiers.')
        record = self._load(identity)
        destination = local_path(destination, exists=True)
        regular(destination, directory=True)
        _destination(destination / 'split-preview.txt', self.library)
        self._verify_sources(record, cancelled)
        documents = self._documents(record, combined, cancelled)
        plans, outputs, conflicts, names, number = [], [], [], set(), 1
        from ..studio.render import _output_name
        for index, (name, document, boundaries) in enumerate(documents, 1):
            plan = plan_split(document, options, template=template, source_name=Path(name).stem,
                              boundaries=boundaries, first_index=number, document_index=index, cancelled=cancelled)
            number += len(plan.parts)
            if number - 1 > 10000:
                raise ValueError('The batch exceeds its 10,000-output preview budget.')
            plans.append(plan.to_dict())
            if action == 'text':
                selected = [part.name for part in plan.parts]
            else:
                project = studio_project(document, plan, project_id=_studio_project_id(identity, index, plan, voice_id, language),
                                         voice_id=voice_id, language=language)
                job_id = _studio_job(identity, index, plan, voice_id, language, format, destination)
                selected = [f'{_output_name(project.title)}-{job_id[:12]}']
            for output in selected:
                key = filename_key(output)
                if key in names:
                    raise ValueError('Two documents would use the same portable output name; include index or document.')
                names.add(key)
                outputs.append(output)
                sidecar = output + '.provenance.json' if action == 'text' else None
                if sidecar:
                    filename_key(sidecar)
                if os.path.lexists(destination / output) or (sidecar and os.path.lexists(destination / sidecar)):
                    conflicts.append(output)
        info = regular(destination, directory=True)
        return BatchPreview(identity, self._source_fingerprint(record), str(destination),
                            (info.st_dev, info.st_ino), action, format, voice_id, language,
                            combined, tuple(plans), tuple(outputs), tuple(conflicts))

    def approve(self, preview, *, cancelled=lambda: False):
        if not isinstance(preview, BatchPreview):
            raise ValueError('Review the exact output preview before approving it.')
        with exclusive_writer(self.library), self.store.lock(preview.job_id):
            record = self._load(preview.job_id)
            self._verify_sources(record, cancelled)
            current = self._preview_again(record, preview, cancelled)
            if current != preview or preview.conflicts:
                raise ValueError('The preview or destination changed, or names are occupied. Preview again.')
            if record.get('review_sha256') != preview.fingerprint:
                if record['review'] is not None:
                    if len(record['history']) >= 8:
                        raise ValueError('This job reached its explicit re-review history budget; create a new job.')
                    record['history'].append({'review': record['review'], 'outputs': record['outputs']})
                record['outputs'] = []
            record['library_root'] = str(self.library)
            record['review'], record['review_sha256'] = preview.to_dict(), preview.fingerprint
            record['status'], record['message'] = 'reviewed', 'Plan saved. Run explicitly to publish text or create existing Studio jobs.'
            check_cancelled(cancelled)
            self._save(record)
            return self.get(preview.job_id)

    def _preview_again(self, record, preview, cancelled):
        options = SplitPlan.from_dict(preview.plans[0]).options
        template = preview.plans[0]['template']
        return self.preview(record['id'], preview.destination, options, template=template,
                            combined=preview.combined, action=preview.action, voice_id=preview.voice_id,
                            language=preview.language, format=preview.format, cancelled=cancelled)

    def _review(self, record):
        value = record.get('review')
        if type(value) is not dict:
            raise ValueError('Preview and explicitly approve this batch before running.')
        preview = BatchPreview.from_dict(value)
        if (preview.job_id != record['id'] or preview.fingerprint != record['review_sha256']
                or preview.conflicts or preview.action not in ('text', 'studio')
                or type(preview.combined) is not bool or preview.format not in _FORMATS):
            raise ValueError('The saved output plan changed; explicitly review it again.')
        if record['library_root'] != str(self.library):
            raise ValueError('Restored outputs require an explicit new destination preview and approval.')
        self._approved_directory(preview)
        return preview

    def _approved_directory(self, preview):
        """Keep the user-reviewed directory bound across a long resumed batch."""
        destination = local_path(preview.destination, exists=True)
        info = regular(destination, directory=True)
        if (info.st_dev, info.st_ino) != preview.destination_identity:
            raise ValueError('The output folder changed since approval.')
        _destination(destination / 'split-preview.txt', self.library)
        return destination

    def _new_output(self, preview, name):
        # Reuse the accepted writer's pre-stage and publication-time parent
        # checks. Recapturing here would approve a replacement directory.
        filename_key(name)
        target = Path(preview.destination) / name
        return ExportDestination(target, self.library, target, preview.destination_identity, None)

    def _verify_outputs(self, record, preview, stopped, production):
        if len(record['outputs']) != len(preview.outputs):
            raise ValueError('The batch completion receipt graph is incomplete.')
        for name, receipt in zip(preview.outputs, record['outputs']):
            check_cancelled(stopped)
            destination = self._approved_directory(preview)
            if receipt['name'] != name:
                raise ValueError('A completed batch output name changed.')
            if preview.action == 'text':
                _file(destination / name, 32 * 1024**2, stopped,
                      expected={key: receipt[key] for key in ('sha256', 'bytes')})
                _file(destination / receipt['provenance']['name'], 32 * 1024**2, stopped,
                      expected={key: receipt['provenance'][key] for key in ('sha256', 'bytes')})
            else:
                child = production.run(receipt['studio_job'], cancelled=stopped)
                if (child['status'] != 'complete' or child['fingerprint'] != receipt['project_fingerprint']
                        or child['destination'] != str(destination / name)):
                    raise ValueError('The existing Studio output graph changed before batch completion.')
        self._approved_directory(preview)

    def run(self, identity, *, cancelled=lambda: False, progress=None):
        with exclusive_writer(self.library), self.store.lock(identity):
            record = self._load(identity)
            preview = self._review(record)
            self._control(identity, 'run')
            stopped = lambda: bool(cancelled()) or self._control(identity) != 'run'
            owned = None
            try:
                self._verify_sources(record, stopped)
                if self._source_fingerprint(record) != preview.source_fingerprint:
                    raise ValueError('The prepared sources changed after review.')
                documents = self._documents(record, preview.combined, stopped)
                if len(documents) != len(preview.plans):
                    raise ValueError('The reviewed document set changed.')
                if record['status'] != 'complete':
                    record['status'], record['message'] = 'running', 'Running the explicitly reviewed document plan.'
                    self._save(record)
                item_index = 0
                for doc_index, ((_, document, _), value) in enumerate(zip(documents, preview.plans), 1):
                    plan = verify_plan(document, SplitPlan.from_dict(value), cancelled=stopped)
                    check_cancelled(stopped)
                    if preview.action == 'text':
                        for part in plan.parts:
                            check_cancelled(stopped)
                            self._approved_directory(preview)
                            if item_index >= len(preview.outputs) or preview.outputs[item_index] != part.name:
                                raise ValueError('Reviewed output names no longer match the split plan.')
                            target = Path(preview.destination) / part.name
                            raw = document.text[part.start:part.end]
                            expected = {'sha256': part.sha256, 'bytes': len(raw.encode('utf-8'))}
                            provenance = {'schema': 'carr-split-output/1', 'batch_id': identity,
                                'plan_sha256': record['review_sha256'], 'document': doc_index,
                                'name': part.name, **expected, 'part': asdict(part),
                                'reading_sha256': plan.reading_sha256, 'quotation_authority': False,
                                'combined_separators': '\n\n' if preview.combined else None,
                                'sources': [{'name': row['name'], 'sha256': row['sha256'],
                                             'reading_sha256': row['prepared']['reading_sha256']}
                                            for row in record['sources']]}
                            receipt_name = part.name + '.provenance.json'
                            receipt_path = target.with_name(receipt_name)
                            receipt_text = encode(provenance).decode('utf-8')
                            receipt_expected = {'sha256': sha256(receipt_text.encode()).hexdigest(),
                                                'bytes': len(receipt_text.encode())}
                            completed = {'name': part.name, **expected,
                                         'provenance': {'name': receipt_name, **receipt_expected}}
                            if item_index < len(record['outputs']):
                                receipt = record['outputs'][item_index]
                                if receipt != completed:
                                    raise ValueError('A completed output receipt changed.')
                                _file(target, 32 * 1024**2, stopped, expected=expected)
                                _file(receipt_path, 32 * 1024**2, stopped, expected=receipt_expected)
                            else:
                                # A real matching completion sidecar can reconcile a
                                # crash after publication but before the private save.
                                # Equal unreceipted bytes are never adopted implicitly.
                                if os.path.lexists(target) or os.path.lexists(receipt_path):
                                    if not target.exists() or not receipt_path.exists():
                                        raise FileExistsError('An incomplete or unrelated output exists. Review a new destination; it will not be overwritten.')
                                    _file(target, 32 * 1024**2, stopped, expected=expected)
                                    _file(receipt_path, 32 * 1024**2, stopped, expected=receipt_expected)
                                else:
                                    write_export(target, raw, private_root=self.library, cancelled=stopped, require_absent=True,
                                                 approval=self._new_output(preview, part.name))
                                    # Complete the bounded receipt after actual output
                                    # publication even if cancellation just arrived.
                                    write_export(receipt_path, receipt_text, private_root=self.library,
                                                 cancelled=lambda: False, require_absent=True,
                                                 approval=self._new_output(preview, receipt_name))
                                # No cancellation between durable publication and its receipt.
                                _file(target, 32 * 1024**2, lambda: False, expected=expected)
                                _file(receipt_path, 32 * 1024**2, lambda: False, expected=receipt_expected)
                                record['outputs'].append(completed)
                                self._save(record)
                            item_index += 1
                            _notify(progress, f'Verified output {item_index} of {len(preview.outputs)}: {part.name}')
                    else:
                        self._approved_directory(preview)
                        service = self.production
                        if service is None:
                            from ..studio.render import ProductionService
                            if owned is None:
                                owned = ProductionService(self.library / 'state' / 'studio')
                            service = owned
                        project = studio_project(document, plan, project_id=_studio_project_id(
                                                 identity, doc_index, plan, preview.voice_id, preview.language),
                                                 voice_id=preview.voice_id, language=preview.language)
                        job_id = _studio_job(identity, doc_index, plan, preview.voice_id,
                                             preview.language, preview.format, preview.destination)
                        try:
                            existing = service.get(job_id)
                        except FileNotFoundError:
                            submitted = service.submit(project, preview.destination, format=preview.format,
                                                       operation_id=job_id)
                        else:
                            if (existing['fingerprint'] != project.fingerprint or existing['format'] != preview.format
                                    or existing['destination'] != str(Path(preview.destination) / preview.outputs[item_index])):
                                raise ValueError('An existing Studio job belongs to different reviewed inputs.')
                            submitted = job_id
                        def studio_stopped():
                            self._approved_directory(preview)
                            return stopped()
                        state = service.run(submitted, cancelled=studio_stopped,
                                            progress=lambda message: _notify(progress, message))
                        if state['status'] != 'complete' or Path(state['destination']).name != preview.outputs[item_index]:
                            raise ValueError('The existing Studio job did not complete the reviewed output.')
                        receipt = {'name': preview.outputs[item_index], 'studio_job': submitted,
                                   'project_fingerprint': project.fingerprint}
                        if item_index < len(record['outputs']):
                            if record['outputs'][item_index] != receipt:
                                raise ValueError('A completed Studio admission receipt changed.')
                        else:
                            record['outputs'].append(receipt)
                            self._save(record)
                        item_index += 1
                if item_index != len(preview.outputs):
                    raise ValueError('The batch output graph is incomplete.')
                self._verify_sources(record, stopped)
                self._verify_outputs(record, preview, stopped, self.production or owned)
                record['status'], record['message'] = 'complete', 'Every reviewed output has been verified; original snapshots remain unchanged.'
                self._save(record)
                _notify(progress, record['message'])
                return self.get(identity)
            except BaseException as exc:
                self._record_failure(record, exc)
                raise
            finally:
                if owned is not None:
                    owned.close()

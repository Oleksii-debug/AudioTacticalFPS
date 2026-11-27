"""Reviewed assistant transcription over the existing supplied-worker service.

The selected machine profile is constructor authority, never a plan argument.
Checkpoints contain inert identities and receipts. A prepare/run dispatch with
no accepted binding is never replayed automatically after a crash. Exports use
one complete, exclusively renamed directory; retained ASR results stay separate.
"""
from dataclasses import asdict
from hashlib import sha256
import os
from pathlib import Path
import re

from .models import canonical, text

TOOL = 'audio.transcribe'
SCHEMA = 'carr-assistant-transcription/1'
RECEIPT = 'carr-assistant-transcription-receipt/1'
FORMATS = ('txt', 'srt', 'vtt', 'json')
_ID = re.compile(r'[0-9a-f]{32}\Z')
_SHA = re.compile(r'[0-9a-f]{64}\Z')
MAX_INPUT_BYTES = 32 * 1024**3


def _digest(value):
    if type(value) is not str or not _SHA.fullmatch(value):
        raise ValueError('Review an exact transcription checksum.')
    return value


def _identity(value):
    if (type(value) is not list or len(value) != 2
            or any(type(item) is not int or item < 0 for item in value)
            or value[1] == 0):
        raise ValueError('Invalid owned transcription directory identity.')
    return value


def _id(value):
    if type(value) is not str or not _ID.fullmatch(value):
        raise ValueError('Invalid transcription operation identity.')
    return value


def _hash(value):
    return sha256(canonical(value).encode('utf-8')).hexdigest()


def _settings(args):
    from ..transcription_receipt import validate_settings
    return validate_settings(args.get('settings', {}))


def validate_arguments(step):
    from .validation import _path
    from ..audio.paths import filename_component
    if (getattr(step, 'tool', None) != TOOL or type(step.arguments) is not dict
            or set(step.arguments) - {'path', 'source_sha256', 'name', 'settings'}
            or not {'path', 'source_sha256', 'name'} <= set(step.arguments)):
        raise ValueError('Transcription requires a source, its checksum and a new output directory name.')
    _path(step.arguments['path'])
    _digest(step.arguments['source_sha256'])
    name = text(step.arguments['name'], 'transcription output directory', maximum=128)
    filename_component(name)
    if name.startswith('.') or any(char in name for char in '/\\:'):
        raise ValueError('Choose one new directory name inside the approved output folder.')
    _settings(step.arguments)
    return step


def validate_saved_scope(step, scope, data_dir):
    from .validation import _path
    validate_arguments(step)
    source, data = _path(step.arguments['path']), _path(data_dir)
    if not any(type(source) is type(_path(root)) and source.is_relative_to(_path(root))
               for root in scope['input_roots']):
        raise ValueError('The transcription input is outside the approved source scope.')
    if type(source) is type(data) and source.is_relative_to(data):
        raise ValueError('Choose an explicit original audio file outside the private library.')
    if scope['output_root'] is None:
        raise ValueError('Choose an output folder for transcription.')
    output = _path(scope['output_root'])
    if type(output) is type(data) and output.is_relative_to(data):
        raise ValueError('Transcription exports must remain outside the private library.')
    count = sum(_settings(step.arguments)['output_' + kind] for kind in FORMATS) + 1
    if scope['max_files'] < count:
        raise ValueError('The transcription file budget must include each selected format and its provenance.')
    return step


def validate_plan_outputs(plan):
    """Reserve each whole export directory before an earlier step can mutate."""
    import unicodedata
    if not any(step.tool == TOOL for step in plan.steps):
        return
    selected = []
    for step in plan.steps:
        if step.tool not in (TOOL, 'topic.export', 'library.extract', 'library.backup',
                             'document.convert', 'audio.effects'):
            continue
        names = [step.arguments['name']]
        if step.tool in ('document.convert', 'audio.effects'):
            names.append(step.arguments['name'] + '.provenance.json')
        for name in names:
            key = unicodedata.normalize('NFC', str(plan.scope.output(name))).replace('\\', '/').casefold()
            selected.append((key, step.tool in (TOOL, 'library.backup')))
    for index, (left, directory) in enumerate(selected):
        for right, other_directory in selected[index + 1:]:
            if (left == right or directory and right.startswith(left + '/')
                    or other_directory and left.startswith(right + '/')):
                raise ValueError('Transcription directories and other planned outputs must not overlap.')


def _formats(review):
    return tuple(kind for kind in FORMATS if review['settings']['output_' + kind])


def _provenance(step, value):
    prepared, complete, review = value['prepared'], value['completion'], value['review']
    return {
        'schema': RECEIPT, 'operation_id': value['operation_id'],
        'job_id': prepared['job_id'], 'definition_sha256': prepared['definition_sha256'],
        'run_id': complete['run_id'], 'completion_sha256': complete['completion_sha256'],
        'job_config_sha256': complete['job_config_sha256'],
        'profile_id': review['profile_id'], 'profile_sha256': review['profile_sha256'],
        'runtime_manifest_sha256': review['runtime_manifest_sha256'],
        'model_sha256': review['model_sha256'],
        'source': {'path': step.arguments['path'], 'bytes': review['source_bytes'],
                   'sha256': step.arguments['source_sha256']},
        'settings': dict(review['settings']),
        'receipt_scope': 'verified-supplied-worker-completion',
    }


def _files(step, value):
    complete = value['completion']
    rows = {row['format']: row for row in complete['outputs']}
    result = [{'name': 'transcript.' + kind, 'size': rows[kind]['bytes'],
               'sha256': rows[kind]['sha256']} for kind in _formats(value['review'])]
    raw = (canonical(_provenance(step, value)) + '\n').encode('utf-8')
    return [*result, {'name': 'provenance.json', 'size': len(raw), 'sha256': sha256(raw).hexdigest()}]


def validate_intent(step, intent, scope, *, operation_id=None):
    """Pure portable checkpoint validation; no profile, runtime or path is opened."""
    from ..transcription_state import relative_path
    validate_arguments(step)
    if type(intent) is not dict or set(intent) != {'transcription'}:
        raise ValueError('The transcription step has no exact reviewed intent.')
    value = intent['transcription']
    if (type(value) is not dict or set(value) != {'schema', 'operation_id', 'review',
            'prepare_dispatched', 'prepared', 'run_dispatched', 'completion', 'publication'}
            or value['schema'] != SCHEMA):
        raise ValueError('Invalid transcription intent fields.')
    _id(value['operation_id'])
    if operation_id is not None and value['operation_id'] != operation_id:
        raise ValueError('The transcription intent belongs to another approved step.')
    review = value['review']
    if (type(review) is not dict or set(review) != {'profile_id', 'profile_sha256',
            'runtime_manifest_sha256', 'model_sha256', 'source_bytes', 'settings'}
            or review['settings'] != _settings(step.arguments)):
        raise ValueError('The transcription review differs from the approved settings.')
    _id(review['profile_id'])
    for key in ('profile_sha256', 'runtime_manifest_sha256', 'model_sha256'):
        _digest(review[key])
    if (type(review['source_bytes']) is not int
            or not 0 < review['source_bytes'] <= min(scope['max_bytes'], MAX_INPUT_BYTES)):
        raise ValueError('The reviewed transcription input exceeds its byte budget.')
    if type(value['prepare_dispatched']) is not bool or type(value['run_dispatched']) is not bool:
        raise ValueError('Invalid transcription dispatch checkpoint.')
    prepared = value['prepared']
    if prepared is not None:
        if (not value['prepare_dispatched'] or type(prepared) is not dict
                or set(prepared) != {'job_id', 'definition_sha256'}):
            raise ValueError('Invalid retained transcription definition binding.')
        _id(prepared['job_id'])
        _digest(prepared['definition_sha256'])
    if value['run_dispatched'] and prepared is None:
        raise ValueError('Inference cannot precede a bound retained definition.')
    complete = value['completion']
    if complete is not None:
        if (not value['run_dispatched'] or type(complete) is not dict
                or set(complete) != {'run_id', 'completion_sha256', 'job_config_sha256', 'outputs'}):
            raise ValueError('Invalid accepted transcription completion.')
        _id(complete['run_id'])
        _digest(complete['completion_sha256'])
        _digest(complete['job_config_sha256'])
        rows = complete['outputs']
        if type(rows) is not list or len(rows) != len(_formats(review)):
            raise ValueError('The transcription completion has incomplete format coverage.')
        formats, names, total = set(), set(), 0
        for row in rows:
            if (type(row) is not dict or set(row) != {'path', 'format', 'source_indexes', 'bytes', 'sha256'}
                    or row['format'] not in _formats(review)
                    or row['source_indexes'] != [0] or type(row['source_indexes'][0]) is not int
                    or type(row['bytes']) is not int or not 0 <= row['bytes'] <= scope['max_bytes']):
                raise ValueError('The accepted transcription output differs from its single reviewed input.')
            relative = relative_path(row['path'])
            if relative.suffix.casefold() != '.' + row['format']:
                raise ValueError('The transcription output suffix differs from its format.')
            _digest(row['sha256'])
            if row['format'] in formats or str(relative).casefold() in names:
                raise ValueError('Duplicate transcription output identity.')
            formats.add(row['format'])
            names.add(str(relative).casefold())
            total += row['bytes']
        if total > scope['max_bytes']:
            raise ValueError('Accepted transcription outputs exceed their byte budget.')
        if sum(row['size'] for row in _files(step, value)) > scope['max_bytes']:
            raise ValueError('Transcription outputs and provenance exceed their complete export budget.')
    publication = value['publication']
    if publication is not None:
        if (complete is None or type(publication) is not dict or set(publication) != {
                'parent_identity', 'stage_identity', 'stage_name', 'sealed', 'files'}
                or publication['stage_name'] != '.assistant-transcription-' + value['operation_id']
                or type(publication['sealed']) is not bool
                or publication['files'] != _files(step, value)):
            raise ValueError('The transcription publication differs from its accepted exact outputs.')
        _identity(publication['parent_identity'])
        _identity(publication['stage_identity'])
    return value


def _receipt(step, value):
    return {**_provenance(step, value), 'files': _files(step, value)}


def validate_result(step, result, scope, intent):
    from .validation import _path
    value = validate_intent(step, intent, scope)
    publication = value['publication']
    if publication is None or not publication['sealed']:
        raise ValueError('A transcription result requires a sealed directory publication.')
    if (type(result) is not dict or set(result) != {
            'directory', 'transcription_receipt', 'artifacts', 'warning'}
            or result['transcription_receipt'] != _receipt(step, value)):
        raise ValueError('The transcription receipt differs from its accepted source and runtime.')
    destination = _path(scope['output_root']) / step.arguments['name']
    if _path(result['directory']) != destination:
        raise ValueError('The transcription result is outside its approved new directory.')
    expected = [{'path': str(destination / row['name']), 'size': row['size'], 'sha256': row['sha256']}
                for row in publication['files']]
    actual = result['artifacts']
    if type(actual) is not list or len(actual) != len(expected):
        raise ValueError('Incomplete transcription artifact inventory.')
    for row, wanted in zip(actual, expected):
        if (type(row) is not dict or set(row) != {'path', 'size', 'sha256'}
                or _path(row['path']) != _path(wanted['path'])
                or type(row['size']) is not int or row['size'] != wanted['size']
                or row['sha256'] != wanted['sha256']):
            raise ValueError('A transcription artifact differs from its accepted receipt.')
    text(result['warning'], 'transcription publication warning', maximum=2000, empty=True)
    return result


def _cancel(cancelled):
    if cancelled():
        raise InterruptedError('Transcription stopped; retained results and its exact checkpoint remain available.')


def capture_source(path, scope, *, cancelled=lambda: False):
    from ..audio.jobs import file_identity, verified_reader
    from ..transcription_state import EXTENSIONS, regular
    source = scope.input(path)
    if source.suffix.lower() not in EXTENSIONS:
        raise ValueError('Choose one supported local audio or video file.')
    info = regular(source)
    if not 0 < info.st_size <= min(scope.max_bytes, MAX_INPUT_BYTES):
        raise ValueError('The selected transcription source exceeds its byte budget or is empty.')
    checksum = sha256()
    with verified_reader(source, expected_identity=file_identity(info)) as stream:
        remaining = info.st_size
        while remaining:
            _cancel(cancelled)
            block = stream.read(min(1024 * 1024, remaining))
            if not block:
                raise ValueError('The transcription source changed during review.')
            checksum.update(block)
            remaining -= len(block)
    return source, {'bytes': info.st_size, 'sha256': checksum.hexdigest()}


class SelectedTranscription:
    """One explicitly selected current EngineProfileStore registration.

    Discovery and execution use current library services. This class never
    registers a profile, starts a model during review, or reserves a second
    resource pool around the domain service's own inference lease.
    """
    def __init__(self, app, profile_id, *, expected_profile=None, current=lambda: True):
        from ..runtime_settings import EngineProfileStore
        self.app, self.current = app, current
        self.store = EngineProfileStore(app.root)
        self.profile = self.store.snapshot().profile(profile_id)
        if expected_profile is not None and self.profile != expected_profile:
            raise PermissionError('The selected transcription profile changed; select it and preview again.')
        if not self.profile.enabled or self.profile.engine != 'supplied-whisper':
            raise PermissionError('Choose an explicitly enabled supplied-whisper profile.')
        self.fingerprint = _hash(asdict(self.profile))

    def _profile(self, cancelled):
        _cancel(cancelled)
        if not self.current() or self.store.snapshot().profile(self.profile.id) != self.profile:
            raise PermissionError('The current library or transcription profile changed; preview again.')
        reviewed = self.store.admit(self.profile.id, engine='supplied-whisper', cancelled=cancelled)
        if (reviewed.manifest.manifest_sha256 != self.profile.manifest_sha256
                or str(reviewed.manifest.path) != self.profile.manifest_path
                or not self.current()
                or self.store.snapshot().profile(self.profile.id) != self.profile):
            raise PermissionError('The selected transcription runtime changed during review.')
        return reviewed.manifest

    def review(self, step, scope, *, cancelled=lambda: False):
        from ..transcription_service import review_runtime
        original_cancelled = cancelled
        cancelled = lambda: original_cancelled() or not self.current()
        validate_saved_scope(step, scope.to_dict(), str(self.app.root))
        destination = scope.output(step.arguments['name'])
        _directory(destination.parent)
        if _present(destination):
            raise FileExistsError('Choose a new transcription output directory before Preview.')
        source, captured = capture_source(step.arguments['path'], scope, cancelled=cancelled)
        if captured['sha256'] != step.arguments['source_sha256']:
            raise PermissionError('The transcription source differs from the reviewed checksum; preview again.')
        manifest = self._profile(cancelled)
        runtime = review_runtime(manifest.path, _settings(step.arguments),
            expected_sha256=self.profile.manifest_sha256, cancelled=cancelled)
        self._current(cancelled)
        return {'profile_id': self.profile.id, 'profile_sha256': self.fingerprint,
            'runtime_manifest_sha256': runtime.manifest.manifest_sha256,
            'model_sha256': runtime.model_sha256, 'source_bytes': captured['bytes'],
            'settings': dict(runtime.settings)}

    def _current(self, cancelled):
        _cancel(cancelled)
        if not self.current() or self.store.snapshot().profile(self.profile.id) != self.profile:
            raise PermissionError('The selected transcription profile or library changed.')

    def admit(self, review, *, cancelled=lambda: False):
        from ..transcription_service import review_runtime
        original_cancelled = cancelled
        cancelled = lambda: original_cancelled() or not self.current()
        if (review['profile_id'] != self.profile.id or review['profile_sha256'] != self.fingerprint):
            raise PermissionError('Select the exact currently reviewed transcription profile before Run.')
        manifest = self._profile(cancelled)
        runtime = review_runtime(manifest.path, review['settings'],
            expected_sha256=review['runtime_manifest_sha256'], cancelled=cancelled)
        if runtime.model_sha256 != review['model_sha256']:
            raise PermissionError('The reviewed local transcription model changed.')
        self._current(cancelled)
        return runtime.manifest.path


def review_step(app, step, scope, adapter):
    if not isinstance(adapter, SelectedTranscription) or adapter.app is not app:
        raise PermissionError('Explicitly select a current local transcription profile in this library before Preview.')
    return adapter.review(step, scope)


def initial_intent(app, step, scope, adapter, operation_id):
    value = {'schema': SCHEMA, 'operation_id': _id(operation_id),
        'review': review_step(app, step, scope, adapter), 'prepare_dispatched': False,
        'prepared': None, 'run_dispatched': False, 'completion': None, 'publication': None}
    intent = {'transcription': value}
    validate_intent(step, intent, scope.to_dict(), operation_id=operation_id)
    return intent


def _domain(app):
    from ..transcription_service import TranscriptionService
    return TranscriptionService(app.root, trust_controller=getattr(app, 'trust_controller', None))


def _definition_binding(definition, step, value, source):
    from .validation import _path
    review = value['review']
    origins = definition['source_provenance']
    if (len(origins) != 1 or len(definition['inputs']) != 1
            or definition['settings'] != review['settings']
            or definition['runtime_manifest_sha256'] != review['runtime_manifest_sha256']
            or definition['model_sha256'] != review['model_sha256']
            or _hash(definition) != value['prepared']['definition_sha256']):
        raise PermissionError('The retained transcription definition differs from its approved source/runtime.')
    origin = origins[0]
    if (_path(origin['path']) != source or _path(origin['root']) != source.parent
            or origin['relative'] != source.name
            or origin['bytes'] != review['source_bytes']
            or origin['sha256'] != step.arguments['source_sha256']):
        raise PermissionError('The retained transcription source differs from its approved provenance.')
    return definition


def _definition(domain, step, scope, value, cancelled):
    from .validation import _path
    definition = domain.definition(value['prepared']['job_id'], cancelled=cancelled)
    source = _path(str(scope.input(step.arguments['path'])))
    return _definition_binding(definition, step, value, source)


def validate_retained_checkpoint(root, step, scope, intent, *, cancelled=lambda: False):
    """Bind a passive assistant checkpoint only to its existing retained ASR job.

    The saved source, external output, profile and runtime paths stay lexical.
    No Scope, LibraryApp, profile store, permission, worker or inference is
    constructed during backup/restore. Existing producer validators own bytes.
    """
    from .validation import _path
    from ..audio.jobs import checked_directory
    from ..transcription_state import DIRECTORY, read_definition
    from ..supplied_transcription_backup import verify_saved_completion
    value = validate_intent(step, intent, scope)
    if value['prepared'] is None:
        return
    job = checked_directory(root) / DIRECTORY / value['prepared']['job_id']
    definition = read_definition(job, cancelled=cancelled)
    _definition_binding(definition, step, value, _path(step.arguments['path']))
    if value['completion'] is not None:
        verified = verify_saved_completion(job, definition, cancelled=cancelled)
        complete = {key: verified[key] for key in
                    ('run_id', 'completion_sha256', 'job_config_sha256', 'outputs')}
        if complete != value['completion']:
            raise ValueError('The assistant checkpoint differs from its retained accepted transcription.')


def _completion(domain, app, step, scope, value, cancelled):
    from ..transcription_state import DIRECTORY
    _definition(domain, step, scope, value, cancelled)
    verified = domain.completed(value['prepared']['job_id'], cancelled=cancelled)
    expected = Path(app.root) / DIRECTORY / value['prepared']['job_id']
    results = Path(verified['results_root'])
    if (Path(verified['job_root']) != expected or not results.is_relative_to(expected / 'results')
            or verified['input_count'] != 1
            or verified['runtime_manifest_sha256'] != value['review']['runtime_manifest_sha256']
            or verified['model_sha256'] != value['review']['model_sha256']):
        raise PermissionError('The completed transcription belongs to another retained job or runtime.')
    complete = {key: verified[key] for key in ('run_id', 'completion_sha256', 'job_config_sha256', 'outputs')}
    candidate = {**value, 'completion': complete}
    validate_intent(step, {'transcription': candidate}, scope.to_dict())
    if value['completion'] is not None and value['completion'] != complete:
        raise PermissionError('The accepted transcription execution changed; it will not be rerun.')
    return complete, results


def _directory(path):
    from ..audio.jobs import checked_directory
    root = checked_directory(path)
    info = root.lstat()
    return root, _identity([info.st_dev, info.st_ino])


def _present(path):
    from ..backup_files import optional_present
    return optional_present(path)


def _tree(path, rows, *, complete, cancelled):
    from ..transcription_state import regular
    from ..audio.model_manifest import digest_file
    folder, identity = _directory(path)
    expected = {row['name']: row for row in rows}
    observed = set()
    with os.scandir(folder) as entries:
        for count, entry in enumerate(entries, 1):
            _cancel(cancelled)
            if count > len(expected) or entry.name not in expected:
                raise PermissionError('An owned transcription stage contains an unexpected entry.')
            row, child = expected[entry.name], folder / entry.name
            if (regular(child).st_size != row['size']
                    or digest_file(child, cancelled=cancelled, max_bytes=max(1, row['size'])) != row['sha256']):
                raise PermissionError('A transcription export changed; no existing file will be replaced.')
            observed.add(entry.name)
    if complete and observed != set(expected):
        raise PermissionError('The sealed transcription publication is incomplete.')
    if _directory(folder)[1] != identity:
        raise PermissionError('The transcription directory changed during verification.')
    return observed


def _result(step, scope, value, warning=''):
    destination = scope.output(step.arguments['name'])
    result = {'directory': str(destination), 'transcription_receipt': _receipt(step, value),
        'artifacts': [{'path': str(destination / row['name']), 'size': row['size'], 'sha256': row['sha256']}
                      for row in value['publication']['files']], 'warning': warning}
    return validate_result(step, result, scope.to_dict(), {'transcription': value})


def _publish(app, step, scope, value, results, persist, cancelled):
    from ..audio.jobs import publish_new, _sync_directory
    from ..file_publication import publish_directory_no_replace
    from ..research.export_file import write_export
    destination = scope.output(step.arguments['name'])
    parent, parent_identity = _directory(destination.parent)
    rows = _files(step, value)
    publication = value['publication']
    stage = parent / ('.assistant-transcription-' + value['operation_id'])
    if publication is not None and parent_identity != publication['parent_identity']:
        raise PermissionError('The reviewed transcription output parent was replaced.')
    if _present(destination):
        if (publication is None or not publication['sealed']
                or _directory(destination)[1] != publication['stage_identity'] or _present(stage)):
            raise FileExistsError('The output directory already exists and is not this exact owned publication.')
        _tree(destination, rows, complete=True, cancelled=cancelled)
        return _result(step, scope, value)
    if publication is None:
        _cancel(cancelled)
        if _present(stage):
            raise FileExistsError('An unbound transcription stage already exists; review a new plan.')
        os.mkdir(stage, mode=0o700)
        stage_identity = _directory(stage)[1]
        if _directory(parent)[1] != parent_identity:
            raise PermissionError('The selected output parent changed before staging.')
        publication = {'parent_identity': parent_identity, 'stage_identity': stage_identity,
            'stage_name': stage.name, 'sealed': False, 'files': rows}
        value['publication'] = publication
        persist()  # The stage may be resumed only after its actual inode is bound.
    if (_directory(stage)[1] != publication['stage_identity']
            or _directory(parent)[1] != publication['parent_identity']):
        raise PermissionError('The owned transcription staging directory was replaced.')
    observed = _tree(stage, rows, complete=publication['sealed'], cancelled=cancelled)
    if not publication['sealed']:
        originals = {row['format']: row for row in value['completion']['outputs']}
        for row in rows:
            _cancel(cancelled)
            if row['name'] in observed:
                continue
            if _directory(stage)[1] != publication['stage_identity'] or _directory(parent)[1] != parent_identity:
                raise PermissionError('The owned transcription destination changed before copying.')
            target = stage / row['name']
            if row['name'] == 'provenance.json':
                write_export(target, canonical(_provenance(step, value)) + '\n',
                    private_root=app.root, require_absent=True, cancelled=cancelled)
            else:
                kind = row['name'].split('.')[-1]
                original = results.joinpath(*originals[kind]['path'].split('/'))
                publish_new(original, target, cancelled=cancelled, expected_sha256=row['sha256'])
        _tree(stage, rows, complete=True, cancelled=cancelled)
        _sync_directory(stage)
        publication['sealed'] = True
        persist()
    _cancel(cancelled)
    if (_directory(parent)[1] != parent_identity or _directory(stage)[1] != publication['stage_identity']):
        raise PermissionError('The transcription publication owner changed before commit.')
    publish_directory_no_replace(stage, destination)
    warning = ''
    try:
        _sync_directory(parent)
    except OSError:
        warning = 'The complete transcription directory was published; its parent durability flush could not be confirmed.'
    # The exclusive rename committed. A late Stop cannot turn this into a retry.
    _tree(destination, rows, complete=True, cancelled=lambda: False)
    return _result(step, scope, value, warning)


def execute(app, adapter, step, scope, intent, persist, *, operation_id,
            cancelled=lambda: False, progress=None):
    validate_saved_scope(step, scope.to_dict(), str(app.root))
    value = validate_intent(step, intent, scope.to_dict(), operation_id=operation_id)
    if adapter is not None and (not isinstance(adapter, SelectedTranscription) or adapter.app is not app):
        raise PermissionError('The selected transcription profile belongs to another library owner.')
    original_cancelled = cancelled
    cancelled = lambda: original_cancelled() or (adapter is not None and not adapter.current())
    source, captured = capture_source(step.arguments['path'], scope, cancelled=cancelled)
    if captured != {'bytes': value['review']['source_bytes'], 'sha256': step.arguments['source_sha256']}:
        raise PermissionError('The transcription input changed; review its current bytes in a new plan.')
    domain = _domain(app)
    if value['prepared'] is None:
        if value['prepare_dispatched']:
            raise PermissionError('Preparation may already have published a retained job. Review saved ASR jobs and submit a new plan; no unbound job will be adopted.')
        if adapter is None:
            raise PermissionError('Explicitly select the reviewed transcription profile before Run.')
        manifest = adapter.admit(value['review'], cancelled=cancelled)
        destination = scope.output(step.arguments['name'])
        _directory(destination.parent)
        if _present(destination):
            raise FileExistsError('Choose a new transcription output directory.')
        value['prepare_dispatched'] = True
        persist()
        identity = domain.prepare([source], manifest, value['review']['settings'],
            title='Assistant transcription', expected_manifest_sha256=value['review']['runtime_manifest_sha256'],
            cancelled=cancelled)
        definition = domain.definition(identity, cancelled=cancelled)
        value['prepared'] = {'job_id': identity, 'definition_sha256': _hash(definition)}
        _definition(domain, step, scope, value, cancelled)
        persist()
    _definition(domain, step, scope, value, cancelled)
    if value['run_dispatched']:
        try:
            complete, results = _completion(domain, app, step, scope, value, cancelled)
        except InterruptedError:
            raise
        except (OSError, ValueError, RuntimeError) as exc:
            raise PermissionError('The dispatched transcription has no matching accepted completion. Review its saved ASR job and explicitly submit a new plan; inference will not be replayed.') from exc
    else:
        if adapter is None:
            raise PermissionError('Explicitly select the reviewed transcription profile before inference.')
        manifest = adapter.admit(value['review'], cancelled=cancelled)
        _cancel(cancelled)
        value['run_dispatched'] = True
        persist()
        def observed(done, total, phase):
            if progress is not None:
                progress(str(done) + '/' + str(total) + ' local transcription: ' + str(phase))
        domain.run(value['prepared']['job_id'], manifest, cancelled=cancelled,
                   progress=observed, timeout=min(7 * 24 * 3600, max(1, scope.max_seconds)))
        complete, results = _completion(domain, app, step, scope, value, cancelled)
    if value['completion'] is None:
        value['completion'] = complete
        persist()
    return _publish(app, step, scope, value, results, persist, cancelled)


def verify_completed(app, step, scope, intent, result, *, cancelled=lambda: False):
    value = validate_intent(step, intent, scope.to_dict())
    validate_result(step, result, scope.to_dict(), intent)
    source, captured = capture_source(step.arguments['path'], scope, cancelled=cancelled)
    if captured != {'bytes': value['review']['source_bytes'], 'sha256': step.arguments['source_sha256']}:
        raise PermissionError('The completed transcription source changed; review a new plan.')
    _completion(_domain(app), app, step, scope, value, cancelled)
    publication = value['publication']
    destination = scope.output(step.arguments['name'])
    if (_directory(destination.parent)[1] != publication['parent_identity']
            or _directory(destination)[1] != publication['stage_identity']):
        raise PermissionError('The completed transcription output owner changed.')
    _tree(destination, publication['files'], complete=True, cancelled=cancelled)
    return result

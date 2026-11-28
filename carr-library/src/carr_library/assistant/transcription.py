"""Approved assistant glue for the existing retained-input transcription service.

This module schedules no worker of its own. It records the current source,
selected local model and configured capability identity before delegating to
TranscriptionService. Unknown submission outcomes remain for explicit review;
only an exact, complete export receipt can close a publication crash window.
"""
from hashlib import sha256
import json
import os
from pathlib import Path
import re

from ..audio.models import AudioCancelled
from ..durable_state import regular
from ..state_backup import relative_name
from ..transcription import MEDIA_SUFFIXES, MAX_SOURCE_BYTES, _checked_digest, settings
from ..transcription_contract import (bound_identity, check_requested_capabilities,
                                     model_inventory, validate_engine_document)
from .models import local_path, text

ADAPTER = 'carr-transcription-service/1'
REQUIRED = {'path', 'name'}
OPTIONAL = {'model', 'model_revision', 'language', 'vad_filter', 'beam_size',
            'condition_on_previous_text', 'word_timestamps'}
_INTENT_KEYS = {'transcription', 'submission_started', 'transcription_job', 'transcript_export'}
_BINDING_KEYS = {'adapter', 'workspace', 'source', 'destination', 'settings',
                 'configuration', 'model', 'sha256'}
MAX_EXPORT_FILES = 10385


def arguments(value):
    """Pure admission also preserves readable historical canonical plans.

    A missing model is admitted for historical review only. New execution and
    submission require an explicit named local model in preflight().
    """
    if type(value) is not dict or set(value) - REQUIRED - OPTIONAL or not REQUIRED <= set(value):
        raise ValueError('Invalid arguments for audio.transcribe.')
    text(value['path'], 'transcription input path', maximum=32768)
    text(value['name'], 'transcription export name', maximum=1024)
    chosen = {key: item for key, item in value.items() if key not in REQUIRED}
    # The reviewed segment-only adapter must never silently request word
    # alignment merely because the standalone editor has a different default.
    chosen.setdefault('word_timestamps', False)
    return settings(chosen)


def preflight(app, scope, args, workspace, *, existing=False, configuration=True):
    chosen = arguments(args)
    if 'model' not in args:
        raise ValueError('Select an explicit local model in audio.transcribe; review and submit a fresh plan.')
    if not workspace:
        raise PermissionError('Choose a library for the retained transcription job.')
    with app.read(workspace):
        pass
    if configuration:
        config = app.transcription(workspace).configuration()
        if config.get('restore_requires_revalidation', False):
            raise PermissionError('Review and revalidate the restored transcription engine before a new plan.')
        check_requested_capabilities(config, chosen)
    source = scope.input(args['path'])
    size = regular(source).st_size
    if source.suffix.lower() not in MEDIA_SUFFIXES or not 0 < size <= min(MAX_SOURCE_BYTES, scope.max_bytes):
        raise ValueError('Choose a nonempty supported media file within the approved byte budget.')
    destination = scope.output(args['name'])
    regular(local_path(destination.parent), directory=True)
    if destination.is_relative_to(app.root):
        raise PermissionError('Transcription exports must stay outside the private library data directory.')
    if not existing and os.path.lexists(destination):
        raise FileExistsError('The transcription export folder already exists; choose another name.')
    return source, size, destination, chosen


def _intent(intent):
    if type(intent) is not dict or set(intent) - _INTENT_KEYS:
        raise PermissionError('Unrecognized transcription checkpoint; review and submit a fresh plan.')
    if not intent:
        return
    binding = intent.get('transcription')
    if type(binding) is not dict or binding.get('adapter') != ADAPTER:
        raise PermissionError('This historical transcription adapter is not executable here; review a fresh plan.')
    if set(binding) != _BINDING_KEYS or bound_identity(binding) != binding:
        raise ValueError('The transcription binding changed.')
    if 'submission_started' in intent and intent['submission_started'] is not True:
        raise ValueError('Invalid transcription submission intent.')
    identity = intent.get('transcription_job')
    if identity is not None and (not isinstance(identity, str) or not re.fullmatch('[0-9a-f]{32}', identity)
                                 or intent.get('submission_started') is not True):
        raise ValueError('Invalid retained transcription job identity.')
    if 'transcript_export' in intent and identity is None:
        raise ValueError('An export receipt has no retained transcription job.')


def _check_job(job, binding, *, completed=False):
    if job.get('restore_requires_review', False):
        raise PermissionError('The retained transcription job was restored; review it and submit a fresh assistant plan.')
    source = binding['source']
    sources = job['sources']
    if (job['workspace'] != binding['workspace'] or job['settings'] != binding['settings']
            or len(sources) != 1 or sources[0]['size_bytes'] != source['size_bytes']
            or sources[0]['sha256'] != source['sha256']
            or sources[0]['original_name'] != Path(source['path']).name):
        raise PermissionError('The retained transcription job differs from the approved binding.')
    context = job.get('run_context')
    if context is not None:
        config = binding['configuration']
        if (context.get('worker_sha256') != config['worker_sha256']
                or context.get('python_sha256') != config['python_sha256']
                or context.get('model_identity') != binding['model']):
            raise PermissionError('The retained recognition used another engine or local model.')
    if completed and (job['status'] != 'completed' or job['done'] != 1 or job['failed'] != 0 or context is None):
        raise RuntimeError('Recognition has not completed every requested export; inspect its retained job.')


def _receipt(receipt, identity, maximum):
    if (type(receipt) is not dict or set(receipt) != {'version', 'job', 'files'}
            or type(receipt['version']) is not int or receipt['version'] != 1
            or receipt['job'] != identity or type(receipt['files']) is not list
            or not 1 <= len(receipt['files']) <= MAX_EXPORT_FILES):
        raise ValueError('Invalid transcription export receipt.')
    rows, aliases, total = {}, set(), 0
    for item in receipt['files']:
        if type(item) is not dict or set(item) != {'filename', 'size_bytes', 'sha256'}:
            raise ValueError('Invalid transcription export file identity.')
        name = item['filename']
        relative = relative_name(name)
        if (len(name) > 2048 or len(relative.parts) > 16 or name.casefold() in aliases
                or type(item['size_bytes']) is not int or item['size_bytes'] < 0
                or not isinstance(item['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', item['sha256'])):
            raise ValueError('Invalid transcription export file identity.')
        total += item['size_bytes']
        if total > maximum:
            raise ValueError('Transcription exports exceed the approved byte budget.')
        aliases.add(name.casefold())
        rows[name] = item
    if 'transcription-manifest.json' not in rows or 'incomplete.txt' in aliases:
        raise ValueError('The transcription export receipt is incomplete.')
    return rows


def verify_export(destination, receipt, identity, maximum, cancelled=lambda: False):
    """Validate the exact published namespace and bytes, without running a model."""
    rows = _receipt(receipt, identity, maximum)
    destination = local_path(destination)
    regular(destination, directory=True)
    expected_dirs = {parent.as_posix() for name in rows for parent in relative_name(name).parents
                     if parent.as_posix() != '.'}
    found, directories = set(), set()
    def walk_error(error):
        raise error
    for directory, dirs, names in os.walk(destination, followlinks=False, onerror=walk_error):
        if cancelled():
            raise AudioCancelled('Transcription export verification was cancelled.')
        regular(local_path(directory), directory=True)
        for name in dirs:
            path = Path(directory) / name
            relative = path.relative_to(destination).as_posix()
            if relative not in expected_dirs or relative in directories:
                raise ValueError('The transcription export has an unexpected directory.')
            regular(path, directory=True)
            directories.add(relative)
        for name in names:
            path = Path(directory) / name
            relative = path.relative_to(destination).as_posix()
            if relative not in rows or relative in found:
                raise ValueError('The transcription export is incomplete or has an unexpected file.')
            row = rows[relative]
            if _checked_digest(path, row['size_bytes'], cancelled) != row['sha256']:
                raise ValueError('A completed transcription export changed; nothing was overwritten.')
            found.add(relative)
    if found != set(rows) or directories != expected_dirs:
        raise ValueError('The transcription export is incomplete.')
    return [str(destination / relative_name(name)) for name in sorted(rows)]


def _result(binding, identity, artifacts):
    config, model = binding['configuration'], binding['model']
    return {'transcription_job': identity, 'status': 'completed',
            'binding_sha256': binding['sha256'], 'artifacts': artifacts,
            'source_sha256': binding['source']['sha256'],
            'model': {'selected': model['selected'], 'sha256': model['sha256'],
                      'resolved_revision': model['resolved_revision']},
            'engine': {'worker_sha256': config['worker_sha256'],
                       'python_sha256': config['python_sha256'],
                       'protocol': config['capabilities']['protocol']},
            'acoustic_quality': 'NOT_RUN',
            'notice': 'Machine transcription requires review. Protocol receipts do not qualify acoustic accuracy.'}


def finalize_result(app, workspace, args, scope, intent, result, cancelled=lambda: False):
    """Acknowledge a committed export from its actual receipt-bound bytes.

    Normal execution reaches this only after the existing exporter returned
    from its durable commit. Late cancellation cannot undo that publication.
    Scope revocation and byte/namespace mismatches still fail closed.
    """
    _intent(intent)
    binding, identity = intent['transcription'], intent.get('transcription_job')
    destination = scope.output(args['name'])
    if (binding['workspace'] != workspace or binding['settings'] != arguments(args)
            or binding['destination'] != str(destination)
            or not isinstance(result, dict) or result.get('transcription_job') != identity
            or result.get('binding_sha256') != binding['sha256']):
        raise PermissionError('Completed transcription permissions or identity changed.')
    with app.read(workspace):
        pass
    try:
        artifacts = verify_export(destination, intent.get('transcript_export'), identity, scope.max_bytes, cancelled)
    except AudioCancelled as exc:
        raise InterruptedError(str(exc)) from exc
    with app.read(workspace):
        pass
    if scope.output(args['name']) != destination:
        raise PermissionError('The selected transcription output scope changed.')
    expected = _result(binding, identity, artifacts)
    actual_paths = [item.get('path') if isinstance(item, dict) else item
                    for item in result.get('artifacts', [])]
    if actual_paths != artifacts or any(result.get(key) != value for key, value in expected.items()
                                        if key != 'artifacts'):
        raise ValueError('Completed transcription provenance differs from its durable binding.')
    rows = _receipt(intent['transcript_export'], identity, scope.max_bytes)
    return [{'path': str(destination / relative_name(name)), 'size': rows[name]['size_bytes'],
             'sha256': rows[name]['sha256']} for name in sorted(rows)]


def verify_completed(app, workspace, args, scope, intent, result, cancelled):
    if finalize_result(app, workspace, args, scope, intent, result, cancelled) != result.get('artifacts'):
        raise ValueError('The completed transcription artifact inventory changed.')


def execute(app, workspace, args, scope, intent, persist, cancelled, progress=None):
    """Delegate one approved media file; never replay an unknown submission."""
    try:
        return _execute(app, workspace, args, scope, intent, persist, cancelled, progress)
    except AudioCancelled as exc:
        raise InterruptedError(str(exc)) from exc


def _execute(app, workspace, args, scope, intent, persist, cancelled, progress):
    _intent(intent)
    service = app.transcription(workspace)
    identity = intent.get('transcription_job')
    job = service.get(identity) if identity is not None else None
    # Restore revocation precedes every current external runtime/model read.
    if job is not None and job.get('restore_requires_review', False):
        raise PermissionError('The retained transcription job was restored; review it before any execution.')
    source, size, destination, chosen = preflight(app, scope, args, workspace,
                                                existing=True, configuration=False)
    def check():
        if cancelled():
            raise InterruptedError('Transcription plan stopped; retained results remain available for review.')
        with app.read(workspace):
            pass
        if scope.input(args['path']) != source or scope.output(args['name']) != destination:
            raise PermissionError('The selected transcription input/output scope changed.')
    check()
    source_record = {'path': str(source), 'size_bytes': size,
                     'sha256': _checked_digest(source, size, cancelled)}
    binding = intent.get('transcription')
    if binding is not None:
        if (binding['workspace'] != workspace or binding['source'] != source_record
                or binding['destination'] != str(destination) or binding['settings'] != chosen):
            raise PermissionError('Transcription source or permissions changed; review a new plan.')
        validate_engine_document(binding['configuration'])
        check_requested_capabilities(binding['configuration'], chosen)
        if job is not None:
            _check_job(job, binding)
    if os.path.lexists(destination):
        if 'transcript_export' not in intent:
            raise FileExistsError('The export destination exists without this plan\'s durable receipt.')
        _check_job(service.verify(identity, cancelled=cancelled), binding, completed=True)
        artifacts = verify_export(destination, intent['transcript_export'], identity, scope.max_bytes, cancelled)
        return _result(binding, identity, artifacts)
    if intent.get('submission_started') is True and identity is None:
        raise PermissionError('The prior submission outcome is unknown. Inspect retained transcription jobs before a new plan; it will not be repeated.')
    config = service.configuration(verify=True)
    check_requested_capabilities(config, chosen)
    if progress:
        progress(0, 1, 'Verifying the explicitly selected local model')
    model = model_inventory(config['models_dir'], chosen, cancelled=cancelled)
    live = bound_identity({'adapter': ADAPTER, 'workspace': workspace, 'source': source_record,
                           'destination': str(destination), 'settings': chosen,
                           'configuration': config, 'model': model})
    if binding is not None and binding != live:
        raise PermissionError('The configured engine or selected model changed; review a new plan.')
    check()
    if binding is None:
        binding = live
        intent['transcription'] = binding
        persist()
    if identity is None:
        intent['submission_started'] = True
        persist()
        check()
        identity = service.submit([source], title='Approved assistant transcription', options=chosen,
            cancelled=cancelled, source_manifests=[{'size_bytes': size, 'sha256': source_record['sha256']}])
        intent['transcription_job'] = identity
        persist()
        job = service.get(identity)
    _check_job(job, binding)
    check()
    if job['status'] != 'completed':
        job = service.run(identity, cancelled=cancelled, progress=progress,
                          timeout=min(scope.max_seconds, 86400), max_result_bytes=scope.max_bytes,
                          expected_configuration=binding['configuration'], expected_model=binding['model'])
    if job['status'] == 'stopped' or cancelled():
        raise InterruptedError('Transcription stopped; its retained job can resume after explicit review.')
    _check_job(service.verify(identity, cancelled=cancelled), binding, completed=True)
    check()
    def before_publish(receipt):
        _receipt(receipt, identity, scope.max_bytes)
        previous = intent.get('transcript_export')
        if previous is not None and previous != receipt:
            raise PermissionError('The prepared transcript export changed; choose a newly reviewed destination.')
        check()
        intent['transcript_export'] = json.loads(json.dumps(receipt))
        persist()
        check()
    service.export(identity, destination, cancelled=cancelled, max_bytes=scope.max_bytes,
                   before_publish=before_publish)
    # The writer has removed INCOMPLETE and synced the destination. The
    # service acknowledges this durable outcome even if cancellation arrives
    # now; the next assistant step still observes its stop request.
    rows = _receipt(intent['transcript_export'], identity, scope.max_bytes)
    artifacts = [str(destination / relative_name(name)) for name in sorted(rows)]
    return _result(binding, identity, artifacts)

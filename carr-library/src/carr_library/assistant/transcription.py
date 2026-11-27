"""Current-service adapter for one explicitly reviewed transcription input."""
from contextlib import ExitStack
from pathlib import Path
from hashlib import sha256
import os

from .transcription_model import (RECEIPT_NAME, artifacts, completed_result,
    definition_digest, export_receipt, job_identity, job_title, validate_checkpoint)
from ..audio import jobs
from ..audio.model_manifest import _read_json_snapshot
from ..backup_files import optional_present
from ..transcription_state import (EXTENSIONS, MAX_JSON_BYTES, check_cancelled,
                                   read_definition, regular, relative_path)


def _as_interruption(cancelled):
    """Keep the assistant's durable Pause contract across shared audio helpers."""
    def stopping():
        check_cancelled(cancelled)
        return False
    return stopping


def _source(scope, arguments):
    source = scope.input(arguments['path'])
    info = regular(source)
    if source.suffix.lower() not in EXTENSIONS or not 0 < info.st_size <= scope.max_bytes:
        raise ValueError('Choose one supported transcription file within the approved byte budget.')
    formats = sum(arguments['settings']['output_' + value] for value in ('txt', 'srt', 'vtt', 'json'))
    if formats + 1 > scope.max_files:
        raise ValueError('The approved file budget must also include the transcript export receipt.')
    return source, info


def validate_admission(service, step, scope):
    from ..transcription_service import review_runtime
    cancelled = _as_interruption(service.validation_cancelled)
    check_cancelled(cancelled)
    _source(scope, step.arguments)
    destination = scope.output(step.arguments['name'])
    if optional_present(destination):
        raise FileExistsError('The transcription export needs a new directory.')
    review_runtime(step.arguments['runtime_manifest'], step.arguments['settings'],
                   expected_sha256=step.arguments['runtime_manifest_sha256'], cancelled=cancelled)


def _matches_definition(definition, binding):
    # Use cross-platform pure paths for restored provenance. No old input or
    # executable path is resolved by this content-only comparison.
    from .validation import _path
    source = _path(binding['source'])
    row = dict(path='inputs/root-0/' + source.name, root='inputs/root-0', relative=source.name,
               bytes=binding['size'], sha256=binding['sha256'])
    original = dict(path=str(source), root=str(source.parent), relative=source.name,
                    bytes=binding['size'], sha256=binding['sha256'])
    if (definition['id'] != binding['job_id'] or definition['title'] != job_title(binding['job_id'])
            or definition['settings'] != binding['settings']
            or definition['runtime_manifest_sha256'] != binding['runtime_manifest_sha256']
            or definition['model_sha256'] != binding['model_sha256']
            or definition['inputs'] != [row] or definition['source_provenance'] != [original]):
        raise ValueError('The retained transcription differs from this assistant step. No job was adopted.')


def _prepared(root, intent, *, cancelled):
    job = Path(root) / 'transcription-jobs' / intent['transcription']['job_id']
    definition = read_definition(job, cancelled=cancelled)
    _matches_definition(definition, intent['transcription'])
    checksum = definition_digest(definition)
    if 'definition_sha256' in intent and checksum != intent['definition_sha256']:
        raise ValueError('The prepared transcription definition changed after approval.')
    return job, definition, checksum


def _saved_summary(job, definition, *, cancelled):
    from ..transcription_backup import verify_saved_completion
    return verify_saved_completion(job, definition, cancelled=cancelled)


def _receipt_matches(summary, intent):
    expected = export_receipt(intent['transcription']['job_id'], summary)
    if intent.get('transcription_export') != expected:
        raise ValueError('The completed transcription no longer matches its saved export intent.')
    return expected


def _export_inventory(destination, expected, expected_dirs, cancelled):
    seen, directories, pending, count = {}, {}, [destination], 0
    while pending:
        directory = pending.pop()
        relative = directory.relative_to(destination).as_posix()
        if relative == '.':
            relative = ''
        info = jobs.checked_directory(directory).stat()
        directories[relative] = (info.st_dev, info.st_ino)
        if relative not in expected_dirs:
            raise ValueError('The transcript export contains an unexpected directory.')
        with os.scandir(directory) as entries:
            for entry in entries:
                check_cancelled(cancelled)
                count += 1
                if count > len(expected) + len(expected_dirs):
                    raise ValueError('The transcript export exceeds its exact inventory.')
                path = Path(entry.path)
                if path.is_symlink() or getattr(path.lstat(), 'st_file_attributes', 0) & 0x400:
                    raise ValueError('Transcript exports cannot contain link aliases.')
                if entry.is_dir(follow_symlinks=False):
                    pending.append(path)
                    continue
                name = path.relative_to(destination).as_posix()
                row = expected.get(name)
                if row is None or name in seen:
                    raise ValueError('The transcript export changed its exact file inventory.')
                info = regular(path)
                if info.st_size != row['size']:
                    raise ValueError('A transcript export changed its saved size.')
                seen[name] = jobs.file_identity(info)
    if set(seen) != set(expected) or set(directories) != expected_dirs:
        raise ValueError('The transcript export is incomplete.')
    return seen, directories


def _verify_export(destination, intent, *, cancelled):
    """Verify one exact exported tree while retaining all immutable file readers."""
    destination = jobs.checked_directory(destination)
    expected = {Path(row['path']).relative_to(destination).as_posix(): row for row in artifacts(intent)}
    expected_dirs = {''}
    for name in expected:
        expected_dirs.update(parent.as_posix() for parent in Path(name).parents if parent.as_posix() != '.')
    captured = _export_inventory(destination, expected, expected_dirs, cancelled)
    with ExitStack() as readers:
        for name, row in expected.items():
            stream = readers.enter_context(jobs.verified_reader(
                destination / name, expected_identity=captured[0][name]))
            digest, remaining = sha256(), row['size']
            while remaining:
                check_cancelled(cancelled)
                block = stream.read(min(1024 * 1024, remaining))
                if not block:
                    raise ValueError('A transcript export was truncated during verification.')
                digest.update(block)
                remaining -= len(block)
            if digest.hexdigest() != row['sha256']:
                raise ValueError('A transcript export failed its saved content check.')
        value, _ = _read_json_snapshot(destination / RECEIPT_NAME, max_bytes=MAX_JSON_BYTES)
        if value != intent['transcription_export']:
            raise ValueError('The transcript export receipt differs from its saved intent.')
        check_cancelled(cancelled)
        if _export_inventory(destination, expected, expected_dirs, lambda: False) != captured:
            raise ValueError('The transcript export changed during verification.')
    return tuple(destination / name for name in
                 sorted(captured[1], key=lambda value: (-len(Path(value).parts), value)))

def verify_completed(service, intent, result, scope, cancelled):
    cancelled = _as_interruption(cancelled)
    if result != completed_result(intent):
        raise ValueError('The saved transcript result no longer matches its intent.')
    job, definition, _ = _prepared(service.app.root, intent, cancelled=cancelled)
    _receipt_matches(_saved_summary(job, definition, cancelled=cancelled), intent)
    _verify_export(scope.output(Path(intent['transcription']['destination']).relative_to(scope.output_root)),
                   intent, cancelled=cancelled)


def validate_retained(root, record, *, cancelled):
    """Backup checks only owned definitions/receipts; all external paths stay inert."""
    cancelled = _as_interruption(cancelled)
    for step, checkpoint in zip(record['plan']['steps'], record['steps']):
        if step['tool'] != 'audio.transcribe' or not checkpoint['intent']:
            continue
        intent = checkpoint['intent']
        job = Path(root) / 'transcription-jobs' / intent['transcription']['job_id']
        if not optional_present(job):
            if 'definition_sha256' in intent:
                raise ValueError('The assistant checkpoint lost its prepared transcription.')
            continue
        job, definition, _ = _prepared(root, intent, cancelled=cancelled)
        if 'transcription_export' in intent:
            _receipt_matches(_saved_summary(job, definition, cancelled=cancelled), intent)


def execute(service, step, scope, record, checkpoint, persist, cancelled, progress, index):
    from ..transcription_service import TranscriptionService, review_runtime
    from ..transcription_backup import _execution
    from ..transcription_exports import export_results

    cancelled = _as_interruption(cancelled)
    args, intent = step.arguments, checkpoint['intent']
    destination = scope.output(args['name'])
    if optional_present(destination) and 'transcription_export' not in intent:
        raise FileExistsError('An existing directory has no saved export intent and will not be adopted.')
    if not intent:
        source, info = _source(scope, args)
        if optional_present(destination):
            raise FileExistsError('The transcription destination already exists and is not this job output.')
        runtime = review_runtime(args['runtime_manifest'], args['settings'],
            expected_sha256=args['runtime_manifest_sha256'], cancelled=cancelled)
        checksum = jobs.sha256_file(source, expected_identity=jobs.file_identity(info), cancelled=cancelled)
        check_cancelled(cancelled)
        intent['transcription'] = dict(job_id=job_identity(record['id'], index), source=str(source),
            size=info.st_size, sha256=checksum, runtime_manifest_sha256=args['runtime_manifest_sha256'],
            model_sha256=runtime.model_sha256, settings=dict(runtime.settings), destination=str(destination))
        persist()  # The unique ID and all authority exist before the first prepare mutation.
    binding = intent['transcription']
    asr = TranscriptionService(service.app.root)
    job = asr.root / binding['job_id']
    if not optional_present(job):
        if 'definition_sha256' in intent:
            raise ValueError('The prepared transcription is missing; this step will not create a replacement.')
        source, info = _source(scope, args)
        if info.st_size != binding['size']:
            raise ValueError('The approved transcription source changed before preparation.')
        with jobs.verified_reader(source, expected_identity=jobs.file_identity(info)):
            if jobs.sha256_file(source, cancelled=cancelled) != binding['sha256']:
                raise ValueError('The approved transcription source changed before preparation.')
            check_cancelled(cancelled)
            asr.prepare([source], args['runtime_manifest'], binding['settings'],
                title=job_title(binding['job_id']), expected_manifest_sha256=binding['runtime_manifest_sha256'],
                job_id=binding['job_id'], cancelled=cancelled)
    job, definition, checksum = _prepared(service.app.root, intent, cancelled=cancelled)
    if 'definition_sha256' not in intent:
        intent['definition_sha256'] = checksum
        persist()
    # A damaged claimed completion must never become an implicit inference retry.
    state_path = job / 'execution.json'
    completed = False
    if optional_present(state_path):
        execution, _ = _read_json_snapshot(state_path, max_bytes=65536)
        _execution(execution)
        completed = execution['state'] == 'complete'
    if not completed:
        if 'transcription_export' in intent:
            raise ValueError('A previously completed transcription lost its accepted execution state.')
        check_cancelled(cancelled)
        def report(done, total, message):
            if progress:
                progress(index, len(record['steps']), 'Transcription ' + str(done) + '/' + str(total) + ': ' + message)
        asr.run(binding['job_id'], args['runtime_manifest'], cancelled=cancelled, progress=report,
            timeout=max(1, min(scope.max_seconds, 7 * 24 * 3600)),
            expected_definition_sha256=intent['definition_sha256'])
    job, definition, _ = _prepared(service.app.root, intent, cancelled=cancelled)
    summary = _saved_summary(job, definition, cancelled=cancelled)
    receipt = export_receipt(binding['job_id'], summary)
    if 'transcription_export' in intent:
        _receipt_matches(summary, intent)
    else:
        if optional_present(destination):
            raise FileExistsError('An existing directory has no prior export intent and will not be adopted.')
        candidate = dict(intent, transcription_export=receipt)
        validate_checkpoint(record, index, args, dict(checkpoint, intent=candidate))
        intent['transcription_export'] = receipt
        # The pure checkpoint validator also enforces exact selected formats,
        # one-input coverage and all output byte/file budgets before publication.
        persist()
    jobs.checked_directory(destination.parent, create=True)
    if optional_present(destination):
        directories = _verify_export(destination, intent, cancelled=cancelled)
        # An earlier directory publication may have reported a post-move sync
        # error. Successful retry acknowledges only a verified, now durable tree.
        for directory in directories:
            jobs._sync_directory(directory)
        jobs._sync_directory(destination.parent)
    else:
        check_cancelled(cancelled)
        export_results(asr, binding['job_id'], destination, cancelled=cancelled)
        _verify_export(destination, intent, cancelled=cancelled)
    return completed_result(intent)

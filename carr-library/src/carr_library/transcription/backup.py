"""Typed streaming sidecars for owned transcription data in common backup V4.

Only fixed owned inputs and host-receipted derivatives are included. Profiles,
executables, weights, diagnostics, live worker configuration/cache and locks are
never traversed. Original path strings remain inert provenance, not capability.
"""
from contextlib import ExitStack
from hashlib import sha256
import os
from pathlib import Path, PurePosixPath
import re

from ..audio.jobs import checked_directory, project_lock, _sync_directory
from ..audio.library_sidecars import _copy
from ..ingest.restore import RestoreError
from .files import canonical, file_digest, parse_json, read_json, regular, stamp, write_new
from .models import INPUT_SUFFIXES
from .validation import (REQUEST_BYTES, RESTORED_BYTES, RESTORED_NAME,
                         validate_request, validate_receipt, validate_restored)


DIRECTORY = 'transcription-jobs'
MAX_JOBS = 10000
MAX_FILES = 100000
MAX_BYTES = 1024**4
MAX_INPUT_BYTES = 512 * 1024**3
MAX_OUTPUT_BYTES = 256 * 1024**2
_PREFIX = re.compile(r'transcription-jobs/([0-9a-f]{32})/(.+)\Z')
_INPUT = re.compile(r'inputs/source-[0-9]{5}\.[a-z0-9]+\Z')
_OUTPUT = re.compile(r'results/inputs/source-[0-9]{5}\.(?:txt|srt|vtt|json)(?:\.v43-output\.json)?\Z')


def check_cancelled(cancelled):
    if not callable(cancelled):
        raise ValueError('A cancellation callback is required.')
    if cancelled():
        raise InterruptedError('Transcription backup was cancelled.')


def _parts(name):
    matched = _PREFIX.fullmatch(name) if isinstance(name, str) else None
    if matched is None:
        raise RestoreError('unsupported transcription sidecar')
    tail = matched[2]
    if tail not in ('request.json', 'receipt.json'):
        if _INPUT.fullmatch(tail):
            if PurePosixPath(tail).suffix not in INPUT_SUFFIXES:
                raise RestoreError('unsupported transcription input sidecar')
        elif not _OUTPUT.fullmatch(tail):
            raise RestoreError('unsupported transcription derivative sidecar')
    return matched[1], tail


def accepts(name):
    if not isinstance(name, str) or not name.startswith(DIRECTORY + '/'):
        return False
    _parts(name)
    return True


def limit(name):
    tail = _parts(name)[1]
    if tail.startswith('inputs/'):
        return MAX_INPUT_BYTES
    if tail.startswith('results/'):
        return MAX_OUTPUT_BYTES
    return REQUEST_BYTES


def minimum(name):
    tail = _parts(name)[1]
    return 0 if tail.endswith(('.txt', '.srt', '.vtt')) else 1


def streamed(name):
    return _parts(name)[1].startswith(('inputs/', 'results/'))


def validate_metadata(db, name, raw):
    """Structural entry check; validate_tree supplies cross-file bindings."""
    job_id, tail = _parts(name)
    if tail not in ('request.json', 'receipt.json') or type(raw) is not bytes or not 0 < len(raw) <= REQUEST_BYTES:
        raise RestoreError('invalid transcription metadata sidecar')
    value = parse_json(raw)
    if tail == 'request.json':
        validate_request(value, job_id)
        if db.execute('SELECT 1 FROM workspace_meta WHERE workspace=?', (value['workspace'],)).fetchone() is None:
            raise RestoreError('transcription request belongs to a missing library')
    elif (type(value) is not dict or value.get('schema') != 'resonance-transcription-receipt/1'
          or value.get('id') != job_id or value.get('quotation_authority') is not False):
        raise RestoreError('invalid transcription derivative receipt')
    return value


def _jobs(root, cancelled):
    directory = Path(root) / DIRECTORY
    if not os.path.lexists(directory):
        return ()
    checked_directory(directory)
    result = []
    with os.scandir(directory) as children:
        for number, child in enumerate(children, 1):
            check_cancelled(cancelled)
            if number > MAX_JOBS * 2:
                raise RestoreError('transcription directory inventory exceeds its budget')
            if child.name.startswith('.preparing-'):
                continue
            if re.fullmatch('[0-9a-f]{32}', child.name) is None or len(result) >= MAX_JOBS:
                raise RestoreError('invalid or oversized transcription job inventory')
            checked_directory(Path(child.path))
            result.append(child.name)
    return tuple(sorted(result))


def _capture(db, root, jobs, max_bytes, cancelled):
    root = Path(root)
    paths, fingerprints, total = {}, {}, 0
    def check():
        check_cancelled(cancelled)
    def add(job_id, tail, expected):
        nonlocal total
        check()
        name = DIRECTORY + '/' + job_id + '/' + tail
        accepts(name)
        path = root / name
        info = regular(path)
        if (len(paths) >= MAX_FILES or not minimum(name) <= info.st_size <= limit(name)
                or info.st_size != expected['size_bytes']):
            raise RestoreError('transcription sidecar exceeds its size/file budget')
        total += info.st_size
        if total > min(MAX_BYTES, max_bytes):
            raise RestoreError('transcription backup exceeds its approved byte budget')
        if file_digest(path, check) != expected['sha256']:
            raise RestoreError('transcription source or derivative changed')
        paths[name], fingerprints[name] = path, (stamp(info), expected['sha256'])
    for job_id in jobs:
        check()
        directory = root / DIRECTORY / job_id
        allowed = {'request.json', 'receipt.json', RESTORED_NAME, 'inputs', 'results',
                   'logs', 'cache', 'temp', 'worker-lock', '.render.lock',
                   'job_config.json', 'settings_live.json', 'pause.flag', 'stop.flag', 'job_status.json'}
        with os.scandir(directory) as children:
            for count, child in enumerate(children, 1):
                check()
                if count > 64:
                    raise RestoreError('transcription job root exceeds its entry budget')
                if child.name not in allowed and re.fullmatch(r'\.transcription-[0-9a-f]{32}', child.name) is None:
                    raise RestoreError('unrecognized transcription state; no files were silently omitted')
        request, raw = read_json(directory / 'request.json', REQUEST_BYTES, check)
        validate_request(request, job_id)
        if db.execute('SELECT 1 FROM workspace_meta WHERE workspace=?', (request['workspace'],)).fetchone() is None:
            raise RestoreError('transcription request belongs to a missing library')
        request_hash = sha256(raw).hexdigest()
        add(job_id, 'request.json', {'size_bytes': len(raw), 'sha256': request_hash})
        input_root = checked_directory(directory / 'inputs')
        expected_inputs = {row['name'] for row in request['sources']}
        actual_inputs = set()
        with os.scandir(input_root) as children:
            for child in children:
                check()
                if len(actual_inputs) >= len(expected_inputs) or child.name not in expected_inputs:
                    raise RestoreError('transcription input inventory changed')
                actual_inputs.add(child.name)
        if actual_inputs != expected_inputs:
            raise RestoreError('transcription input inventory is incomplete')
        for row in request['sources']:
            add(job_id, 'inputs/' + row['name'], {'size_bytes': row['bytes'], 'sha256': row['sha256']})
        receipt_path = directory / 'receipt.json'
        receipt_hash = None
        if os.path.lexists(receipt_path):
            receipt, encoded = read_json(receipt_path, REQUEST_BYTES, check)
            outputs = validate_receipt(receipt, request, raw)
            receipt_hash = sha256(encoded).hexdigest()
            add(job_id, 'receipt.json', {'size_bytes': len(encoded), 'sha256': receipt_hash})
            result_root = checked_directory(directory / 'results')
            result_names = set()
            with os.scandir(result_root) as children:
                for child in children:
                    check()
                    if child.name != 'inputs' or result_names:
                        raise RestoreError('unexpected completed transcription output directory')
                    result_names.add(child.name)
            if result_names != {'inputs'}:
                raise RestoreError('unexpected completed transcription output directory')
            output_root = checked_directory(result_root / 'inputs')
            expected_outputs = {PurePosixPath(row['path']).name for row in outputs}
            actual_outputs = set()
            with os.scandir(output_root) as children:
                for child in children:
                    check()
                    if len(actual_outputs) >= len(expected_outputs) or child.name not in expected_outputs:
                        raise RestoreError('unexpected completed transcription output')
                    actual_outputs.add(child.name)
            if actual_outputs != expected_outputs:
                raise RestoreError('completed transcription outputs are incomplete')
            for row in outputs:
                add(job_id, row['path'], {'size_bytes': row['bytes'], 'sha256': row['sha256']})
        marker = directory / RESTORED_NAME
        if os.path.lexists(marker):
            value, _ = read_json(marker, RESTORED_BYTES, check)
            validate_restored(value, job_id, request_hash, receipt_hash)
            if value['receipt_sha256'] != receipt_hash:
                raise RestoreError('restored transcription marker changed')
    return paths, fingerprints


def snapshot(db, root, stage, *, max_bytes=MAX_BYTES, cancelled=lambda: False):
    check_cancelled(cancelled)
    if type(max_bytes) is not int or max_bytes < 0:
        raise RestoreError('invalid transcription backup byte budget')
    root, stage = Path(root), Path(stage)
    jobs = _jobs(root, cancelled)
    with ExitStack() as locks:
        for job_id in jobs:
            directory = root / DIRECTORY / job_id
            try:
                locks.enter_context(project_lock(directory))
                locks.enter_context(project_lock(directory / 'worker-lock'))
            except RuntimeError as exc:
                raise RestoreError('A transcription worker is active. Stop it and retry the backup.') from exc
        paths, before = _capture(db, root, jobs, max_bytes, cancelled)
        entries = []
        for name in sorted(paths):
            check_cancelled(cancelled)
            fingerprint = before[name]
            expected = {'size_bytes': fingerprint[0][4], 'sha256': fingerprint[1]}
            entries.append({'filename': name, **_copy(paths[name], stage / name,
                maximum=limit(name), cancelled=cancelled, expected=expected)})
        _, after = _capture(db, root, jobs, max_bytes, cancelled)
        if before != after or _jobs(root, cancelled) != jobs:
            raise RestoreError('transcription jobs changed during backup')
        validate_tree(db, stage, entries, cancelled=cancelled)
        return entries


def validate_tree(db, root, entries, *, cancelled=lambda: False):
    names = {entry['filename'] for entry in entries}
    if len(names) != len(entries):
        raise RestoreError('duplicate transcription sidecar')
    jobs = tuple(sorted({_parts(name)[0] for name in names}))
    paths, _ = _capture(db, Path(root), jobs, MAX_BYTES, cancelled)
    if set(paths) != names:
        raise RestoreError('transcription manifest includes incomplete or unaccepted artifacts')


def restore(db, source, stage, entries, *, cancelled=lambda: False, copy_files=True):
    check_cancelled(cancelled)
    if type(copy_files) is not bool or len(entries) > MAX_FILES:
        raise RestoreError('invalid transcription restore options')
    total = 0
    for entry in entries:
        check_cancelled(cancelled)
        name = entry['filename']
        accepts(name)
        path = Path(source) / name
        regular(path)
        total += entry['size_bytes']
        if total > MAX_BYTES:
            raise RestoreError('transcription restore exceeds its byte budget')
        _copy(path, Path(stage) / name if copy_files else None,
              maximum=limit(name), cancelled=cancelled, expected=entry)
    validate_tree(db, stage if copy_files else source, entries, cancelled=cancelled)


def initialize_restored(root, entries):
    """Called only after verified restore publication, never during backup creation.

    Reviewed source bytes and their hashes are unchanged. The new marker is
    runtime authorization state, not a rewrite of the signed source manifest.
    """
    by_name = {entry['filename']: entry for entry in entries}
    for job_id in sorted({_parts(name)[0] for name in by_name}):
        prefix = DIRECTORY + '/' + job_id + '/'
        request = by_name[prefix + 'request.json']
        receipt = by_name.get(prefix + 'receipt.json')
        directory = checked_directory(Path(root) / DIRECTORY / job_id)
        marker = {'schema': 'resonance-transcription-restored/1', 'id': job_id,
                  'request_sha256': request['sha256'],
                  'receipt_sha256': None if receipt is None else receipt['sha256'],
                  'requires_reapproval': True}
        write_new(directory / RESTORED_NAME, canonical(marker))
        checked_directory(directory / 'worker-lock', create=True)
        _sync_directory(directory)

"""Typed backup of prepared media, retained originals and completed receipts.

Restore preserves inert data and never invokes an executable or an old source
path. Only the current service's explicit Resume can admit a new processor.
"""
from contextlib import ExitStack, contextmanager
import os
from pathlib import Path, PurePosixPath
import re

from ..backup_files import local_path, optional_present, regular
from .jobs import project_lock, read_json
from .media_conversion import read_definition, verified_results
from .media_types import (DIRECTORY, MAX_FILES, MAX_JOBS, MAX_METADATA_BYTES, MAX_RECEIPT_BYTES,
                         MAX_SNAPSHOT_BYTES, identity, validate_control, validate_job, validate_presets)

_JOB = re.compile(r'[0-9a-f]{32}\Z')
_ITEM = re.compile(r'[0-9]{6}\Z')
_INPUT = re.compile(r'[0-9]{6}\.media\Z')
_OUTPUT = re.compile(r'[0-9]{6}-[^/\\]{1,70}\.(?:wav|flac|mp3|opus|m4a|m4b|aiff|ogg|aac|wma)\Z')
_PREPARE = re.compile(r'\.prepare-[a-z0-9_]{8}\Z')
_WORK = re.compile(r'\.work-[a-z0-9_]{8}\Z')
_CHECKPOINT = re.compile(r'\.checkpoint-[a-z0-9_]{8}\Z')


def _check(cancelled):
    if cancelled():
        raise InterruptedError('Media snapshot verification cancelled.')


def _parts(relative):
    if type(relative) is not str or '\\' in relative or '\x00' in relative:
        return ()
    parts = PurePosixPath(relative).parts
    if '/'.join(parts) != relative or not parts or parts[0] != DIRECTORY:
        return ()
    return parts[1:]


def is_state_path(relative):
    p = _parts(relative)
    if p == ('presets.json',):
        return True
    if len(p) < 2 or not _JOB.fullmatch(p[0]):
        return False
    return bool(len(p) == 2 and p[1] in ('definition.json', 'job.json', 'control.json')
        or len(p) == 3 and p[1] == 'inputs' and _INPUT.fullmatch(p[2])
        or len(p) == 4 and p[1] == 'results' and _ITEM.fullmatch(p[2])
            and (p[3] == 'receipt.json' or _OUTPUT.fullmatch(p[3])))


def state_limit(relative):
    if not is_state_path(relative):
        raise ValueError('Unsupported media backup path.')
    if relative.endswith('.lock'):
        return 1
    if relative.endswith('/receipt.json'):
        return MAX_RECEIPT_BYTES
    return MAX_METADATA_BYTES if relative.endswith('.json') else MAX_SNAPSHOT_BYTES


def allows_empty(relative):
    return False


def _directory(parts):
    return bool(not parts or len(parts) == 1 and _JOB.fullmatch(parts[0])
                or len(parts) == 2 and _JOB.fullmatch(parts[0]) and parts[1] in ('inputs', 'results')
                or len(parts) == 3 and _JOB.fullmatch(parts[0]) and parts[1] == 'results' and _ITEM.fullmatch(parts[2]))


def state_paths(root, *, cancelled=lambda: False):
    root = local_path(root, exists=True)
    directory = root / DIRECTORY
    if not optional_present(directory):
        return ()
    regular(directory, directory=True)
    result, stack, count, total, jobs = [], [(directory, False, 0)], 0, 0, 0
    while stack:
        parent, opaque, depth = stack.pop()
        _check(cancelled)
        if depth > 5:
            raise ValueError('The media namespace contains an unexpected directory depth.')
        with os.scandir(parent) as entries:
            for entry in entries:
                _check(cancelled)
                count += 1
                if count > MAX_FILES:
                    raise ValueError('The saved media inventory exceeds its entry allowance.')
                path = local_path(entry.path, root=directory, exists=True)
                # Check local file/reparse/hardlink rules before excluding any
                # exact transient stage. Pending bytes are never acknowledged
                # outputs; their original inputs remain in the registered set.
                is_directory = path.is_dir()
                info = regular(path, directory=is_directory)
                relative = path.relative_to(root).as_posix()
                p = _parts(relative)
                temporary = (opaque or len(p) == 1 and _PREPARE.fullmatch(p[0])
                             or len(p) == 2 and _JOB.fullmatch(p[0]) and _WORK.fullmatch(p[1]))
                if is_directory:
                    if not temporary and not _directory(p):
                        raise ValueError('The media namespace contains an unregistered directory.')
                    if len(p) == 1 and not temporary:
                        jobs += 1
                        if jobs > MAX_JOBS:
                            raise ValueError('The saved media job count exceeds its allowance.')
                    stack.append((path, bool(temporary), depth + 1))
                    continue
                total += info.st_size
                if total > MAX_SNAPSHOT_BYTES:
                    raise ValueError('Saved media exceeds the snapshot byte allowance.')
                if temporary:
                    if not opaque:
                        raise ValueError('A pending media stage must be a directory.')
                    continue
                if (p == ('.state.lock',) or len(p) == 2 and _JOB.fullmatch(p[0])
                        and p[1] in ('.render.lock', '.control.lock')):
                    if info.st_size > 1:
                        raise ValueError('A media native lock has unexpected bytes.')
                    continue
                if _CHECKPOINT.fullmatch(path.name) and len(p) in (1, 2, 4):
                    if info.st_size > MAX_METADATA_BYTES:
                        raise ValueError('A pending media checkpoint exceeds its metadata allowance.')
                    continue
                if not is_state_path(relative) or not (0 if allows_empty(relative) else 1) <= info.st_size <= state_limit(relative):
                    raise ValueError('The media namespace contains an unsupported or oversized file.')
                result.append(path)
    _check(cancelled)
    return tuple(sorted(result))


def validate_state_tree(db, root, *, cancelled=lambda: False):
    root = local_path(root, exists=True)
    paths = state_paths(root, cancelled=cancelled)
    names = {path.relative_to(root).as_posix() for path in paths}
    expected = set()
    for path in paths:
        _check(cancelled)
        if path == root / DIRECTORY / 'presets.json':
            validate_presets(read_json(path, limit=MAX_METADATA_BYTES))
            expected.add(path.relative_to(root).as_posix())
    jobs = []
    directory = root / DIRECTORY
    if optional_present(directory):
        with os.scandir(directory) as entries:
            for entry in entries:
                _check(cancelled)
                if _JOB.fullmatch(entry.name):
                    jobs.append(entry.name)
    jobs.sort()
    for job_id in jobs:
        job = root / DIRECTORY / job_id
        definition = read_definition(job)
        state = validate_job(read_json(job / 'job.json', limit=MAX_METADATA_BYTES), definition)
        results = verified_results(job, definition, cancelled=cancelled)
        if state['completed'] > len(results) or state['status'] == 'complete' and len(results) != state['total']:
            raise ValueError('The saved media checkpoint is missing acknowledged results.')
        prefix = f'{DIRECTORY}/{job_id}/'
        expected.update((prefix + 'definition.json', prefix + 'job.json'))
        expected.update(prefix + row['retained'] for row in definition['inputs'])
        for result in results:
            result_prefix = prefix + f"results/{result['index']:06d}/"
            expected.update((result_prefix + 'receipt.json', result_prefix + result['output']['path']))
        control = job / 'control.json'
        if optional_present(control):
            validate_control(read_json(control, limit=MAX_METADATA_BYTES), job_id)
            expected.add(prefix + 'control.json')
    if names != expected:
        raise ValueError('The media snapshot contains missing or unbound job files.')
    _check(cancelled)


@contextmanager
def snapshot_guard(root, *, cancelled=lambda: False):
    """Outer caller owns app writer; then namespace and sorted actual job locks."""
    root = local_path(root, exists=True)
    directory = root / DIRECTORY
    _check(cancelled)
    if not optional_present(directory):
        yield
        return
    regular(local_path(directory, root=root, exists=True), directory=True)
    with project_lock(directory, lock_name='.state.lock'), ExitStack() as held:
        selected = []
        with os.scandir(directory) as entries:
            for count, entry in enumerate(entries, 1):
                _check(cancelled)
                if count > MAX_FILES:
                    raise ValueError('The media namespace exceeds its entry allowance.')
                if _JOB.fullmatch(entry.name):
                    selected.append(entry.name)
                    if len(selected) > MAX_JOBS:
                        raise ValueError('The media job count exceeds its snapshot allowance.')
        for job_id in sorted(selected):
            _check(cancelled)
            job = local_path(directory / identity(job_id), root=directory, exists=True)
            regular(job, directory=True)
            held.enter_context(project_lock(job))
            held.enter_context(project_lock(job, lock_name='.control.lock'))
        yield

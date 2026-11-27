"""Inspect explicit local files into the existing ASR manifest schema.

This writes an unapproved draft. It never imports the selected runtime, loads a
model, executes the selected interpreter/worker, downloads files or grants rights.
The same local-file policy and supplied-worker preparation are reused; actual
admission remains SuppliedWhisperRuntime.verify after separate review.
"""
from dataclasses import dataclass
from hashlib import sha256
import os
from pathlib import Path
import re

from .models import AudioCancelled, integer
from .supplied_whisper import prepare
from .transcription import _RUNTIME_PACKAGES, _bounded_bytes
from ..durable_state import atomic_json, encode, local_path, regular


@dataclass(frozen=True)
class ManifestBudget:
    max_files: int = 100000
    max_entries: int = 150000
    max_bytes: int = 64 * 1024**3

    def __post_init__(self):
        integer(self.max_files, 'Manifest file allowance', 1, 100000)
        integer(self.max_entries, 'Manifest directory allowance', 1, 150000)
        integer(self.max_bytes, 'Manifest byte allowance', 1, 2**63 - 1)


def _check(cancelled):
    if cancelled():
        raise AudioCancelled('Local manifest inspection cancelled; no draft was published.')


def _stamp(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _hash_snapshot(path, usage, budget, cancelled):
    _check(cancelled)
    path = local_path(path, exists=True)
    before = regular(path)
    usage['files'] += 1
    usage['bytes'] += before.st_size
    if usage['files'] > budget.max_files:
        raise ValueError('The selected installation exceeds the manifest file allowance.')
    if usage['bytes'] > budget.max_bytes:
        raise ValueError('The selected installation exceeds the manifest byte allowance.')
    digest, count = sha256(), 0
    with path.open('rb') as stream:
        if _stamp(os.fstat(stream.fileno())) != _stamp(before):
            raise ValueError('An installation file changed before inspection.')
        while block := stream.read(min(1024 * 1024, before.st_size - count + 1)):
            _check(cancelled)
            count += len(block)
            if count > before.st_size:
                raise ValueError('An installation file grew during inspection.')
            digest.update(block)
        if _stamp(os.fstat(stream.fileno())) != _stamp(before):
            raise ValueError('An installation file changed during inspection.')
    if count != before.st_size or _stamp(regular(path)) != _stamp(before):
        raise ValueError('An installation file changed during inspection.')
    return digest.hexdigest()


def _snapshot_inventory(root, usage, budget, cancelled, *, runtime=False):
    root = local_path(root, exists=True)
    regular(root, directory=True)
    pending, paths, directories = [root], [], {}
    while pending:
        _check(cancelled)
        directory = pending.pop()
        directories[directory] = _stamp(regular(directory, directory=True))
        with os.scandir(directory) as entries:
            for entry in entries:
                _check(cancelled)
                usage['entries'] += 1
                if usage['entries'] > budget.max_entries:
                    raise ValueError('The selected installation exceeds the manifest directory allowance.')
                path = Path(entry.path)
                # Match the canonical runtime inventory: cache directories do
                # not execute under the isolated fresh pycache prefix; loose
                # bytecode and all links/reparse points are rejected.
                if runtime and entry.name == '__pycache__':
                    regular(path, directory=True)
                    continue
                if runtime and path.suffix == '.pyc':
                    raise ValueError('Standalone Python bytecode is not an accepted runtime file.')
                if entry.is_dir(follow_symlinks=False):
                    regular(path, directory=True)
                    pending.append(path)
                else:
                    regular(path)
                    name = path.relative_to(root).as_posix()
                    if '\\' in name or ':' in name:
                        raise ValueError('An installation filename is unsafe for the runtime manifest.')
                    paths.append((name, path))
                    if len(paths) + usage['files'] > budget.max_files:
                        raise ValueError('The selected installation exceeds the manifest file allowance.')
    files = {name: _hash_snapshot(path, usage, budget, cancelled) for name, path in sorted(paths)}
    for directory, stamp in directories.items():
        _check(cancelled)
        if _stamp(regular(directory, directory=True)) != stamp:
            raise ValueError('An installation directory changed during inspection.')
    return files


def write_draft_manifest(output, *, python, worker, runtime_directory, models_directory,
                         model, budget=None, cancelled=lambda: False):
    """Write a new unapproved manifest; both review flags are always false."""
    budget = budget or ManifestBudget()
    if not isinstance(budget, ManifestBudget):
        raise TypeError('Use a validated installation inspection budget.')
    _check(cancelled)
    if not isinstance(model, str) or not re.fullmatch('[a-zA-Z0-9_-]{1,100}', model):
        raise ValueError('Select one simple local model directory name.')
    output = local_path(output)
    python, worker = local_path(python, exists=True), local_path(worker, exists=True)
    runtime = local_path(runtime_directory, exists=True)
    models = local_path(models_directory, exists=True)
    regular(runtime, directory=True)
    regular(models, directory=True)
    if output.is_relative_to(runtime) or output.is_relative_to(models):
        raise ValueError('Save the draft manifest outside the runtime and model directories.')
    if os.path.lexists(output):
        raise FileExistsError('The draft destination already exists; choose a new filename.')
    # Preparation parses only the exact approved source bytes; it does not
    # execute or import the supplied worker or its optional dependencies.
    source_bytes = _bounded_bytes(worker, maximum=128 * 1024)
    prepare(source_bytes)
    _check(cancelled)
    usage = {'files': 0, 'entries': 0, 'bytes': 0}
    python_hash = _hash_snapshot(python, usage, budget, cancelled)
    if _hash_snapshot(worker, usage, budget, cancelled) != sha256(source_bytes).hexdigest():
        raise ValueError('The supplied worker changed during inspection.')
    runtime_files = _snapshot_inventory(runtime, usage, budget, cancelled, runtime=True)
    model_files = _snapshot_inventory(models / model, usage, budget, cancelled)
    if any(name + '/__init__.py' not in runtime_files for name in _RUNTIME_PACKAGES):
        raise ValueError('The draft inventory is missing a required local ASR dependency.')
    if not {'model.bin', 'config.json', 'tokenizer.json'} <= set(model_files):
        raise ValueError('The selected local Whisper model is incomplete.')
    data = {'schema': 1, 'engine': 'supplied-whisper-v43', 'python': str(python),
            'python_sha256': python_hash, 'worker': str(worker),
            'runtime_directory': str(runtime), 'runtime_files': runtime_files,
            'models_directory': str(models), 'model': model, 'model_files': model_files,
            'rights_reviewed': False, 'runtime_reviewed': False,
            'rights_notice': ('REVIEW REQUIRED: inspect exact runtime code/native dependencies and model rights. '
                              'This inventory performs no runtime execution, quality or distribution approval.')}
    encode(data)  # Apply the same bounded canonical JSON encoding before publication.
    _check(cancelled)
    atomic_json(output, data, replace=False)
    return {'status': 'review_required', 'manifest': str(output),
            'runtime_files': len(runtime_files), 'model_files': len(model_files),
            'inspected_bytes': usage['bytes'], 'runtime_reviewed': False, 'rights_reviewed': False}


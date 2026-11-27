"""Selected-model byte identities for the receipts-v1 worker protocol.

This is the public model-inventory contract from the reviewed 7af7d9f adapter
host. It does not import a recognizer, download assets, or qualify model quality.
"""
from hashlib import sha256
import json
import os
from pathlib import Path
import re

from .durable_state import local_path, regular
from .transcription_host import AudioCancelled

MAX_MODEL_BYTES = 32 * 1024**3
MAX_MODEL_FILES = 4096
MAX_MODEL_DIRECTORIES = 10000


def bound_identity(value):
    result = {key: item for key, item in value.items() if key != 'sha256'}
    raw = json.dumps(result, sort_keys=True, ensure_ascii=True, allow_nan=False,
                     separators=(',', ':')).encode('utf-8')
    return {**result, 'sha256': sha256(raw).hexdigest()}


def _stamp(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def model_inventory(models_dir, options, *, cancelled=lambda: False):
    """Bind the complete named local model to bounded, streamed file hashes."""
    if cancelled():
        raise AudioCancelled('Local model verification was cancelled.')
    root = local_path(models_dir, exists=True)
    regular(root, directory=True)
    model = local_path(root / options['model'], root=root, exists=True)
    regular(model, directory=True)
    files, total, directories_seen, revision_bytes = [], 0, 0, None

    def traversal_error(error):
        raise error

    for directory, dirs, names in os.walk(model, followlinks=False, onerror=traversal_error):
        if cancelled():
            raise AudioCancelled('Local model verification was cancelled.')
        regular(local_path(directory, root=model, exists=True), directory=True)
        directories_seen += 1
        if directories_seen > MAX_MODEL_DIRECTORIES:
            raise ValueError('The selected local model exceeds its verification allowance.')
        for name in dirs:
            regular(local_path(Path(directory) / name, root=model, exists=True), directory=True)
        # Bytecode is not part of this receipt protocol's model inventory.
        # Validate directory links before excluding the interpreter cache.
        dirs[:] = sorted(name for name in dirs if name != '__pycache__')
        for name in sorted(names):
            if name.endswith(('.pyc', '.pyo')):
                continue
            if cancelled():
                raise AudioCancelled('Local model verification was cancelled.')
            path = local_path(Path(directory) / name, root=model, exists=True)
            before = regular(path)
            relative = path.relative_to(model).as_posix()
            if (len(files) >= MAX_MODEL_FILES or before.st_size > MAX_MODEL_BYTES
                    or total + before.st_size > MAX_MODEL_BYTES):
                raise ValueError('The selected local model exceeds its verification allowance.')
            if relative == 'revision.txt' and before.st_size > 80:
                raise ValueError('Invalid local model revision marker.')
            digest, count, marker = sha256(), 0, bytearray()
            with path.open('rb') as stream:
                if _stamp(os.fstat(stream.fileno())) != _stamp(before):
                    raise ValueError('Local model bytes changed before verification.')
                while block := stream.read(min(1024**2, before.st_size - count + 1)):
                    if cancelled():
                        raise AudioCancelled('Local model verification was cancelled.')
                    count += len(block)
                    if count > before.st_size:
                        raise ValueError('Local model bytes grew during verification.')
                    digest.update(block)
                    if relative == 'revision.txt':
                        marker.extend(block)
                if _stamp(os.fstat(stream.fileno())) != _stamp(before):
                    raise ValueError('Local model bytes changed during verification.')
            if count != before.st_size or _stamp(regular(path)) != _stamp(before):
                raise ValueError('Local model bytes changed during verification.')
            if relative == 'revision.txt':
                revision_bytes = bytes(marker)
            files.append({'path': relative, 'size_bytes': count, 'sha256': digest.hexdigest()})
            total += count
    names = {item['path'] for item in files}
    if (not {'model.bin', 'config.json', 'tokenizer.json'} <= names
            or not names.intersection({'vocabulary.json', 'vocabulary.txt'})
            or any(item['size_bytes'] == 0 for item in files)):
        raise ValueError('Install the complete selected local model before transcription; nothing will be downloaded.')
    revision = None
    if revision_bytes is not None:
        try:
            revision = revision_bytes.decode('ascii').strip()
        except UnicodeDecodeError as exc:
            raise ValueError('Invalid local model revision marker.') from exc
        if not re.fullmatch('[0-9a-f]{40}', revision):
            raise ValueError('Invalid local model revision marker.')
    if options['model_revision'] is not None and revision != options['model_revision']:
        raise ValueError('The installed local model does not match the requested exact revision.')
    return bound_identity({'selected': options['model'], 'requested_revision': options['model_revision'],
                           'resolved_revision': revision, 'files': sorted(files, key=lambda item: item['path'])})

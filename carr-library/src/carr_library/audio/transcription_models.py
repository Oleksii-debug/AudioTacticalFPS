"""Register an explicitly reviewed, already installed Whisper model in place.

This writes a small identity manifest. It neither downloads nor copies model
weights, and it does not turn the user's local licence decision into permission
to redistribute the model with the application.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile

from .jobs import publish_new
from .model_manifest import (LocalModelManifest, MAX_MODEL_DIRECTORY_ENTRIES,
                             MAX_MODEL_FILES, MAX_MODEL_MANIFEST_BYTES,
                             digest_file, local_path)


MANIFEST_NAME = 'resonance-asr-model.json'
REQUIRED_FILES = ('model.bin', 'config.json', 'tokenizer.json')


def register_local_model(directory, *, model_id, license_name, license_accepted=False,
                         trusted_weights=False, private_root=None,
                         cancelled=lambda: False, on_status=lambda message: None):
    """Seal an existing CTranslate2 model's bytes without replacing any file.

    The revision is the SHA256 of the complete ordered file inventory. A publisher
    revision is not invented. Registration can precede optional runtime installation;
    the session independently records and verifies the runtime it actually executes.
    """
    if license_accepted is not True or trusted_weights is not True:
        raise ValueError('Review the model licence and its trusted source before registration.')
    for value, label in ((model_id, 'model name'), (license_name, 'model licence')):
        if (not isinstance(value, str) or not value.strip() or len(value) > 512
                or any(ord(char) < 32 for char in value)):
            raise ValueError(f'Enter a valid {label}.')
    root = local_path(directory)
    if not root.is_dir():
        raise ValueError('Choose the existing folder containing the CTranslate2 model.')
    if private_root is not None and root.is_relative_to(Path(private_root).resolve()):
        raise ValueError('Choose a model folder outside the private library data.')
    target = root / MANIFEST_NAME
    if target.exists() or target.is_symlink():
        raise FileExistsError('This folder already has a Resonance ASR manifest. Select that manifest instead.')
    for name in REQUIRED_FILES:
        candidate = local_path(root / name)
        if not candidate.is_file() or candidate.stat().st_size == 0:
            raise ValueError(f'The local model requires a nonempty {name}; no download is performed.')
    inventory, total, count = {}, 0, 0
    pending = [root]
    while pending:
        folder = pending.pop()
        with os.scandir(folder) as entries:
            for entry in entries:
                if cancelled():
                    raise InterruptedError('Model registration cancelled; no manifest was published.')
                count += 1
                if count > MAX_MODEL_DIRECTORY_ENTRIES:
                    raise ValueError('The model folder exceeds the bounded inventory size.')
                path = local_path(entry.path)
                info = path.stat()
                if stat.S_ISDIR(info.st_mode):
                    pending.append(path)
                    continue
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError('The model folder must contain only regular files and directories.')
                if len(inventory) >= MAX_MODEL_FILES:
                    raise ValueError('The model contains too many files.')
                total += info.st_size
                if total > 64 * 1024 ** 3:
                    raise ValueError('The model exceeds the local asset byte budget.')
                name = path.relative_to(root).as_posix()
                on_status(f'Verifying local model file {len(inventory) + 1}: {name}')
                inventory[name] = digest_file(path, cancelled=cancelled)
    ordered = dict(sorted(inventory.items()))
    revision = hashlib.sha256(json.dumps(ordered, sort_keys=True,
        separators=(',', ':')).encode('utf-8')).hexdigest()
    raw = {'schema_version': 1, 'engine': 'faster-whisper', 'model_id': model_id.strip(),
           'revision': revision, 'license': license_name.strip(), 'files': ordered,
           'settings': {}, 'license_accepted': True, 'trusted_weights': True}
    payload = (json.dumps(raw, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode('utf-8')
    if len(payload) > MAX_MODEL_MANIFEST_BYTES:
        raise ValueError('The model manifest exceeds its byte budget.')
    descriptor, temporary = tempfile.mkstemp(prefix='.asr-manifest-', dir=root)
    published = False
    cleanup_pending = None
    try:
        with os.fdopen(descriptor, 'wb') as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        LocalModelManifest.load(temporary, engine='faster-whisper')
        if cancelled():
            raise InterruptedError('Model registration cancelled; no manifest was published.')
        for name, expected in ordered.items():
            if digest_file(root / name, cancelled=cancelled) != expected:
                raise ValueError('The model changed while its manifest was being prepared.')
        if cancelled():
            raise InterruptedError('Model registration cancelled; no manifest was published.')
        publish_new(temporary, target)
        published = True
    finally:
        try:
            Path(temporary).unlink(missing_ok=True)
        except OSError:
            if published:
                cleanup_pending = str(temporary)
            else:
                raise
    manifest = LocalModelManifest.load(target, engine='faster-whisper')
    return {'manifest': str(target), 'manifest_sha256': manifest.manifest_sha256,
            'model_revision': revision, 'files': len(ordered), 'bytes': total,
            'network_access': False, 'weights_copied': False,
            'status': 'cleanup_required' if cleanup_pending else 'complete',
            'cleanup_pending': cleanup_pending}

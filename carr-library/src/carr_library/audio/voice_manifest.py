"""Explicit, checksum-verified voice assets; no downloader or remote fallback.

A manifest is an installation decision, not an instruction embedded in a book.
Model licenses and consent must be reviewed before setting the acceptance flags.
Hash verification establishes identity, not that a pickle/model is safe to execute.
"""
# Selectively adapted from public PR127 commit 68777f9f36b142ed1ce847ee47d82e20d0bdd132.
# Current held-source and library authority boundaries are used throughout.
# Public adapter/validator code only; no private worker, model or voice assets.
from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import stat
from typing import Any


class ModelManifestError(ValueError):
    """An optional local model has not passed the installation boundary."""


def local_path(path: Path | str, *, must_exist: bool = True) -> Path:
    """Use current local-path/no-link checks and explicit portable path syntax."""
    from ..durable_state import local_path as selected_path
    raw = os.fspath(path)
    if (type(raw) is not str or not raw or '://' in raw
            or any(ord(char) < 32 for char in raw)
            or raw.startswith(chr(92)) or raw.startswith('//')):
        raise ModelManifestError('Choose an explicit local model path.')
    candidate = Path(raw).absolute()
    for part in candidate.parts[1:]:
        if (':' in part or part.rstrip(' .') != part
                or PureWindowsPath(part).is_reserved()):
            raise ModelManifestError('Model paths cannot contain alternate streams or reserved components.')
    return selected_path(candidate, exists=must_exist)


def _stamp(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns, getattr(info, 'st_file_attributes', 0))


def _read_file(path, *, cancelled, maximum=None, collect=False):
    from ..catalog.intake import _open_file_source, _stream_change_time
    from ..durable_state import regular
    if cancelled():
        raise InterruptedError('Local asset verification cancelled.')
    path = local_path(path)
    before = regular(path)
    if maximum is not None and before.st_size > maximum:
        raise ModelManifestError('The selected local file exceeds its byte budget.')
    limit = before.st_size
    result, chunks, count = hashlib.sha256(), [], 0
    with _open_file_source(path) as stream:
        if _stamp(os.fstat(stream.fileno())) != _stamp(before):
            raise ModelManifestError('The selected local file changed while opening.')
        change_time = _stream_change_time(stream)
        while True:
            if cancelled():
                raise InterruptedError('Local asset verification cancelled.')
            block = stream.read(min(1024 * 1024, limit - count + 1))
            if not block:
                break
            count += len(block)
            if count > limit:
                raise ModelManifestError('The selected local file grew while reading.')
            result.update(block)
            if collect:
                chunks.append(block)
        if (count != limit or _stamp(os.fstat(stream.fileno())) != _stamp(before)
                or _stamp(regular(path)) != _stamp(before)
                or _stream_change_time(stream) != change_time):
            raise ModelManifestError('The selected local file changed while reading.')
    if cancelled():
        raise InterruptedError('Local asset verification cancelled.')
    return result.hexdigest(), b''.join(chunks) if collect else None


def digest_file(path: Path | str, *, cancelled=lambda: False) -> str:
    return _read_file(path, cancelled=cancelled)[0]


@contextmanager
def held_file(path, *, expected_sha256, maximum, cancelled=lambda: False):
    """Keep the exact selected source readable and nonreplaceable through a child."""
    from ..catalog.intake import _open_file_source, _stream_change_time
    from ..durable_state import regular
    path = local_path(path)
    before = regular(path)
    if (type(maximum) is not int or not 0 <= before.st_size <= maximum
            or type(expected_sha256) is not str or not re.fullmatch('[0-9a-f]{64}', expected_sha256)):
        raise ModelManifestError('A bounded exact source-file review is required.')
    if cancelled():
        raise InterruptedError('Local asset operation cancelled.')
    with _open_file_source(path) as stream:
        if _stamp(os.fstat(stream.fileno())) != _stamp(before):
            raise ModelManifestError('The selected source changed before admission.')
        changed = _stream_change_time(stream)
        hashed, remaining = hashlib.sha256(), before.st_size
        while remaining:
            if cancelled():
                raise InterruptedError('Local asset verification cancelled.')
            block = stream.read(min(1024 * 1024, remaining))
            if not block:
                raise ModelManifestError('The selected source became truncated.')
            remaining -= len(block)
            hashed.update(block)
        if stream.read(1) or hashed.hexdigest() != expected_sha256:
            raise ModelManifestError('The selected source no longer matches its reviewed bytes.')
        def check():
            if (_stamp(os.fstat(stream.fileno())) != _stamp(before)
                    or _stamp(regular(path)) != _stamp(before)
                    or _stream_change_time(stream) != changed):
                raise ModelManifestError('The selected source changed during the owner operation.')
        check()
        try:
            yield path
        finally:
            check()


def _unique_object(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ModelManifestError('A manifest contains a duplicate field.')
        result[key] = value
    return result


def _read_json_snapshot(path, *, max_bytes=1024 * 1024, cancelled=lambda: False):
    if type(max_bytes) is not int or not 1 <= max_bytes <= 1024 * 1024:
        raise ModelManifestError('Choose a bounded local JSON document.')
    digest, data = _read_file(path, cancelled=cancelled, maximum=max_bytes, collect=True)
    def reject_constant(value):
        raise ModelManifestError('Nonfinite numbers are not permitted in a manifest.')
    value = json.loads(data.decode('utf-8-sig'), object_pairs_hook=_unique_object,
                       parse_constant=reject_constant)
    return value, digest


def read_json(path: Path | str, *, max_bytes: int = 1024 * 1024):
    return _read_json_snapshot(path, max_bytes=max_bytes)[0]


def _text(value, label, *, maximum=512):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or '\x00' in value:
        raise ModelManifestError(f'Invalid {label}.')
    return value


def _relative(value):
    _text(value, 'asset path', maximum=2048)
    path = PurePosixPath(value)
    if (path.is_absolute() or '\\' in value or ':' in value or
            any(p in ('', '.', '..') for p in value.split('/')) or
            any(ord(c) < 32 for c in value)):
        raise ModelManifestError('Model assets must use simple relative paths.')
    return path


@dataclass(frozen=True)
class LocalModelManifest:
    path: Path
    engine: str
    model_id: str
    revision: str
    license: str
    files: tuple[tuple[str, str], ...]
    settings: dict[str, Any]
    license_accepted: bool
    trusted_weights: bool
    personal_noncommercial: bool
    manifest_sha256: str
    root_identity: tuple[int, int]
    ownership_identity: dict | None = None

    @classmethod
    def load(cls, path, *, engine=None, expected_sha256=None):
        path = local_path(path)
        from ..durable_state import regular
        root_info = regular(path.parent, directory=True)
        root_identity = (root_info.st_dev, root_info.st_ino)
        raw, manifest_sha256 = _read_json_snapshot(path)
        if not isinstance(raw, dict) or type(raw.get('schema_version')) is not int or raw.get('schema_version') != 1:
            raise ModelManifestError('Expected local-model manifest schema version 1.')
        known = {'schema_version', 'engine', 'model_id', 'revision', 'license', 'files',
                 'settings', 'license_accepted', 'trusted_weights', 'personal_noncommercial'}
        if set(raw) - known:
            raise ModelManifestError('Unsupported model manifest fields.')
        actual_engine = _text(raw.get('engine'), 'engine')
        if actual_engine not in {'piper', 'dordiai-voices'}:
            raise ModelManifestError('Unsupported local model engine.')
        if engine is not None and actual_engine != engine:
            raise ModelManifestError('The model manifest belongs to another engine.')
        revision = _text(raw.get('revision'), 'model revision')
        if not re.fullmatch('[0-9a-f]{40}|[0-9a-f]{64}', revision):
            raise ModelManifestError('Pin an exact model revision or SHA256, not a moving branch.')
        license_name = _text(raw.get('license'), 'model license')
        files = raw.get('files')
        if not isinstance(files, dict) or not 1 <= len(files) <= 8192:
            raise ModelManifestError('List every required local asset and its SHA256.')
        for name, digest in files.items():
            _relative(name)
            if not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest):
                raise ModelManifestError('Each model asset requires an exact SHA256.')
        for flag in ('license_accepted', 'trusted_weights', 'personal_noncommercial'):
            if flag in raw and type(raw[flag]) is not bool:
                raise ModelManifestError('Manifest acceptance flags must be booleans.')
        settings = raw.get('settings', {})
        if not isinstance(settings, dict):
            raise ModelManifestError('Model settings must be an object.')
        self_root = regular(path.parent, directory=True)
        if (self_root.st_dev, self_root.st_ino) != root_identity:
            raise ModelManifestError('The selected runtime directory changed during review.')
        if expected_sha256 is not None and manifest_sha256 != expected_sha256:
            raise ModelManifestError('The local model manifest has changed.')
        return cls(path, actual_engine, _text(raw.get('model_id'), 'model identity'),
                   revision, license_name, tuple(sorted(files.items())), settings,
                   raw.get('license_accepted', False), raw.get('trusted_weights', False),
                   raw.get('personal_noncommercial', False), manifest_sha256, root_identity)

    @property
    def root(self):
        return self.path.parent

    def check_root(self):
        from ..durable_state import regular
        selected = local_path(self.root)
        info = regular(selected, directory=True)
        if selected != self.root or (info.st_dev, info.st_ino) != self.root_identity:
            raise ModelManifestError('The selected runtime directory changed; review it again.')

    def asset(self, name: str) -> Path:
        self.check_root()
        _relative(name)
        if name not in dict(self.files):
            raise ModelManifestError('The requested asset is not in the verified manifest.')
        path = local_path(self.root.joinpath(*PurePosixPath(name).parts))
        if not path.is_relative_to(self.root):
            raise ModelManifestError('A model asset escapes its installation directory.')
        return path

    def verify(self, *, cancelled=lambda: False, max_asset_bytes=64 * 1024**3,
               require_complete=False, ownership=None):
        self.check_root()
        if ownership is not None:
            from .voice_ownership import VoiceOwnershipLease
            if (type(ownership) is not VoiceOwnershipLease or not ownership.held
                    or ownership.ticket.runtime != self.root):
                raise ModelManifestError('Use the actual held owner lease for this runtime.')
            ownership.checkpoint()
        if not self.license_accepted:
            raise ModelManifestError('Review and accept this local model license before use.')
        if self.engine == 'dordiai-voices' and not self.trusted_weights:
            raise ModelManifestError('Review the exact trusted dordiai-voices model weights before execution.')
        if type(max_asset_bytes) is not int or max_asset_bytes < 1:
            raise ModelManifestError('Invalid local asset verification budget.')
        if digest_file(self.path, cancelled=cancelled) != self.manifest_sha256:
            raise ModelManifestError('The model manifest changed after it was loaded.')
        if require_complete:
            listed = dict(self.files)
            for count, entry in enumerate(self.root.rglob('*'), 1):
                if cancelled():
                    raise InterruptedError('Local asset verification cancelled.')
                if count > 32768:
                    raise ModelManifestError('The model installation exceeds the directory entry budget.')
                entry = local_path(entry)
                if entry.is_file() and entry != self.path and entry.relative_to(self.root).as_posix() not in listed:
                    raise ModelManifestError('The local runtime contains a file missing from its reviewed manifest.')
        total = 0
        for name, expected in self.files:
            path = self.asset(name)
            total += path.stat().st_size
            if total > max_asset_bytes:
                raise ModelManifestError('Local assets exceed the selected verification budget.')
            actual = (ownership.lock_sha256() if ownership is not None
                      and path == ownership.ticket.lock else digest_file(path, cancelled=cancelled))
            if actual != expected:
                raise ModelManifestError(f'Checksum mismatch for local asset: {name}')
        self.check_root()
        if digest_file(self.path, cancelled=cancelled) != self.manifest_sha256:
            raise ModelManifestError('The model manifest changed during asset verification.')
        return {'files': len(self.files), 'bytes': total, 'manifest_sha256': self.manifest_sha256}

    def cache_identity(self):
        # Do not publish machine paths or private voice names in cache metadata.
        return {'engine': self.engine, 'model_id': self.model_id, 'revision': self.revision,
                'license': self.license, 'manifest_sha256': self.manifest_sha256}


def offline_environment():
    """Offline flags complement the fixed worker's socket audit restrictions."""
    result = dict(os.environ)
    for key in list(result):
        if (key.upper() in {'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY'} or
                any(part in key.upper() for part in ('TOKEN', 'API_KEY', 'ACCESS_KEY', 'SECRET', 'PASSWORD', 'CREDENTIAL'))):
            result.pop(key, None)
    result.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', HF_DATASETS_OFFLINE='1',
                  HF_HUB_DISABLE_TELEMETRY='1', DO_NOT_TRACK='1', TOKENIZERS_PARALLELISM='false')
    return result

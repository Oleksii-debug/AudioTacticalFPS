"""Explicit, checksum-verified local assets; no downloader or remote fallback.

A manifest is an installation decision, not an instruction embedded in a book.
Model licenses and consent must be reviewed before setting the acceptance flags.
Hash verification establishes identity, not that a pickle/model is safe to execute.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Any

from .paths import filename_component
from .jobs import _open_json_file


# A normal reviewed CPU Torch wheel already contains more than 10,000 files.
# Keep explicit byte, file and traversal budgets while permitting its complete
# inventory together with the interpreter and other inference dependencies.
MAX_MODEL_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_MODEL_FILES = 40960
MAX_MODEL_DIRECTORY_ENTRIES = 65536


class ModelManifestError(ValueError):
    """An optional local model has not passed the installation boundary."""


def local_path(path: Path | str, *, must_exist: bool = True) -> Path:
    """Reject network paths and links before resolving a local filesystem path."""
    if str(path).startswith(('\\\\', '//')):
        raise ModelManifestError('Choose a local path, not a network share.')
    path = Path(path).absolute()
    raw = str(path)
    if raw.startswith(('\\\\', '//')) or '\x00' in raw:
        raise ModelManifestError('Choose a local path, not a network share.')
    if any(':' in part for part in path.parts[1:]):
        raise ModelManifestError('Alternate streams and URL paths are not supported.')
    try:
        for name in path.parts[1:]:
            filename_component(name)
    except ValueError as exc:
        raise ModelManifestError(str(exc)) from exc
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ModelManifestError('Linked paths are not accepted for local engine assets.')
        try:
            attributes = getattr(part.lstat(), 'st_file_attributes', 0)
        except FileNotFoundError:
            continue
        if attributes & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400):
            raise ModelManifestError('Reparse points are not accepted for local engine assets.')
    if must_exist and not path.exists():
        raise FileNotFoundError(path)
    return path.resolve(strict=must_exist)


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _digest_snapshot(path, *, cancelled=lambda: False, max_bytes=None):
    if max_bytes is not None and (type(max_bytes) is not int or max_bytes < 0):
        raise ModelManifestError('Invalid local asset verification budget.')
    if cancelled():
        raise InterruptedError('Local asset verification cancelled.')
    path = local_path(path)
    before = path.stat()
    if not stat.S_ISREG(before.st_mode):
        raise ModelManifestError('Expected a regular file.')
    if max_bytes is not None and before.st_size > max_bytes:
        raise ModelManifestError('Local assets exceed the selected verification budget.')
    result = hashlib.sha256()
    count = 0
    with path.open('rb') as stream:
        if _identity(os.fstat(stream.fileno())) != _identity(before):
            raise ModelManifestError('A local asset changed before verification.')
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            if cancelled():
                raise InterruptedError('Local asset verification cancelled.')
            count += len(block)
            if max_bytes is not None and count > max_bytes:
                raise ModelManifestError('Local assets exceed the selected verification budget.')
            result.update(block)
        if _identity(os.fstat(stream.fileno())) != _identity(before):
            raise ModelManifestError('A local asset changed during verification.')
    after = path.stat()
    if count != before.st_size or _identity(before) != _identity(after):
        raise ModelManifestError('A local asset changed during verification.')
    if cancelled():
        raise InterruptedError('Local asset verification cancelled.')
    return result.hexdigest(), count


def digest_file(path: Path | str, *, cancelled=lambda: False, max_bytes=None) -> str:
    return _digest_snapshot(path, cancelled=cancelled, max_bytes=max_bytes)[0]


def _unique_object(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ModelManifestError('A manifest contains a duplicate field.')
        result[key] = value
    return result


def _read_json_snapshot(path, *, max_bytes=1024 * 1024):
    if type(max_bytes) is not int or max_bytes < 1:
        raise ModelManifestError('Invalid local manifest byte budget.')
    path = local_path(path)
    before = path.stat()
    if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
        raise ModelManifestError('The local manifest is not a bounded regular file.')
    with _open_json_file(path) as stream:
        if _identity(os.fstat(stream.fileno())) != _identity(before):
            raise ModelManifestError('The local manifest changed before reading.')
        data = stream.read(max_bytes + 1)
        if _identity(os.fstat(stream.fileno())) != _identity(before):
            raise ModelManifestError('The local manifest changed while reading its snapshot.')
    if len(data) > max_bytes:
        raise ModelManifestError('The local manifest exceeds the byte budget.')
    if len(data) != before.st_size or _identity(path.stat()) != _identity(before):
        raise ModelManifestError('The local manifest changed while reading its snapshot.')
    def reject_constant(value):
        raise ModelManifestError('Nonfinite numbers are not permitted in a manifest.')
    value = json.loads(data.decode('utf-8-sig'), object_pairs_hook=_unique_object,
                       parse_constant=reject_constant)
    return value, hashlib.sha256(data).hexdigest()


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
    try:
        for name in path.parts:
            filename_component(name)
    except ValueError as exc:
        raise ModelManifestError(str(exc)) from exc
    return path


def _complete_inventory(root, manifest_path, *, cancelled):
    """Enumerate every directory or refuse the complete-runtime claim.

    pathlib.rglob can suppress an unreadable subtree. The current manifest
    authority instead uses bounded scandir iteration, retaining the canonical
    fail-closed traversal behavior without adding another model framework.
    """
    found, pending, count = set(), [root], 0

    def check_cancelled():
        if cancelled():
            raise InterruptedError('Local asset verification cancelled.')

    try:
        while pending:
            check_cancelled()
            directory = local_path(pending.pop())
            if not stat.S_ISDIR(directory.stat().st_mode):
                raise ModelManifestError('The model inventory requires regular local directories.')
            with os.scandir(directory) as entries:
                for entry in entries:
                    check_cancelled()
                    count += 1
                    if count > MAX_MODEL_DIRECTORY_ENTRIES:
                        raise ModelManifestError('The model installation exceeds the directory entry budget.')
                    path = local_path(directory / entry.name)
                    info = path.stat()
                    if stat.S_ISDIR(info.st_mode):
                        pending.append(path)
                    elif stat.S_ISREG(info.st_mode):
                        if path != manifest_path:
                            found.add(path.relative_to(root).as_posix())
                    else:
                        raise ModelManifestError('Model inventories contain only regular files and directories.')
        check_cancelled()
    except InterruptedError:
        raise
    except OSError as error:
        raise ModelManifestError('The local runtime inventory is unreadable or incomplete.') from error
    return found


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

    @classmethod
    def load(cls, path, *, engine=None, expected_sha256=None):
        path = local_path(path)
        raw, manifest_sha256 = _read_json_snapshot(path, max_bytes=MAX_MODEL_MANIFEST_BYTES)
        if not isinstance(raw, dict) or type(raw.get('schema_version')) is not int or raw.get('schema_version') != 1:
            raise ModelManifestError('Expected local-model manifest schema version 1.')
        known = {'schema_version', 'engine', 'model_id', 'revision', 'license', 'files',
                 'settings', 'license_accepted', 'trusted_weights', 'personal_noncommercial'}
        if set(raw) - known:
            raise ModelManifestError('Unsupported model manifest fields.')
        actual_engine = _text(raw.get('engine'), 'engine')
        if actual_engine not in {'musicgen', 'piper', 'dordiai-voices', 'ace-step', 'fluidsynth', 'supplied-whisper'}:
            raise ModelManifestError('Unsupported local model engine.')
        if engine is not None and actual_engine != engine:
            raise ModelManifestError('The model manifest belongs to another engine.')
        revision = _text(raw.get('revision'), 'model revision')
        if not re.fullmatch('[0-9a-f]{40}|[0-9a-f]{64}', revision):
            raise ModelManifestError('Pin an exact model revision or SHA256, not a moving branch.')
        license_name = _text(raw.get('license'), 'model license')
        files = raw.get('files')
        if not isinstance(files, dict) or not 1 <= len(files) <= MAX_MODEL_FILES:
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
        if expected_sha256 is not None and manifest_sha256 != expected_sha256:
            raise ModelManifestError('The local model manifest has changed.')
        return cls(path, actual_engine, _text(raw.get('model_id'), 'model identity'),
                   revision, license_name, tuple(sorted(files.items())), settings,
                   raw.get('license_accepted', False), raw.get('trusted_weights', False),
                   raw.get('personal_noncommercial', False), manifest_sha256)

    @property
    def root(self):
        return self.path.parent

    def asset(self, name: str) -> Path:
        return self._asset(name, dict(self.files))

    def directory(self, name: str) -> Path:
        """Select an installed package directory covered by this inventory."""
        relative = _relative(name)
        path = local_path(self.root.joinpath(*relative.parts))
        if (not path.is_dir() or not path.is_relative_to(self.root)
                or not any(asset.startswith(name + '/') for asset, _ in self.files)):
            raise ModelManifestError('The package directory is not covered by the reviewed manifest.')
        return path

    def _asset(self, name: str, inventory: dict[str, str]) -> Path:
        _relative(name)
        if name not in inventory:
            raise ModelManifestError('The requested asset is not in the verified manifest.')
        path = local_path(self.root.joinpath(*PurePosixPath(name).parts))
        if not path.is_relative_to(self.root):
            raise ModelManifestError('A model asset escapes its installation directory.')
        return path

    def verify(self, *, cancelled=lambda: False, max_asset_bytes=64 * 1024**3,
               require_complete=False):
        if not self.license_accepted:
            raise ModelManifestError('Review and accept this local model license before use.')
        if self.engine == 'ace-step' and not self.trusted_weights:
            raise ModelManifestError('Review the exact ACE-Step checkpoints and installed code before execution.')
        if self.engine == 'supplied-whisper' and not self.trusted_weights:
            raise ModelManifestError('Review the supplied transcription worker, interpreter and exact local model before execution.')
        if self.engine == 'musicgen':
            if self.license != 'CC-BY-NC-4.0' or not self.personal_noncommercial:
                raise ModelManifestError('MusicGen weights require the personal noncommercial profile.')
            if not self.trusted_weights:
                raise ModelManifestError('Review the exact trusted MusicGen checkpoint before execution.')
            for name in ('state_dict.bin', 'compression_state_dict.bin'):
                if name not in dict(self.files):
                    raise ModelManifestError('Both MusicGen checkpoints must be installed and pinned.')
        if type(max_asset_bytes) is not int or max_asset_bytes < 1:
            raise ModelManifestError('Invalid local asset verification budget.')
        if digest_file(self.path, cancelled=cancelled, max_bytes=MAX_MODEL_MANIFEST_BYTES) != self.manifest_sha256:
            raise ModelManifestError('The model manifest changed after it was loaded.')
        listed = dict(self.files)
        if require_complete:
            actual = _complete_inventory(self.root, self.path, cancelled=cancelled)
            if actual - set(listed):
                raise ModelManifestError('The local runtime contains a file missing from its reviewed manifest.')
            if actual != set(listed):
                raise ModelManifestError('The reviewed runtime file inventory differs from its complete installation.')
        total = 0
        for name, expected in self.files:
            path = self._asset(name, listed)
            actual, size = _digest_snapshot(path, cancelled=cancelled,
                                           max_bytes=max_asset_bytes - total)
            total += size
            if actual != expected:
                raise ModelManifestError(f'Checksum mismatch for local asset: {name}')
        return {'files': len(self.files), 'bytes': total, 'manifest_sha256': self.manifest_sha256}

    def cache_identity(self):
        # Do not publish machine paths or private voice names in cache metadata.
        return {'engine': self.engine, 'model_id': self.model_id, 'revision': self.revision,
                'license': self.license, 'manifest_sha256': self.manifest_sha256}


def offline_environment():
    """Offline flags complement the fixed worker's socket audit restrictions."""
    result = dict(os.environ)
    for key in list(result):
        if (key.upper() in {'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'FFREPORT', 'SSLKEYLOGFILE'} or
                any(part in key.upper() for part in ('TOKEN', 'API_KEY', 'ACCESS_KEY', 'SECRET', 'PASSWORD', 'CREDENTIAL'))):
            result.pop(key, None)
    result.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', HF_DATASETS_OFFLINE='1',
                  HF_HUB_DISABLE_TELEMETRY='1', DO_NOT_TRACK='1', TOKENIZERS_PARALLELISM='false')
    return result

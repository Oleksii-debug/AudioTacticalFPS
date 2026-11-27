"""Explicit offline installation of pinned OCR models, with atomic activation.

Models are copied from a user-selected local folder, never downloaded. Each
selected file must match the reviewed upstream Git blob and size. A SHA256
manifest is committed only after the complete selection passes. Cancellation
leaves earlier installed models active; validated unregistered copies can be
reused by a later explicit retry. No selected file is silently overwritten.
"""
from dataclasses import dataclass
from hashlib import sha1, sha256
import json
import os
from pathlib import Path
import re
import shutil
import stat
from uuid import uuid4

from .model_catalog import (REVISIONS, catalogue_sha256, license_bytes, models,
                            profile_provenance, unique_object)
from .ocr_languages import model_ids


class ModelStoreError(ValueError):
    pass


@dataclass(frozen=True)
class InstallReport:
    installed: tuple
    already_present: tuple
    bytes: int
    profile: str
    revision: str


def default_model_root():
    override = os.environ.get('CARR_OCR_MODELS_DIR')
    if override is not None:
        if not override or not Path(override).is_absolute():
            raise ModelStoreError('CARR_OCR_MODELS_DIR must name an absolute local folder.')
        return Path(override)
    from platformdirs import user_data_path
    return user_data_path('Universal Research Library', appauthor=False, roaming=False) / 'ocr-models'


def _stamp(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _regular(path, *, allow_links=False):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or (not allow_links and info.st_nlink != 1)
            or getattr(info, 'st_file_attributes', 0) & 0x400):
        raise ModelStoreError('OCR model files must be regular files without links.')
    return info


def _directory(path, *, create=False):
    """Check ancestors before resolving or creating, including Windows reparse points."""
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts or str(path).startswith(('//', '\\\\')):
        raise ModelStoreError('OCR models need an absolute local folder.')
    for item in reversed((path, *path.parents)):
        if not os.path.lexists(item):
            if create:
                item.mkdir(mode=0o700)
            else:
                raise FileNotFoundError(item)
        info = item.lstat()
        if not stat.S_ISDIR(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ModelStoreError('OCR model folders cannot be links or reparse points.')
    return path


def _small_read(path, limit, *, allow_links=False):
    before = _regular(path, allow_links=allow_links)
    if before.st_size > limit:
        raise ModelStoreError('OCR manifest exceeds its size limit.')
    with path.open('rb') as stream:
        if _stamp(os.fstat(stream.fileno())) != _stamp(before):
            raise ModelStoreError('OCR manifest changed while opening.')
        raw = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    if (len(raw) != before.st_size or len(raw) > limit or _stamp(after) != _stamp(before)
            or _stamp(_regular(path, allow_links=allow_links)) != _stamp(before)):
        raise ModelStoreError('OCR manifest changed during reading.')
    return raw


def _sync_directory(path):
    if os.name == 'posix':
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _atomic_json(path, value):
    if os.path.lexists(path):
        _regular(path)
    temporary = path.with_name('.manifest-' + uuid4().hex + '.tmp')
    try:
        with temporary.open('xb') as stream:
            stream.write((json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')) + '\n').encode('utf-8'))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _model_digest(path, model, *, output=None, remaining=lambda: None, progress=None,
                  allow_links=False):
    """Stream a stable, exact-size official blob; never load whole packs in RAM."""
    _directory(path.parent)
    before = _regular(path, allow_links=allow_links)
    if before.st_size != model.size:
        raise ModelStoreError(f'OCR model size differs from the catalogue: {model.identifier}.')
    git = sha1(b'blob ' + str(model.size).encode('ascii') + b'\0')
    digest = sha256()
    count = 0
    with path.open('rb') as stream:
        if _stamp(os.fstat(stream.fileno())) != _stamp(before):
            raise ModelStoreError('OCR model changed while opening.')
        while chunk := stream.read(1024 * 1024):
            remaining()
            count += len(chunk)
            if count > model.size:
                raise ModelStoreError('OCR model grew during validation.')
            git.update(chunk)
            digest.update(chunk)
            if output is not None:
                output.write(chunk)
            if progress is not None:
                progress(len(chunk))
        after = os.fstat(stream.fileno())
    if (count != model.size or _stamp(after) != _stamp(before)
            or _stamp(_regular(path, allow_links=allow_links)) != _stamp(before)):
        raise ModelStoreError('OCR model changed during validation.')
    if git.hexdigest() != model.blob_sha1:
        raise ModelStoreError(f'OCR model differs from the pinned official bytes: {model.identifier}.')
    remaining()
    return digest.hexdigest()


def _recover_publication(target, verify):
    """Recover only a proved link to our exact private staging namespace.

    A killed process may leave both names between link() and unlink(). Unknown
    links are never removed. Callers hold the cooperative model-store writer
    lock; this is not a sandbox against other programs of the same user.
    """
    before = _regular(target, allow_links=True)
    if before.st_nlink == 1:
        return
    if before.st_nlink != 2 or not before.st_ino:
        raise ModelStoreError('An OCR model has unaccounted hard links.')
    twins = []
    for path in target.parent.iterdir():
        if not re.fullmatch(r'\.model-[0-9a-f]{32}\.tmp', path.name):
            continue
        info = _regular(path, allow_links=True)
        if os.path.samestat(before, info):
            twins.append((path, info))
    if len(twins) != 1:
        raise ModelStoreError('An OCR model has an unaccounted staging link.')
    verify()
    current = _regular(target, allow_links=True)
    other = _regular(twins[0][0], allow_links=True)
    if (_stamp(before) != _stamp(current) or current.st_nlink != 2
            or _stamp(twins[0][1]) != _stamp(other) or not os.path.samestat(current, other)):
        raise ModelStoreError('OCR staging changed during recovery.')
    twins[0][0].unlink()
    _regular(target)
    _sync_directory(target.parent)


def _license(directory, profile):
    target = directory / 'LICENSE'
    expected = license_bytes(profile)

    def verify(*, allow_links=False):
        if _small_read(target, 32 * 1024, allow_links=allow_links) != expected:
            raise ModelStoreError('The installed OCR license differs from the reviewed source.')

    if os.path.lexists(target):
        _recover_publication(target, lambda: verify(allow_links=True))
        verify()
        return
    temporary = directory / ('.model-' + uuid4().hex + '.tmp')
    try:
        with temporary.open('xb') as stream:
            stream.write(expected)
            stream.flush()
            os.fsync(stream.fileno())
        if _small_read(temporary, 32 * 1024) != expected:
            raise ModelStoreError('The OCR license could not be saved completely.')
        os.link(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    _sync_directory(directory)


@dataclass(frozen=True)
class ModelSelection:
    root: Path
    directory: Path
    profile: str
    revision: str
    model_sha256: tuple

    @property
    def languages(self):
        return tuple(name for name, _ in self.model_sha256)

    def fingerprint(self):
        return {'profile': self.profile, 'revision': self.revision,
                'catalogue_sha256': catalogue_sha256(), 'license': 'Apache-2.0',
                'model_sha256': dict(self.model_sha256)}

    def assert_unchanged(self, remaining=lambda: None):
        current = OcrModelStore(self.root).select(self.profile, '+'.join(self.languages),
                                                 remaining=remaining)
        if current != self:
            raise ModelStoreError('The selected OCR models changed during recognition.')


class OcrModelStore:
    def __init__(self, root=None):
        self.root = Path(root) if root is not None else default_model_root()
        if (not self.root.is_absolute() or '..' in self.root.parts
                or str(self.root).startswith(('//', '\\\\'))):
            raise ModelStoreError('OCR models need an absolute local folder.')

    def _profile(self, profile):
        if not isinstance(profile, str) or profile not in REVISIONS:
            raise ModelStoreError('Choose the Fast or Best OCR model profile.')
        return self.root / (profile + '-' + REVISIONS[profile])

    def _manifest(self, profile):
        directory = self._profile(profile)
        table = {m.identifier: m for m in models(profile)}
        empty = {'schema': 1, 'profile': profile, 'revision': REVISIONS[profile],
                 'catalogue_sha256': catalogue_sha256(), 'models': {}}
        try:
            _directory(directory)
        except FileNotFoundError:
            return empty
        path = directory / 'manifest.json'
        if not os.path.lexists(path):
            return empty
        try:
            data = json.loads(_small_read(path, 512 * 1024), object_pairs_hook=unique_object)
            if (not isinstance(data, dict) or set(data) != set(empty)
                    or type(data['schema']) is not int or data['schema'] != 1
                    or any(data[key] != empty[key] for key in ('profile', 'revision', 'catalogue_sha256'))
                    or not isinstance(data['models'], dict)
                    or not set(data['models']) <= set(table)):
                raise ModelStoreError('The installed OCR model manifest is invalid.')
            for identifier, entry in data['models'].items():
                model = table[identifier]
                if (not isinstance(entry, dict) or set(entry) != {'size', 'sha256', 'blob_sha1'}
                        or type(entry['size']) is not int or entry['size'] != model.size
                        or entry['blob_sha1'] != model.blob_sha1
                        or not isinstance(entry['sha256'], str)
                        or not re.fullmatch('[0-9a-f]{64}', entry['sha256'])):
                    raise ModelStoreError('The installed OCR model identity is invalid.')
            return data
        except (UnicodeError, json.JSONDecodeError, TypeError) as exc:
            raise ModelStoreError('The installed OCR model manifest is unreadable.') from exc

    def available(self, profile):
        """Cheap inventory lookup; selected bytes are verified before every OCR use."""
        return tuple(sorted(self._manifest(profile)['models']))

    def select(self, profile, languages, *, orientation=False, remaining=lambda: None):
        selected = tuple(dict.fromkeys((*model_ids(languages), *(['osd'] if orientation else []))))
        table = {m.identifier: m for m in models(profile)}
        manifest = self._manifest(profile)
        if not selected or not set(selected) <= set(manifest['models']):
            raise ModelStoreError('Install the selected OCR languages and orientation model first.')
        directory = _directory(self._profile(profile) / 'tessdata')
        notice = _small_read(directory.parent / 'LICENSE', 32 * 1024)
        if notice != license_bytes(profile):
            raise ModelStoreError('The installed OCR model license is missing or changed.')
        hashes = []
        for identifier in selected:
            remaining()
            value = _model_digest(directory / (identifier + '.traineddata'), table[identifier],
                                  remaining=remaining)
            if value != manifest['models'][identifier]['sha256']:
                raise ModelStoreError('The installed OCR model checksum changed.')
            hashes.append((identifier, value))
        if self._manifest(profile) != manifest:
            raise ModelStoreError('The OCR model selection changed during validation; retry the operation.')
        return ModelSelection(self.root, directory, profile, REVISIONS[profile], tuple(sorted(hashes)))

    def install(self, profile, source_directory, identifiers, *, cancelled=lambda: False,
                progress=None):
        """Verify and activate one explicit selection. Repeating it is idempotent."""
        directory = self._profile(profile)
        table = {m.identifier: m for m in models(profile)}
        if isinstance(identifiers, str):
            raise ModelStoreError('Choose one or more OCR models from the catalogue.')
        selected = tuple(identifiers)
        if (not selected or len(selected) > len(table) or any(not isinstance(i, str) for i in selected)
                or len(set(selected)) != len(selected) or not set(selected) <= set(table)):
            raise ModelStoreError('Choose one or more distinct OCR models from the catalogue.')
        source = _directory(Path(source_directory))

        def remaining():
            if cancelled():
                raise InterruptedError('OCR model installation cancelled; the previous selection is unchanged.')

        remaining()
        _directory(self.root, create=True)
        lock_path = self.root / '.install.lock'
        if os.path.lexists(lock_path):
            _regular(lock_path)
        from filelock import FileLock
        with FileLock(lock_path, timeout=1):
            remaining()
            manifest = self._manifest(profile)
            previous = tuple(i for i in selected if i in manifest['models'])
            required = sum(table[i].size for i in selected
                           if not os.path.lexists(directory / 'tessdata' / (i + '.traineddata')))
            if shutil.disk_usage(self.root).free < required + 16 * 1024 * 1024:
                raise ModelStoreError('Not enough free space for the selected OCR models and staging files.')
            _directory(directory / 'tessdata', create=True)
            _license(directory, profile)
            total = sum(table[i].size for i in selected)
            completed = copied = 0
            additions = {}
            for identifier in selected:
                remaining()
                model = table[identifier]
                target = directory / 'tessdata' / (identifier + '.traineddata')
                _directory(target.parent, create=True)

                def advance(amount):
                    nonlocal completed
                    completed += amount
                    if progress is not None:
                        progress(identifier, completed, total)

                if os.path.lexists(target):
                    _recover_publication(target, lambda: _model_digest(
                        target, model, remaining=remaining, allow_links=True))
                    value = _model_digest(target, model, remaining=remaining, progress=advance)
                    if identifier in manifest['models'] and value != manifest['models'][identifier]['sha256']:
                        raise ModelStoreError('An installed OCR model changed; no file was overwritten.')
                else:
                    temporary = target.with_name('.model-' + uuid4().hex + '.tmp')
                    try:
                        with temporary.open('xb') as stream:
                            value = _model_digest(source / model.relative_path, model, output=stream,
                                                  remaining=remaining, progress=advance)
                            stream.flush()
                            os.fsync(stream.fileno())
                        remaining()
                        if _model_digest(temporary, model, remaining=remaining) != value:
                            raise ModelStoreError('The OCR model could not be saved completely.')
                        # A Windows in-place write can retain observed metadata.
                        # Recheck source bytes without replaying copy progress.
                        try:
                            source_value = _model_digest(source / model.relative_path, model,
                                                         remaining=remaining)
                        except ModelStoreError as exc:
                            raise ModelStoreError('The source OCR model changed during installation.') from exc
                        if source_value != value:
                            raise ModelStoreError('The source OCR model changed during installation.')
                        # Atomic no-replace publication on the same filesystem.
                        os.link(temporary, target)
                        copied += model.size
                    finally:
                        temporary.unlink(missing_ok=True)
                additions[identifier] = {'size': model.size, 'sha256': value, 'blob_sha1': model.blob_sha1}
            remaining()
            # Recheck every selected published file before activating the batch.
            for identifier in selected:
                value = _model_digest(directory / 'tessdata' / (identifier + '.traineddata'),
                                      table[identifier], remaining=remaining)
                if value != additions[identifier]['sha256']:
                    raise ModelStoreError('An OCR model changed before activation.')
            if self._manifest(profile) != manifest:
                raise ModelStoreError('The OCR model manifest changed during installation; no activation was saved.')
            manifest['models'].update(additions)
            remaining()
            # Model files and directory entries precede the durable manifest.
            for parent in {directory / 'tessdata', directory, self.root,
                           *((directory / 'tessdata' / (i + '.traineddata')).parent for i in selected)}:
                _sync_directory(parent)
            _atomic_json(directory / 'manifest.json', manifest)
            return InstallReport(selected, previous, copied, profile, REVISIONS[profile])

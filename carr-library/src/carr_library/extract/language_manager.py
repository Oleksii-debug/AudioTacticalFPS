"""Install and verify explicitly chosen offline OCR packs; never download them.

The installer contract is retained from PR103. Installed-language discovery stays
in ocr_languages. A separately reviewed manifest digest binds model bytes and the
full license; it is an integrity identity, not an OCR accuracy certification.
"""
from dataclasses import dataclass
import errno
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
from tempfile import TemporaryDirectory

from .ocr_languages import valid_model_code
from .ocr_model_catalog import DEFAULT_MAX_BYTES, _json, _local_path, _sync, _write
from .ocr_runtime import OcrRuntimeError, _read, _stamp, _stat, _unique_object

MANIFEST = 'models.json'
READY = 'PACK-INSTALLED.json'
INCOMPLETE = 'INSTALL-INCOMPLETE.json'
MAX_MANIFEST = 2 * 1024 * 1024
MAX_MODEL = 512 * 1024**2


@dataclass(frozen=True)
class VerifiedModelPack:
    root: Path
    manifest_sha256: str
    upstream: str
    models: tuple[tuple[str, int, str], ...]

    @property
    def languages(self):
        return tuple(code for code, _, _ in self.models)

    def fingerprint(self):
        return {'manifest_sha256': self.manifest_sha256, 'upstream': self.upstream,
                'models': {code: {'size': size, 'sha256': digest}
                           for code, size, digest in self.models}}


def validate_pack_selection(path, digest):
    """Validate serialized configuration without opening files or following links."""
    if path is None and digest is None:
        return
    if (not isinstance(path, str) or not 0 < len(path) <= 4096
            or any(ord(char) < 32 or ord(char) == 127 for char in path)
            or not Path(path).is_absolute() or path.startswith(('//', '\\\\'))
            or (os.name == 'nt' and ':' in path[2:])
            or not isinstance(digest, str) or re.fullmatch('[0-9a-f]{64}', digest) is None):
        raise OcrRuntimeError('Select a local installed OCR pack and its independent SHA256 manifest checksum.')


def _budget(value):
    if type(value) is not int or not 0 < value <= DEFAULT_MAX_BYTES:
        raise ValueError('Choose a positive OCR pack budget of at most 4 GiB.')


def _manifest(root, expected, max_bytes, remaining):
    if not isinstance(expected, str) or re.fullmatch('[0-9a-f]{64}', expected) is None:
        raise OcrRuntimeError('Supply the independently recorded OCR manifest SHA256 checksum.')
    _, digest, raw = _read(root / MANIFEST, MAX_MANIFEST, remaining, retain=True)
    if digest != expected:
        raise OcrRuntimeError('The independently supplied OCR manifest checksum does not match.')
    try:
        data = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Non-finite JSON.')))
    except (UnicodeError, ValueError, RecursionError):
        raise OcrRuntimeError('Invalid offline OCR model manifest.') from None
    if (type(data) is not dict or set(data) != {'schema', 'upstream', 'license', 'models'}
            or type(data['schema']) is not int or data['schema'] != 1
            or not isinstance(data['upstream'], str) or not 0 < len(data['upstream']) <= 4096
            or any(ord(char) < 32 or ord(char) == 127 for char in data['upstream'])
            or not isinstance(data['license'], str) or not data['license'].strip()
            or len(data['license']) > 128 * 1024 or '\x00' in data['license']
            or type(data['models']) is not dict or not 1 <= len(data['models']) <= 1024):
        raise OcrRuntimeError('Unsupported offline OCR model manifest.')
    total, names = 0, set()
    for code, item in data['models'].items():
        if (not valid_model_code(code) or code.casefold() in names
                or type(item) is not dict or set(item) != {'size', 'sha256'}
                or type(item['size']) is not int or not 0 < item['size'] <= MAX_MODEL
                or not isinstance(item['sha256'], str)
                or re.fullmatch('[0-9a-f]{64}', item['sha256']) is None):
            raise OcrRuntimeError('Invalid or ambiguous OCR model identity.')
        total += item['size']
        names.add(code.casefold())
    if total > max_bytes:
        raise OcrRuntimeError('The OCR pack exceeds the selected disk budget.')
    return data, raw


def _companions(manifest, digest):
    return {'LICENSE.txt': manifest['license'].encode('utf-8'),
            READY: _json({'schema': 1, 'manifest_sha256': digest,
                          'upstream': manifest['upstream']})}


def _inventory(root, manifest, *, prepared=False, remaining=lambda: None):
    expected = {MANIFEST, *(code + '.traineddata' for code in manifest['models'])}
    if prepared:
        permitted = expected | {'LICENSE.txt', 'MODEL_PROVENANCE.json'}
    else:
        expected.update(_companions(manifest, ''))
        permitted = expected
    found = set()
    allowed_dirs = {'script'} if any(code.startswith('script/') for code in manifest['models']) else set()
    for directory, dirs, files in os.walk(root, followlinks=False):
        remaining()
        for name in dirs:
            path = Path(directory) / name
            _stat(path, directory=True)
            if path.relative_to(root).as_posix() not in allowed_dirs:
                raise OcrRuntimeError('The OCR pack contains an unlisted directory.')
        for name in files:
            path = Path(directory) / name
            info = _stat(path)
            relative = path.relative_to(root).as_posix()
            if relative not in permitted or len(found) >= 1028:
                raise OcrRuntimeError('The OCR pack contains incomplete or unlisted files.')
            if relative in ('LICENSE.txt', 'MODEL_PROVENANCE.json', READY) and info.st_size > MAX_MANIFEST:
                raise OcrRuntimeError('OCR pack metadata exceeds its size budget.')
            found.add(relative)
    if not expected <= found:
        raise OcrRuntimeError('The OCR pack is incomplete.')


def verify_offline_pack(root, *, expected_manifest_sha256, remaining=lambda: None):
    """Check the complete installed inventory before recognition or cache reuse."""
    root = _local_path(root, exists=True)
    _stat(root, directory=True)
    manifest, raw = _manifest(root, expected_manifest_sha256, DEFAULT_MAX_BYTES, remaining)
    _inventory(root, manifest, remaining=remaining)
    identities = []
    for code, item in sorted(manifest['models'].items()):
        size, digest, _ = _read(root / (code + '.traineddata'), item['size'], remaining)
        if size != item['size'] or digest != item['sha256']:
            raise OcrRuntimeError('An installed OCR model checksum does not match.')
        identities.append((code, size, digest))
    for name, expected in _companions(manifest, expected_manifest_sha256).items():
        _, _, actual = _read(root / name, MAX_MANIFEST, remaining, retain=True)
        if actual != expected:
            raise OcrRuntimeError('The installed OCR license or completion receipt changed.')
    if _read(root / MANIFEST, MAX_MANIFEST, remaining, retain=True)[2] != raw:
        raise OcrRuntimeError('The installed OCR manifest changed during verification.')
    _inventory(root, manifest, remaining=remaining)
    return VerifiedModelPack(root, expected_manifest_sha256, manifest['upstream'], tuple(identities))


def check_pack_destination(value):
    """Never add model files within a sealed portable application/runtime tree."""
    path = _local_path(value)
    for parent in (path.parent, *path.parent.parents):
        if any(os.path.lexists(parent / marker) for marker in ('ocr-manifest.json', 'bundle-manifest.json')):
            raise OcrRuntimeError('Choose an OCR pack directory outside the sealed portable installation.')
    return path


def _copy_model(source, target, item, remaining):
    before = _stat(source)
    if before.st_size != item['size']:
        raise OcrRuntimeError('OCR model size mismatch.')
    descriptor = os.open(source, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0))
    digest, size = sha256(), 0
    with os.fdopen(descriptor, 'rb') as incoming, target.open('xb') as output:
        if _stamp(os.fstat(incoming.fileno())) != _stamp(before):
            raise OcrRuntimeError('The OCR model changed before installation.')
        while True:
            remaining()
            block = incoming.read(min(1024 * 1024, item['size'] - size + 1))
            if not block:
                break
            size += len(block)
            if size > item['size']:
                raise OcrRuntimeError('The OCR model grew during installation.')
            digest.update(block)
            output.write(block)
        if _stamp(os.fstat(incoming.fileno())) != _stamp(before):
            raise OcrRuntimeError('The OCR model changed during installation.')
        output.flush()
        os.fsync(output.fileno())
    if size != item['size'] or digest.hexdigest() != item['sha256'] or _stamp(_stat(source)) != _stamp(before):
        raise OcrRuntimeError('OCR model checksum mismatch.')


def _publish(source, target, remaining):
    try:
        os.link(source, target)
    except OSError as exc:
        if isinstance(exc, FileExistsError) or not (exc.errno in
                {errno.EPERM, errno.EXDEV, errno.ENOSYS, getattr(errno, 'EOPNOTSUPP', errno.ENOSYS)}
                or getattr(exc, 'winerror', None) in (1, 50, 1314)):
            raise
        created = False
        try:
            with target.open('xb') as output, source.open('rb') as incoming:
                created = True
                while block := incoming.read(1024 * 1024):
                    remaining()
                    output.write(block)
                remaining()
                output.flush()
                os.fsync(output.fileno())
        except BaseException:
            if created:
                target.unlink(missing_ok=True)
            raise
    # Completed public models must not retain mutable staging aliases.
    source.unlink()


def install_offline_pack(source, destination, *, expected_manifest_sha256,
                         max_bytes=DEFAULT_MAX_BYTES, cancelled=lambda: False):
    """Install a reviewed prepared pack in a NEW directory, without network use."""
    _budget(max_bytes)

    def remaining():
        if cancelled():
            raise InterruptedError('OCR pack installation cancelled.')

    remaining()
    source = _local_path(source, exists=True)
    _stat(source, directory=True)
    destination = check_pack_destination(destination)
    parent = _local_path(destination.parent, exists=True)
    if destination.is_relative_to(source) or source.is_relative_to(destination):
        raise OcrRuntimeError('Prepared and installed OCR pack directories must be separate.')
    if os.path.lexists(destination):
        raise FileExistsError('Choose a new OCR pack installation directory.')
    manifest, raw = _manifest(source, expected_manifest_sha256, max_bytes, remaining)
    _inventory(source, manifest, prepared=True, remaining=remaining)
    total = sum(item['size'] for item in manifest['models'].values())
    if shutil.disk_usage(parent).free < total + MAX_MANIFEST:
        raise OSError('There is not enough free space for the selected OCR pack.')
    with TemporaryDirectory(prefix='.ocr-install-', dir=parent) as temporary:
        stage = Path(temporary)
        for code, item in sorted(manifest['models'].items()):
            target = stage / (code + '.traineddata')
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            _copy_model(source / (code + '.traineddata'), target, item, remaining)
        _write(stage / MANIFEST, raw)
        for name, content in _companions(manifest, expected_manifest_sha256).items():
            _write(stage / name, content)
        if _read(source / MANIFEST, MAX_MANIFEST, remaining, retain=True)[2] != raw:
            raise OcrRuntimeError('The approved OCR manifest changed during installation.')
        _inventory(source, manifest, prepared=True, remaining=remaining)
        remaining()
        check_pack_destination(destination)
        destination.mkdir(mode=0o700)
        owned, directories = [], []
        try:
            marker = destination / INCOMPLETE
            _write(marker, b'Installation incomplete; do not use this OCR pack.\n')
            owned.append(marker)
            _sync(destination)
            payloads = sorted(path for path in stage.rglob('*') if path.is_file() and path.name != READY)
            for path in [*payloads, stage / READY]:
                remaining()
                target = destination / path.relative_to(stage)
                if target.parent != destination and not target.parent.exists():
                    target.parent.mkdir(mode=0o700)
                    directories.append(target.parent)
                _publish(path, target, remaining)
                owned.append(target)
            for directory in [*directories, destination]:
                _sync(directory)
            marker.unlink()
            owned.remove(marker)
            _sync(destination)
            _sync(parent)
        except BaseException:
            for path in reversed(owned):
                path.unlink(missing_ok=True)
            for directory in reversed(directories):
                try:
                    directory.rmdir()
                except OSError:
                    pass
            try:
                destination.rmdir()
            except OSError:
                pass
            raise
    return {'models': len(manifest['models']), 'codes': tuple(sorted(manifest['models'])),
            'size_bytes': total, 'manifest_sha256': expected_manifest_sha256,
            'path': str(destination)}

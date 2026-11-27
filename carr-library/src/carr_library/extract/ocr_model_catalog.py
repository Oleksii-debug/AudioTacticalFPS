"""Pinned official Tesseract models and explicitly requested offline pack preparation.

The catalogue contains metadata, never weights. Preparing a pack only reads a
user-selected local directory; it does not download, execute or install models.
The output is consumed by language_manager.install_offline_pack. A complete
models.json is published last, after every selected byte has been verified.
"""
from dataclasses import asdict, dataclass
from hashlib import sha1, sha256
from importlib.resources import files
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile

from .ocr_runtime import OcrRuntimeError, _relative, _stamp, _stat, _unique_object

CATALOGUE = 'ocr-model-catalog.json'
MAX_CATALOGUE = 2 * 1024 * 1024
DEFAULT_MAX_BYTES = 4 * 1024**3
CODE = re.compile(r'(?:script/)?[A-Za-z][A-Za-z0-9_]{0,79}')
HEX40 = re.compile(r'[0-9a-f]{40}')
NAMES = {'eng': 'English', 'ukr': 'Ukrainian / Українська', 'slk': 'Slovak / Slovenčina',
         'ces': 'Czech', 'deu': 'German', 'fra': 'French', 'spa': 'Spanish',
         'pol': 'Polish', 'hun': 'Hungarian', 'ita': 'Italian', 'por': 'Portuguese',
         'rus': 'Russian', 'ara': 'Arabic', 'heb': 'Hebrew', 'jpn': 'Japanese',
         'kor': 'Korean', 'chi_sim': 'Chinese, simplified', 'chi_tra': 'Chinese, traditional',
         'osd': 'Orientation and script detection', 'equ': 'Equation detection'}


@dataclass(frozen=True)
class Model:
    profile: str
    code: str
    name: str
    kind: str
    size: int
    git_blob_sha1: str
    path: str
    url: str


@dataclass(frozen=True)
class Source:
    profile: str
    repository: str
    revision: str
    license_text: str
    models: tuple[Model, ...]
    aliases: tuple[tuple[str, str], ...]

    @property
    def upstream(self):
        return f'https://github.com/{self.repository}/tree/{self.revision}'


@dataclass(frozen=True)
class PreparedPack:
    path: str
    profile: str
    revision: str
    models: tuple[str, ...]
    size_bytes: int
    manifest_sha256: str


def _git_hash(raw):
    digest = sha1(usedforsecurity=False)
    digest.update(f'blob {len(raw)}\0'.encode('ascii'))
    digest.update(raw)
    return digest.hexdigest()


def load_source(profile='fast'):
    """Load a packaged, immutable upstream snapshot, with no network access."""
    if profile not in ('fast', 'best'):
        raise ValueError('Choose the fast or best OCR model profile.')
    raw = files(__package__).joinpath(CATALOGUE).read_bytes()
    if len(raw) > MAX_CATALOGUE:
        raise OcrRuntimeError('The OCR catalogue exceeds its metadata budget.')
    data = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object)
    if data.get('schema') != 'carr.ocr-model-catalog.v1':
        raise OcrRuntimeError('Unsupported OCR catalogue schema.')
    source = data['sources'][profile]
    repository = f'tesseract-ocr/tessdata_{profile}'
    if source['repository'] != repository or not HEX40.fullmatch(source['revision']):
        raise OcrRuntimeError('Invalid pinned OCR catalogue source.')
    license_raw = source['license']['text'].encode('utf-8')
    if (source['license']['spdx_id'] != 'Apache-2.0'
            or len(license_raw) != source['license']['size']
            or _git_hash(license_raw) != source['license']['git_blob_sha1']):
        raise OcrRuntimeError('The OCR catalogue license identity does not match.')
    models, paths = [], set()
    for code, value in sorted(source['models'].items()):
        if (not CODE.fullmatch(code) or value['path'] != code + '.traineddata'
                or not HEX40.fullmatch(value['git_blob_sha1'])
                or type(value['size']) is not int or not 0 < value['size'] <= 512 * 1024**2
                or value['kind'] not in ('language', 'script', 'special')):
            raise OcrRuntimeError('Invalid pinned OCR model identity.')
        _relative(value['path'])
        if value['path'].casefold() in paths:
            raise OcrRuntimeError('Ambiguous OCR catalogue path.')
        paths.add(value['path'].casefold())
        name = value.get('name') or NAMES.get(code, code.replace('script/', 'Script: ').replace('_', ' '))
        models.append(Model(profile, code, name, value['kind'], value['size'],
                            value['git_blob_sha1'], value['path'],
                            f'https://raw.githubusercontent.com/{repository}/{source["revision"]}/{value["path"]}'))
    if not models or len(models) > 1024:
        raise OcrRuntimeError('Invalid OCR catalogue model count.')
    aliases = []
    for code, value in source.get('aliases', {}).items():
        target = value.get('resolved_code') or value.get('target_code')
        if target is None:
            target = value.get('target', '').removesuffix('.traineddata')
        if not CODE.fullmatch(code) or target not in source['models']:
            raise OcrRuntimeError('Invalid OCR catalogue alias.')
        aliases.append((code, target))
    return Source(profile, repository, source['revision'], license_raw.decode('utf-8'),
                  tuple(models), tuple(aliases))


def list_models(profile='fast', *, query='', kind=None):
    """Search exact codes, human-readable names and model kinds offline."""
    if kind not in (None, 'language', 'script', 'special'):
        raise ValueError('Unknown OCR model kind.')
    terms = query.casefold().split()
    return tuple(model for model in load_source(profile).models
                 if (kind is None or model.kind == kind)
                 and all(term in f'{model.code} {model.name} {model.kind} {NAMES.get(model.code, "")}'.casefold()
                         for term in terms))


def _selection(source, codes, max_bytes):
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError('Choose a positive OCR pack byte budget.')
    codes = tuple(codes)
    if not codes or len(codes) > 1024 or any(not isinstance(code, str) for code in codes):
        raise ValueError('Choose at least one model from the official catalogue.')
    aliases, by_code = dict(source.aliases), {model.code: model for model in source.models}
    selected = set(aliases.get(code, code) for code in codes)
    if not selected <= by_code.keys():
        raise ValueError('An OCR model is absent from the selected official profile.')
    models = tuple(by_code[code] for code in sorted(selected))
    if sum(model.size for model in models) > max_bytes:
        raise ValueError('The selected OCR models exceed the disk budget.')
    return models


def _local_path(value, *, exists=False):
    value = os.fspath(value)
    if (not value or '\x00' in value or value.startswith(('//', '\\\\'))
            or (os.name == 'nt' and ':' in value[2:])):
        raise ValueError('Choose a local OCR model path.')
    path = Path(value).absolute()
    for parent in reversed((path, *path.parents)):
        if os.path.lexists(parent):
            _stat(parent, directory=parent != path or parent.is_dir())
    return path.resolve(strict=exists)


def _checkpoint(cancelled):
    if cancelled():
        raise InterruptedError('OCR pack preparation cancelled.')


def _copy_verified(incoming, target, model, cancelled):
    before = _stat(incoming)
    if before.st_size != model.size:
        raise OcrRuntimeError(f'OCR model size mismatch: {model.code}.')
    legacy, secure, count = sha1(usedforsecurity=False), sha256(), 0
    legacy.update(f'blob {model.size}\0'.encode('ascii'))
    descriptor = os.open(incoming, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0))
    with os.fdopen(descriptor, 'rb') as read, target.open('xb') as write:
        if _stamp(os.fstat(read.fileno())) != _stamp(before):
            raise OcrRuntimeError('An OCR model changed before verification.')
        while True:
            _checkpoint(cancelled)
            block = read.read(min(1024 * 1024, model.size - count + 1))
            if not block:
                break
            count += len(block)
            if count > model.size:
                raise OcrRuntimeError('An OCR model grew during verification.')
            legacy.update(block)
            secure.update(block)
            write.write(block)
        if _stamp(os.fstat(read.fileno())) != _stamp(before):
            raise OcrRuntimeError('An OCR model changed during verification.')
        write.flush()
        os.fsync(write.fileno())
    if (count != model.size or legacy.hexdigest() != model.git_blob_sha1
            or _stamp(_stat(incoming)) != _stamp(before)):
        raise OcrRuntimeError(f'OCR model does not match the pinned official bytes: {model.code}.')
    return {'size': count, 'sha256': secure.hexdigest()}


def _write(path, raw):
    with path.open('xb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def _json(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                       separators=(',', ':')) + '\n').encode('utf-8')


def _sync(path):
    if os.name != 'nt':
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def prepare_offline_pack(source_directory, destination, codes, *, profile='fast',
                         max_bytes=DEFAULT_MAX_BYTES, cancelled=lambda: False):
    """Verify selected, already-downloaded models and prepare a new offline pack.

    Keep the returned manifest checksum separately for installer review. fast
    and best are separate packs; a profile mismatch fails byte verification.
    Aliases resolve to the official regular model, never to symlink payloads.
    """
    source = load_source(profile)
    models = _selection(source, codes, max_bytes)
    source_directory = _local_path(source_directory, exists=True)
    _stat(source_directory, directory=True)
    destination = _local_path(destination)
    if os.path.lexists(destination):
        raise FileExistsError('Choose a new directory for the prepared OCR pack.')
    parent = _local_path(destination.parent, exists=True)
    _stat(parent, directory=True)
    total = sum(model.size for model in models)
    if shutil.disk_usage(parent).free < total + MAX_CATALOGUE:
        raise OSError('There is not enough free space for the selected OCR pack.')
    _checkpoint(cancelled)
    with tempfile.TemporaryDirectory(prefix='.ocr-prepare-', dir=parent) as temporary:
        stage = Path(temporary)
        identities = {}
        for model in models:
            incoming = _local_path(source_directory / model.path, exists=True)
            if not incoming.is_relative_to(source_directory):
                raise ValueError('An OCR model is outside the selected source directory.')
            target = stage / model.path
            target.parent.mkdir(parents=True, exist_ok=True)
            identities[model.code] = _copy_verified(incoming, target, model, cancelled)
        # The consumer preserves models.json but may omit ancillary files. Keep
        # the complete upstream license inside that preserved manifest as well.
        manifest = {'schema': 1, 'upstream': source.upstream,
                    'license': 'Apache-2.0\n' + source.license_text, 'models': identities}
        raw = _json(manifest)
        _write(stage / 'LICENSE.txt', source.license_text.encode('utf-8'))
        _write(stage / 'MODEL_PROVENANCE.json', _json({
            'schema': 1, 'profile': profile, 'repository': source.repository,
            'revision': source.revision, 'models': {
                model.code: {'git_blob_sha1': model.git_blob_sha1, 'upstream_path': model.path}
                for model in models},
            'verification': 'Exact upstream Git blob size and SHA-1; SHA-256 computed from verified bytes.',
            'ocr_quality': 'Not established by a model identity check.'}))
        _write(stage / 'models.json', raw)
        _checkpoint(cancelled)
        _local_path(parent, exists=True)
        # mkdir is exclusive on both Windows and POSIX. Never rename over an
        # existing directory. models.json is the last publication/ready marker.
        destination.mkdir(mode=0o700)
        published = []
        created_directories = []
        try:
            payloads = sorted(path for path in stage.rglob('*')
                              if path.is_file() and path.name != 'models.json')
            for path in [*payloads, stage / 'models.json']:
                _checkpoint(cancelled)
                target = destination / path.relative_to(stage)
                if target.parent != destination and not target.parent.exists():
                    target.parent.mkdir(mode=0o700)
                    created_directories.append(target.parent)
                os.link(path, target)
                published.append(target)
            for directory in sorted({p.parent for p in published}, reverse=True):
                _sync(directory)
            _sync(parent)
        except BaseException:
            # Remove only names this operation created, never unknown content.
            for path in reversed(published):
                path.unlink(missing_ok=True)
            for directory in reversed(created_directories):
                try:
                    directory.rmdir()
                except OSError:
                    pass
            try:
                destination.rmdir()
            except OSError:
                pass
            raise
    return PreparedPack(str(destination), profile, source.revision,
                        tuple(model.code for model in models), total, sha256(raw).hexdigest())


from carr_library.installation_lease import installed_entrypoint


@installed_entrypoint
def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    listing = sub.add_parser('list', help='Search the pinned offline model catalogue.')
    listing.add_argument('--profile', choices=('fast', 'best'), default='fast')
    listing.add_argument('--query', default='')
    listing.add_argument('--kind', choices=('language', 'script', 'special'))
    prepare = sub.add_parser('prepare', help='Verify local weights and prepare a new offline pack.')
    prepare.add_argument('--profile', choices=('fast', 'best'), default='fast')
    prepare.add_argument('--source', type=Path, required=True)
    prepare.add_argument('--destination', type=Path, required=True)
    prepare.add_argument('--models', nargs='+', required=True)
    prepare.add_argument('--max-bytes', type=int, default=DEFAULT_MAX_BYTES)
    args = parser.parse_args(argv)
    try:
        if args.action == 'list':
            result = [asdict(model) for model in list_models(args.profile, query=args.query, kind=args.kind)]
        else:
            result = asdict(prepare_offline_pack(args.source, args.destination, args.models,
                                               profile=args.profile, max_bytes=args.max_bytes))
        # JSON escapes preserve Unicode paths even when Windows stdout uses an
        # older console encoding. Do not report failure after creating a pack.
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return 0
    except (OSError, ValueError) as exc:
        print(f'OCR models: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

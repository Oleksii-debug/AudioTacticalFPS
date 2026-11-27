"""Narrow old-queue import through the complete canonical pending validator.

The older archive is already hash/schema verified by archive.py. This adapter
uses only retained TXT/PDF bytes and the shared installed-profile declaration.
Unsupported or ambiguous queues remain intact and inactive in that archive.
No stored external source path is opened, and no extraction is performed here.
"""
from contextlib import closing
from dataclasses import asdict
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory

from .. import pending_backup as canonical
from ..backup_extensions import copy_files, read_bytes, write_bytes, _check
from ..ingest.pipeline import canonical as json_text
from ..extract.page_ocr import Config
from .formats import SUFFIXES

NOTICE = dict(component='canonical-pending-imports', status='restored',
    reason='Retained TXT/PDF imports passed the current schema, source and checkpoint checks and were rebound into the current queue. The original archived queue remains inert.')


def _copy_known(source, target, name, size, digest, cancelled):
    copy_files(source.parent, target.parent, [dict(filename=source.name, size_bytes=size, sha256=digest)], cancelled=cancelled)
    copied = target.parent / source.name
    if copied != target:
        os.rename(copied, target)
    return dict(filename=name, size_bytes=size, sha256=digest)


def _component(archive, target, manifest, cancelled):
    recovery = manifest.get('recovery')
    if recovery is None:
        return None
    saved = {item['path']: item for item in recovery['files']}
    names = sorted(name for name in saved if name == 'imports.sqlite' or name.startswith('import-requests/'))
    if not names:
        return None
    if any(name.startswith('import-batches/') for name in saved):
        raise ValueError('Legacy batch queues require a separate explicit mapping.')
    target.mkdir(mode=0o700)
    files, requests, assets, bindings = [], {}, {}, []
    for name in names:
        _check(cancelled)
        item = saved[name]
        source = archive / 'recovery' / name
        destination = target / name
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if name.startswith('import-requests/') and '/reviews/' not in name:
            record = json.loads(read_bytes(source, canonical.MAX_REQUEST), object_pairs_hook=canonical._unique)
            config = dict(record['config'])
            if config.pop('model_profile', 'installed') != 'installed':
                raise ValueError('A managed legacy OCR profile has no explicit canonical mapping.')
            if 'model_profile' in asdict(Config()):
                config['model_profile'] = 'installed'
            # This is the sole configuration adaptation. An absent installed
            # profile uses the current schema's exact default representation.
            record = dict(record, config=config)
            raw = (json_text(record) + '\n').encode('utf-8')
            write_bytes(destination, raw)
            files.append(dict(filename=name, size_bytes=len(raw), sha256=sha256(raw).hexdigest()))
            requests[name] = record
        else:
            files.append(_copy_known(source, destination, name, item['size_bytes'], item['sha256'], cancelled))
    available = {item['sha256']: (archive / 'originals' / item['filename'], item['mime'], item['size_bytes'])
                 for item in manifest['assets']}
    reverse = {suffix: mime for mime, suffix in SUFFIXES.items()}
    for name, item in saved.items():
        if name.startswith('pending-originals/'):
            path = archive / 'recovery' / name
            available[item['sha256']] = path, reverse[path.suffix], item['size_bytes']
    required = set()
    if 'imports.sqlite' in names:
        with closing(sqlite3.connect((target / 'imports.sqlite').as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
            db.execute('PRAGMA trusted_schema=OFF')
            required.update(row[0] for row in db.execute('SELECT asset FROM job'))
    for name, request in requests.items():
        expected = request.get('expected_manifest')
        identity = request.get('asset_sha256') or (expected['sha256'] if expected else None)
        if identity is None:
            raise ValueError('An old request has no unambiguous retained original identity.')
        required.add(identity)
        bindings.append(dict(filename=name, row_id=None, source=request['source'], asset_sha256=identity))
    for identity in sorted(required):
        if identity not in available:
            raise ValueError('An old request has no retained original bytes.')
        source, mime, size = available[identity]
        if mime not in ('text/plain', 'application/pdf'):
            raise ValueError('Only the shared TXT/PDF queue protocol has an explicit import mapping.')
        name = identity + SUFFIXES[mime]
        destination = target / 'originals' / name
        destination.parent.mkdir(exist_ok=True, mode=0o700)
        files.append(_copy_known(source, destination, 'originals/' + name, size, identity, cancelled))
        assets[identity] = dict(filename=name, size_bytes=size, sha256=identity, mime=mime)
    metadata = dict(version=1, data_dir=recovery['source_root'], files=sorted(files, key=lambda item: item['filename']),
                    assets=sorted(assets.values(), key=lambda item: item['filename']), sources=bindings)
    canonical.validate_pending(metadata)
    return metadata


def prepare_import(archive, stage, new_root, manifest, *, cancelled=lambda: False):
    """Return canonical staged paths, or None for a preserved incompatible queue.

    All semantic validation occurs before the caller's staging root is changed.
    I/O and cancellation failures propagate; they are not mislabeled as an
    unsupported queue. The caller must publish the whole restored root atomically.
    """
    from ..library_backup import _copy_database
    archive, stage, new_root = Path(archive), Path(stage), Path(new_root)
    _check(cancelled)
    with TemporaryDirectory(prefix='.legacy-pending-import-', dir=stage.parent) as tmp:
        temporary = Path(tmp)
        verified = temporary / 'verified'
        verified.mkdir(mode=0o700)
        try:
            metadata = _component(archive, temporary / 'component', manifest, cancelled)
            if metadata is None:
                return None
            _copy_database(archive / 'library.sqlite', verified / 'library.sqlite', manifest, cancelled=cancelled)
            originals = [dict(filename=item['filename'], size_bytes=item['size_bytes'], sha256=item['sha256'])
                         for item in manifest['assets']]
            copy_files(archive / 'originals', verified / 'originals', originals, cancelled=cancelled)
            # Reuse all current cross-request, catalog, review, MIME, source-
            # checkpoint and retained-identity checks before producing any path.
            result = canonical.restore_pending(temporary / 'component', verified, new_root, metadata, cancelled=cancelled)
        except (ValueError, TypeError, KeyError):
            _check(cancelled)
            return None
        for name in result['paths']:
            if os.path.lexists(stage / name):
                raise FileExistsError('The legacy queue importer cannot overwrite an existing canonical queue.')
        for item in result['assets']:
            if os.path.lexists(stage / 'originals' / item['filename']):
                raise FileExistsError('The legacy queue importer cannot overwrite an existing original.')
        for name in result['paths']:
            os.rename(verified / name, stage / name)
        (stage / 'originals').mkdir(mode=0o700, exist_ok=True)
        for item in result['assets']:
            os.rename(verified / 'originals' / item['filename'], stage / 'originals' / item['filename'])
        return result


def validate_import_notice(archive, manifest, *, cancelled=lambda: False):
    """Recheck the meaning of a recorded successful canonical import."""
    with TemporaryDirectory(prefix='.legacy-import-notice-') as tmp:
        stage = Path(tmp) / 'stage'
        stage.mkdir(mode=0o700)
        if prepare_import(archive, stage, Path(tmp) / 'future-root', manifest, cancelled=cancelled) is None:
            raise ValueError('The recorded legacy queue import has no supported canonical mapping.')

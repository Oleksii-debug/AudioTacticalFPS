"""Compose checked library backups with explicit, offline snapshot transport.

The transport never opens a live database. A portable checkpoint is an immutable
backup, not a live shared SQLite file. Restoring creates a separate data folder;
publishing requires the locally remembered history to agree with the vault.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
from tempfile import TemporaryDirectory
from uuid import uuid4

from ..data_paths import guard_data_directory
from ..ingest.writer_guard import _regular_if_present
from ..library_backup import create_backup, restore_backup
from .archive import (INVENTORY, cancelled_check, hash_file, local_path, pack_directory,
                      read_json, unpack_archive, write_json, _open_regular, _identity)


STATE = 'portable-sync-state.json'
TOKEN = re.compile(r'[0-9a-f]{64}\Z')


class SyncConflict(ValueError):
    """Local and portable history differ; neither side was overwritten."""


class SyncPublishedError(RuntimeError):
    """A checkpoint exists, but local history did not finish saving."""
    def __init__(self, receipt):
        super().__init__('Checkpoint published; local history could not be saved.')
        self.receipt = receipt


class SyncRestoredError(RuntimeError):
    """A verified library exists, but final directory durability was not confirmed."""
    def __init__(self, destination, token):
        super().__init__('Library restored; final directory durability could not be confirmed.')
        self.destination = str(destination)
        self.token = token


def _token(value):
    if not isinstance(value, str) or not TOKEN.fullmatch(value):
        raise ValueError('Enter the complete 64-character transfer code kept by the publishing device.')
    return value


def _message(callback, text):
    if callback is not None:
        callback(text)


def _atomic_json(path, value):
    path = local_path(path)
    _regular_if_present(path)
    temporary = path.with_name('.' + path.name + '-' + uuid4().hex)
    try:
        write_json(temporary, value)
        _regular_if_present(path)
        os.replace(temporary, path)
        if os.name != 'nt':
            fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    finally:
        temporary.unlink(missing_ok=True)


def _label(value):
    if (not isinstance(value, str) or not value.strip() or len(value) > 200
            or any(ord(ch) < 32 or 127 <= ord(ch) < 160 or 0xD800 <= ord(ch) <= 0xDFFF for ch in value)):
        raise ValueError('Use a checkpoint label of 1 to 200 printable characters.')
    return value.strip()


def _transfer_metadata(unpacked):
    if {p.name for p in unpacked.iterdir()} != {INVENTORY, 'backup'}:
        raise ValueError('Unexpected portable checkpoint contents.')
    metadata = read_json(unpacked / INVENTORY)
    if (type(metadata) is not dict or set(metadata) != {'kind', 'version', 'family_id', 'backup_checksum', 'label', 'created_at'}
            or metadata['kind'] != 'carr-portable-backup'
            or type(metadata['version']) is not int or metadata['version'] != 1
            or not isinstance(metadata['family_id'], str)
            or not re.fullmatch(r'[0-9a-f]{32}', metadata['family_id'])):
        raise ValueError('Unsupported portable library metadata.')
    _token(metadata['backup_checksum'])
    _label(metadata['label'])
    if not isinstance(metadata['created_at'], str) or len(metadata['created_at']) > 64:
        raise ValueError('Invalid portable checkpoint timestamp.')
    try:
        if datetime.fromisoformat(metadata['created_at']).tzinfo is None:
            raise ValueError('Missing checkpoint timezone.')
    except ValueError as exc:
        raise ValueError('Invalid portable checkpoint timestamp.') from exc
    return metadata


def _copy_verified_tree(source, destination, cancelled):
    """Copy a privately verified restore in bounded, cancellable blocks."""
    destination.mkdir(mode=0o700)
    records, folders = {}, set()
    def walk_error(error):
        raise error
    for directory, subdirs, names in os.walk(source, followlinks=False, onerror=walk_error):
        cancelled_check(cancelled)
        directory = local_path(directory, exists=True, directory=True)
        relative = directory.relative_to(source)
        folders.add(relative.as_posix())
        for name in subdirs:
            local_path(directory / name, exists=True, directory=True)
            (destination / relative / name).mkdir(mode=0o700)
        for name in names:
            incoming, before = _open_regular(directory / name)
            target = destination / relative / name
            digest, size = sha256(), 0
            with incoming, target.open('xb') as outgoing:
                os.chmod(target, 0o600)
                while block := incoming.read(1024 * 1024):
                    cancelled_check(cancelled)
                    outgoing.write(block)
                    digest.update(block)
                    size += len(block)
                if _identity(os.fstat(incoming.fileno())) != _identity(before):
                    raise ValueError('The verified restore changed while copying.')
                outgoing.flush()
                os.fsync(outgoing.fileno())
            records[(relative / name).as_posix()] = (size, digest.hexdigest())
        if os.name != 'nt':
            fd = os.open(destination / relative, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    return records, folders


def _verify_restored_output(completion):
    destination = local_path(completion['destination'], exists=True, directory=True)
    if (_identity(destination.stat())[:2] != completion['identity']
            or _identity(destination.parent.stat())[:2] != completion['parent']):
        raise ValueError('The restored destination is no longer this operation\'s directory.')
    observed_files, observed_folders = set(), set()
    def walk_error(error):
        raise error
    for parent, folders, files in os.walk(destination, followlinks=False, onerror=walk_error):
        parent = local_path(parent, exists=True, directory=True)
        relative = parent.relative_to(destination)
        observed_folders.add(relative.as_posix())
        if len(observed_folders) > len(completion['folders']):
            raise ValueError('Unexpected content appeared in the restored directory.')
        for name in files:
            key = (relative / name).as_posix()
            expected = completion['files'].get(key)
            if expected is None:
                raise ValueError('Unexpected content appeared in the restored directory.')
            incoming, before = _open_regular(parent / name)
            digest, remaining = sha256(), expected[0]
            with incoming:
                if before.st_size != remaining:
                    raise ValueError('The restored file size changed.')
                while remaining:
                    data = incoming.read(min(1024 * 1024, remaining))
                    if not data:
                        raise ValueError('A restored file is incomplete.')
                    digest.update(data)
                    remaining -= len(data)
                if (incoming.read(1) or digest.hexdigest() != expected[1]
                        or _identity(os.fstat(incoming.fileno())) != _identity(before)
                        or _identity((parent / name).lstat()) != _identity(before)):
                    raise ValueError('A restored file changed after publication.')
            observed_files.add(key)
    if (observed_files != set(completion['files']) or observed_folders != completion['folders']
            or _identity(destination.stat())[:2] != completion['identity']
            or _identity(destination.parent.stat())[:2] != completion['parent']):
        raise ValueError('The restored tree changed after publication.')


class SyncService:
    def __init__(self, app):
        self.app = app
        self.root = local_path(app.root, exists=True, directory=True)
        self.database = local_path(app.database, exists=True)
        self._root_identity = (self.root.stat().st_dev, self.root.stat().st_ino)
        self._database_identity = (self.database.stat().st_dev, self.database.stat().st_ino)
        if self.database.parent != self.root:
            raise ValueError('The library database is outside its data folder.')
        self.state_path = self.root / STATE
        self.cache_root = self.root.parent / ('.carr-portable-' + sha256(str(self.root).encode()).hexdigest()[:20])

    def _bound(self):
        if (Path(self.app.root).absolute() != self.root
                or Path(self.app.database).absolute() != self.database):
            raise ValueError('The library data folder changed. Reopen Portable snapshots.')
        local_path(self.root, exists=True, directory=True)
        local_path(self.database, exists=True)
        if ((self.root.stat().st_dev, self.root.stat().st_ino) != self._root_identity
                or (self.database.stat().st_dev, self.database.stat().st_ino) != self._database_identity):
            raise ValueError('The library data folder was replaced. Reopen Portable snapshots.')

    def _vault_path(self, folder):
        path = local_path(guard_data_directory(folder, purpose='portable snapshot'))
        if (path.is_relative_to(self.root) or self.root.is_relative_to(path)
                or path.is_relative_to(self.cache_root) or self.cache_root.is_relative_to(path)):
            raise ValueError('Choose a portable folder separate from the live library and its private staging.')
        return path

    def _state(self):
        self._bound()
        if not os.path.lexists(self.state_path):
            return {'kind': 'carr-portable-state', 'version': 1, 'family_id': uuid4().hex, 'vaults': {}}
        value = read_json(self.state_path)
        if (type(value) is not dict or set(value) != {'kind', 'version', 'family_id', 'vaults'}
                or value['kind'] != 'carr-portable-state' or type(value['version']) is not int or value['version'] != 1
                or not isinstance(value['family_id'], str) or not re.fullmatch(r'[0-9a-f]{32}', value['family_id'])
                or type(value['vaults']) is not dict or len(value['vaults']) > 4096):
            raise ValueError('Invalid portable-snapshot history. Existing data was not changed.')
        for key, record in value['vaults'].items():
            _token(key)
            if (type(record) is not dict or set(record) != {'head', 'label'}
                    or not isinstance(record['label'], str)):
                raise ValueError('Invalid portable-snapshot history entry.')
            _token(record['head'])
            _label(record['label'])
        return value

    @contextmanager
    def _operation(self):
        self._bound()
        from filelock import FileLock, Timeout
        lock_path = self.root / '.carr-portable.lock'
        _regular_if_present(lock_path, empty=True)
        lock = FileLock(lock_path, mode=0o600)
        try:
            lock.acquire(timeout=0)
        except Timeout as exc:
            raise RuntimeError('Another portable-snapshot operation is using this library.') from exc
        try:
            self._bound()
            yield
        finally:
            lock.release()

    def _cache(self):
        local_path(self.cache_root)
        if not os.path.lexists(self.cache_root):
            self.cache_root.mkdir(mode=0o700)
        return local_path(self.cache_root, exists=True, directory=True)

    @staticmethod
    def _vault_key(path):
        return sha256(os.fsencode(path)).hexdigest()

    def list(self, folder):
        from .vault import Vault
        self._bound()
        path = self._vault_path(folder)
        if not path.exists():
            return []
        vault = Vault(path)
        head = vault.head()
        result = []
        for receipt in vault.list_snapshots():
            result.append({'id': receipt.snapshot_id, 'parent': receipt.parent,
                'label': receipt.label, 'created_at': receipt.created_at,
                'files': receipt.file_count, 'bytes': receipt.total_bytes,
                'head': receipt.snapshot_id == head, 'encrypted': None})
        return sorted(result, key=lambda row: (row['created_at'], row['id']), reverse=True)

    def publish(self, folder, label, expected_head, password=None, *,
                cancelled=lambda: False, on_status=None):
        completion = {}
        try:
            return self._publish(folder, label, expected_head, password,
                                 cancelled=cancelled, on_status=on_status,
                                 completion=completion)
        except (Exception, KeyboardInterrupt) as error:
            receipt = completion.get('receipt')
            if receipt is None:
                raise
            if not completion.get('history_saved'):
                if isinstance(error, SyncPublishedError):
                    raise
                raise SyncPublishedError(receipt) from error
            warning = 'Checkpoint and local history were saved; private cleanup or final notification did not complete.'
            return {**receipt, 'message': warning + ' Keep the transfer code separately.',
                    'warnings': [*receipt.get('warnings', ()), warning]}

    def _publish(self, folder, label, expected_head, password, *,
                 cancelled, on_status, completion):
        from .vault import Vault
        label = _label(label)
        if expected_head is not None:
            _token(expected_head)
        if password is not None and (not isinstance(password, str) or not password):
            raise ValueError('An encryption password cannot be empty.')
        with self._operation():
            path = self._vault_path(folder)
            key = self._vault_key(path)
            state = self._state()
            remembered = state['vaults'].get(key, {}).get('head')
            vault = Vault(path)
            actual = vault.head()
            if actual != expected_head:
                raise SyncConflict('The portable folder changed. Refresh and review it before publishing.')
            if actual != remembered:
                raise SyncConflict('This device has a different checkpoint history. Restore the reviewed checkpoint to a new folder, or publish to a separate empty portable folder.')
            # Establish the local identity before external publication. No key or
            # password is persisted. An interrupted upload reuses verified chunks.
            _atomic_json(self.state_path, state)
            cancelled_check(cancelled)
            _message(on_status, 'Creating a checked backup of all libraries in this data folder.')
            cache = self._cache()
            with TemporaryDirectory(prefix='prepare-', dir=cache) as transient:
                transient = Path(transient)
                backup = transient / 'backup'
                checksum = create_backup(self.app, backup, cancelled=cancelled)
                cancelled_check(cancelled)
                identity = sha256(json.dumps([key, checksum, label, expected_head,
                    password is not None], ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
                job = cache / identity
                payload = job / 'payload'
                ready = job / 'ready.json'
                if os.path.lexists(job):
                    local_path(job, exists=True, directory=True)
                    metadata = read_json(ready)
                    if (type(metadata) is not dict or set(metadata) != {'identity', 'backup_checksum', 'encrypted', 'archive_sha256', 'payload_sha256'}
                            or metadata.get('backup_checksum') != checksum
                            or metadata.get('encrypted') is not (password is not None)
                            or metadata.get('identity') != identity):
                        raise ValueError('The pending transfer cache is inconsistent. No checkpoint was published.')
                    content = local_path(payload / ('snapshot.carr' if password is not None else 'snapshot.zip'), exists=True)
                    if {p.name for p in payload.iterdir()} != {content.name}:
                        raise ValueError('Unexpected pending transfer contents.')
                    _token(metadata['payload_sha256']); _token(metadata['archive_sha256'])
                    if hash_file(content, cancelled=cancelled) != metadata['payload_sha256']:
                        raise ValueError('The prepared transfer changed. No checkpoint was published.')
                    if password is not None:
                        from .crypto import decrypt_file
                        _message(on_status, 'Authenticating the existing encrypted transfer before resuming.')
                        decrypt_file(content, transient / 'resume-check.zip', password, cancelled=cancelled)
                        if hash_file(transient / 'resume-check.zip', cancelled=cancelled) != metadata['archive_sha256']:
                            raise ValueError('The cached encrypted transfer does not match its prepared backup.')
                    archive = transient / 'resume-check.zip' if password is not None else content
                    cached = unpack_archive(archive, transient / 'resume-unpacked', cancelled=cancelled)
                    transfer = _transfer_metadata(cached)
                    if (transfer['backup_checksum'] != checksum or transfer['family_id'] != state['family_id']
                            or transfer['label'] != label):
                        raise ValueError('The pending transfer belongs to different library contents or history.')
                    restore_backup(cached / 'backup', transient / 'resume-verified', checksum, cancelled=cancelled)
                    cancelled_check(cancelled)
                else:
                    job.mkdir(mode=0o700)
                    try:
                        payload.mkdir(mode=0o700)
                        source = transient / 'source'
                        source.mkdir(mode=0o700)
                        os.rename(backup, source / 'backup')
                        transfer = {'kind': 'carr-portable-backup', 'version': 1,
                            'family_id': state['family_id'], 'backup_checksum': checksum,
                            'label': label, 'created_at': datetime.now(timezone.utc).isoformat()}
                        write_json(source / INVENTORY, transfer)
                        archive = transient / 'prepared.zip'
                        archive_info = pack_directory(source, archive, cancelled=cancelled, on_status=on_status)
                        if password is not None:
                            from .crypto import encrypt_file
                            _message(on_status, 'Encrypting the checkpoint in private local staging.')
                            encrypt_file(archive, payload / 'snapshot.carr', password,
                                         cancelled=cancelled)
                        else:
                            os.rename(archive, payload / 'snapshot.zip')
                        content = payload / ('snapshot.carr' if password is not None else 'snapshot.zip')
                        write_json(ready, {'identity': identity, 'backup_checksum': checksum,
                            'encrypted': password is not None, 'archive_sha256': archive_info['sha256'],
                            'payload_sha256': hash_file(content, cancelled=cancelled)})
                    except BaseException:
                        # This newly created job is entirely owned by this call.
                        # Existing pending jobs are never removed on an error.
                        shutil.rmtree(job)
                        raise
                cancelled_check(cancelled)
                def progress(value):
                    _message(on_status, f'Publishing checkpoint: {value.bytes_completed} of {value.total_bytes} bytes; {value.chunks_reused} verified chunks reused.')
                receipt = vault.publish(payload, expected_head=expected_head,
                    label='Encrypted checkpoint' if password is not None else label,
                    cancelled=cancelled, on_progress=progress)
                result = {'id': receipt.snapshot_id, 'token': receipt.transfer_token,
                    'head': receipt.snapshot_id, 'encrypted': password is not None,
                    'files': receipt.file_count, 'bytes': receipt.total_bytes,
                    'scope': 'All libraries in this data folder',
                    'warnings': [receipt.durability_warning] if receipt.durability_warning else [],
                    'message': 'Checkpoint published and local history saved. Keep the transfer code separately from the portable folder.'}
                completion['receipt'] = result
                state['vaults'][key] = {'head': receipt.snapshot_id, 'label': label}
                try:
                    _atomic_json(self.state_path, state)
                except (OSError, ValueError) as exc:
                    raise SyncPublishedError({'id': receipt.snapshot_id, 'token': receipt.transfer_token,
                        'head': receipt.snapshot_id, 'encrypted': password is not None}) from exc
                completion['history_saved'] = True
                # A stale private cache can be removed later. Failure to remove it
                # must not hide a successfully committed checkpoint and its code.
                try:
                    shutil.rmtree(job)
                except OSError:
                    _message(on_status, 'Checkpoint saved. Private transfer cache cleanup will be retried later.')
                return result

    @contextmanager
    def _opened(self, folder, token, password, cancelled, on_status):
        from .vault import Vault
        self._bound()
        token = _token(token)
        path = self._vault_path(folder)
        with TemporaryDirectory(prefix='read-', dir=self._cache()) as transient:
            transient = Path(transient)
            vault = Vault(path)
            materialized = vault.materialize(token, transient / 'payload', expected_token=token,
                                             cancelled=cancelled)
            children = {entry.name for entry in materialized.iterdir()}
            encrypted = children == {'snapshot.carr'}
            if encrypted:
                if password is None:
                    raise ValueError('This checkpoint is encrypted. Enter its password for this operation.')
                from .crypto import decrypt_file
                _message(on_status, 'Authenticating and decrypting the selected checkpoint locally.')
                archive = transient / 'checked.zip'
                decrypt_file(materialized / 'snapshot.carr', archive, password, cancelled=cancelled)
            elif children == {'snapshot.zip'}:
                if password is not None:
                    raise ValueError('This checkpoint is not encrypted. No password was used.')
                archive = materialized / 'snapshot.zip'
            else:
                raise ValueError('This is not a supported portable library checkpoint.')
            unpacked = unpack_archive(archive, transient / 'unpacked', cancelled=cancelled, on_status=on_status)
            metadata = _transfer_metadata(unpacked)
            cancelled_check(cancelled)
            _message(on_status, 'Verifying database, originals, citations and backup identities.')
            verified = transient / 'verified-library'
            restore_backup(unpacked / 'backup', verified,
                           expected_manifest_sha256=metadata['backup_checksum'], cancelled=cancelled)
            cancelled_check(cancelled)
            manifest = read_json(unpacked / 'backup' / 'manifest.json', max_bytes=8 * 1024 * 1024)
            # Read only the validated staging database, never the live library.
            with closing(sqlite3.connect((verified / 'library.sqlite').as_uri() + '?mode=ro', uri=True)) as db:
                db.execute('PRAGMA trusted_schema=OFF')
                libraries = [str(row[0]) for row in db.execute('SELECT name FROM workspace_meta ORDER BY workspace')]
            warnings = []
            if manifest.get('version', 0) <= 2:
                warnings.append('This source backup format includes registered libraries and originals. Reading bookmarks, pending imports and optional studio state require their separate exports.')
            warnings.append('Only registered data and explicitly supported sidecars are included. External files, API credentials and unsupported project state require separate recovery arrangements.')
            if not manifest.get('generated_audio_included', False):
                warnings.append('Generated audio is not included in this library checkpoint. Keep the Audio Studio project backup and finished exports separately.')
            file_count = total_bytes = 0
            for item in verified.rglob('*'):
                cancelled_check(cancelled)
                if item.is_file():
                    file_count += 1
                    total_bytes += item.stat().st_size
            summary = {'id': token, 'token': token, 'encrypted': encrypted,
                'scope': 'All libraries in the published data folder', 'libraries': libraries,
                'schema_version': manifest['schema_version'], 'files': file_count,
                'bytes': total_bytes,
                'backup_checksum': metadata['backup_checksum'], 'warnings': warnings,
                'restore_mode': 'new-folder-only', 'verified': True, 'label': metadata['label']}
            yield verified, metadata, summary

    def inspect(self, folder, token, password=None, *, cancelled=lambda: False, on_status=None):
        with self._operation(), self._opened(folder, token, password, cancelled, on_status) as (_, _, result):
            return result

    def restore(self, folder, token, new_destination, password=None, *,
                cancelled=lambda: False, on_status=None):
        completion = {}
        try:
            return self._restore(folder, token, new_destination, password, cancelled=cancelled,
                                 on_status=on_status, completion=completion)
        except (Exception, KeyboardInterrupt) as error:
            if not completion:
                raise
            try:
                _verify_restored_output(completion)
            except (Exception, KeyboardInterrupt):
                raise error
            raise SyncRestoredError(completion['destination'], token) from error

    def _restore(self, folder, token, new_destination, password, *, cancelled, on_status, completion):
        with self._operation():
            destination = local_path(guard_data_directory(new_destination, purpose='restored library'))
            vault_path = self._vault_path(folder)
            for source in (self.root, self.cache_root, vault_path):
                if destination.is_relative_to(source) or source.is_relative_to(destination):
                    raise ValueError('Restore to a separate new directory outside the live library and portable folder.')
            if os.path.lexists(destination):
                raise FileExistsError('The restore destination already exists. Choose a new directory.')
            local_path(destination.parent, exists=True, directory=True)
            with self._opened(folder, token, password, cancelled, on_status) as (verified, metadata, _):
                cancelled_check(cancelled)
                _message(on_status, 'Restoring the checked checkpoint to the new data folder.')
                cancelled_check(cancelled)
                state = {'kind': 'carr-portable-state', 'version': 1,
                    'family_id': metadata['family_id'],
                    'vaults': {self._vault_key(vault_path): {'head': token, 'label': metadata['label']}}}
                from ..file_publication import publish_directory_no_replace
                from .vault import _sync_directory
                with TemporaryDirectory(prefix='.carr-portable-restore-', dir=destination.parent) as staging:
                    prepared = Path(staging) / 'library'
                    files, folders = _copy_verified_tree(verified, prepared, cancelled)
                    _atomic_json(prepared / STATE, state)
                    files[STATE] = ((prepared / STATE).stat().st_size, hash_file(prepared / STATE, cancelled=cancelled))
                    cancelled_check(cancelled)
                    completion.update(destination=destination, identity=_identity(prepared.stat())[:2],
                                      parent=_identity(destination.parent.stat())[:2], files=files, folders=folders)
                    publish_directory_no_replace(prepared, destination)
                    try:
                        _sync_directory(destination.parent)
                    except (Exception, KeyboardInterrupt) as exc:
                        raise SyncRestoredError(destination, token) from exc
                restored = destination
                return {'id': token, 'token': token, 'destination': str(restored),
                        'verified': True, 'message': 'Checkpoint restored to a separate folder. Open that library folder when ready.'}

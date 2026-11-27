"""Authenticated standard-setup updates through the existing maintenance owner.

An external idle helper, outside the program being replaced, calls this service.
TUF remains the only package verifier. The standard setup retains app/cache A,
then changes its own uninstall registration under a borrowed native lease. That
lease stays held through child-family cleanup, health and protected-anchor CAS.
Neither this service nor setup moves or restores the user's research directory.
"""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import os
from pathlib import Path
import re
import time
import uuid

from .. import managed_installation as protocol
from ._files import (TrustError, atomic_write, component, directory, file_info, json_bytes,
                     json_value, locked, read_bytes, sync_directory)
from .anchors import Anchor
from .installation import UpdateTransaction, WindowsBundleAdapter, unpack
from .updates import Release, require_verified_package, _cancel
from .windows_registration import WindowsRegistration, validate_snapshot


UNINSTALL_FILES = ('unins000.exe', 'unins000.dat', 'unins000.msg')
MAX_UNINSTALL_FILE = 32 * 1024**2
MAX_WRAPPER = 2 * 1024**2
RECEIPT_FILE = 'setup-build.json'
SETUP_FILE = 'ResonanceSetup.exe'


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _same_file(path, raw):
    if not os.path.lexists(path) or file_info(path).st_size != len(raw):
        return False
    return read_bytes(path, max(len(raw), 1)) == raw


def setup_receipt(stage, release):
    """Inspect data only; no proposed setup or helper executes during preview."""
    stage = directory(stage)
    if {path.name for path in stage.iterdir()} != {SETUP_FILE, RECEIPT_FILE}:
        raise TrustError('A managed update must contain only its standard setup and build receipt.')
    return validate_setup_receipt(stage / SETUP_FILE,
        read_bytes(stage / RECEIPT_FILE, 128 * 1024),
        source_commit=release.source_commit, version=release.version)


def validate_setup_receipt(setup, raw, *, source_commit, version):
    """Validate the fixed setup bytes and data-only build receipt, without execution."""
    setup = Path(setup)
    value = json_value(raw, maximum=128 * 1024)
    fields = {'schema', 'status', 'source_commit', 'bundle_manifest_sha256',
              'application_version', 'template_sha256', 'maintenance_sha256',
              'release_status', 'authenticode_verified', 'windows_installation_verified',
              'nvda_verified', 'setup_sha256', 'setup_bytes', 'toolchain', 'managed_upgrade_protocol'}
    helpers = {'maintenance.py', 'installation_lease.py', 'bundle_verifier.py',
               'windows_job.py', 'cleanup.py', 'capture_types.py', 'managed_installation.py'}
    if (type(value) is not dict or set(value) != fields
            or type(value['schema']) is not int or value['schema'] != 1
            or value['status'] != 'BUILT_NOT_INSTALLED'
            or value['managed_upgrade_protocol'] != protocol.PROOF_SCHEMA
            or value['source_commit'] != source_commit
            or value['application_version'] != version
            or value['release_status'] != 'unsigned-private-candidate'
            or any(type(value[key]) is not bool for key in
                   ('authenticode_verified', 'windows_installation_verified', 'nvda_verified'))
            or type(value['maintenance_sha256']) is not dict
            or set(value['maintenance_sha256']) != helpers
            or type(value['toolchain']) is not dict
            or type(value['setup_bytes']) is not int or not 0 < value['setup_bytes'] <= 16 * 1024**3):
        raise TrustError('The managed setup receipt differs from the authenticated release contract.')
    for checksum in (value['bundle_manifest_sha256'], value['template_sha256'],
                     value['setup_sha256'], *value['maintenance_sha256'].values()):
        protocol.digest(checksum)
    # The TUF extraction already bounded uncompressed bytes independently from
    # compressed target length. Hash the setup without loading it all into RAM.
    before = file_info(setup)
    if before.st_size != value['setup_bytes']:
        raise TrustError('The authenticated setup length changed after extraction.')
    with setup.open('rb') as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino, opened.st_size) != (before.st_dev, before.st_ino, before.st_size):
            raise TrustError('The authenticated setup changed while opening.')
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        after = os.fstat(stream.fileno())
    current = file_info(setup)
    identity = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_ctime_ns)
    if digest != value['setup_sha256'] or any(identity(item) != identity(before) for item in (after, current)):
        raise TrustError('The authenticated setup bytes changed after extraction.')
    return value


class WindowsManagedSetupAdapter:
    """Use a trusted host maintenance module, never one imported from target B."""
    def __init__(self, maintenance, *, registration=None, timeout=1800):
        if type(timeout) is not int or not 1 <= timeout <= 1800:
            raise TrustError('Invalid managed setup timeout.')
        self.maintenance = maintenance
        self.registration = WindowsRegistration() if registration is None else registration
        self.bundle = WindowsBundleAdapter(maintenance.verifier.verify, timeout=timeout)
        self.timeout = timeout

    def verify_current(self, root):
        root = directory(root)
        value = self.maintenance.marker(root)
        if value['state'] != 'complete':
            raise TrustError('Recover pending standard installation maintenance first.')
        owned = self.maintenance.owned(root, value['bundle_sha256'], value['setup_sha256'])
        if self.maintenance.inventory(root / 'app') != (owned['files'], owned['directories']):
            raise TrustError('Changed or foreign program files require reviewed Repair before updating.')
        self.maintenance.bundle(root / 'app', value['bundle_sha256'], allow_installed=True)
        return value

    def _wrapper(self, root, destination, *, allow_partial=False):
        """Retain registration, exact shortcuts and fixed uninstall-file bytes."""
        directory(destination, create=True)
        files = {}
        for name in UNINSTALL_FILES:
            source = root / name
            if not os.path.lexists(source):
                files[name] = None
                continue
            raw = read_bytes(source, MAX_UNINSTALL_FILE)
            retained = destination / name
            if os.path.lexists(retained):
                if read_bytes(retained, MAX_UNINSTALL_FILE) != raw:
                    raise TrustError('A retained uninstall snapshot has another identity.')
            else:
                atomic_write(retained, raw)
            files[name] = {'bytes': len(raw), 'sha256': _sha(raw)}
        value = {'schema': 1, 'root': str(root), 'files': files,
                 'registration': self.registration.snapshot(root, allow_partial=allow_partial)}
        raw = json_bytes(value)
        if len(raw) > MAX_WRAPPER:
            raise TrustError('The managed wrapper snapshot exceeds its byte allowance.')
        atomic_write(destination / 'wrapper.json', raw)
        sync_directory(destination)
        return _sha(raw)

    def _read_wrapper(self, root, source, expected):
        raw = read_bytes(source / 'wrapper.json', MAX_WRAPPER)
        if _sha(raw) != protocol.digest(expected):
            raise TrustError('The retained installer wrapper snapshot changed.')
        value = json_value(raw, maximum=MAX_WRAPPER)
        if (type(value) is not dict or set(value) != {'schema', 'root', 'files', 'registration'}
                or type(value['schema']) is not int or value['schema'] != 1
                or value['root'] != str(root) or type(value['files']) is not dict
                or set(value['files']) != set(UNINSTALL_FILES)):
            raise TrustError('Invalid retained installer wrapper snapshot.')
        validate_snapshot(value['registration'], root)
        for name, item in value['files'].items():
            if item is None:
                if os.path.lexists(source / name):
                    raise TrustError('An undeclared uninstall snapshot was added.')
                continue
            if (type(item) is not dict or set(item) != {'bytes', 'sha256'}
                    or type(item['bytes']) is not int or not 0 <= item['bytes'] <= MAX_UNINSTALL_FILE):
                raise TrustError('Invalid retained uninstall file identity.')
            raw = read_bytes(source / name, MAX_UNINSTALL_FILE)
            if len(raw) != item['bytes'] or _sha(raw) != protocol.digest(item['sha256']):
                raise TrustError('A retained uninstall file changed.')
        return value

    def capture(self, root, stage, previous, release, token):
        receipt = setup_receipt(stage, release)
        original = self.verify_current(root)
        if original['bundle_sha256'] == receipt['bundle_manifest_sha256']:
            raise TrustError('The same bundle uses Repair, not a new update identity.')
        previous.mkdir(mode=0o700)
        for name in (self.maintenance.MARKER, self.maintenance.OWNER):
            atomic_write(previous / name, self.maintenance.read(root / name))
        wrapper_sha256 = self._wrapper(root, previous / 'wrapper')
        original_wrapper = self._read_wrapper(root, previous / 'wrapper', wrapper_sha256)
        if any(original_wrapper['files'][name] is None for name in ('unins000.exe', 'unins000.dat')):
            raise TrustError('The original standard uninstaller is incomplete; use reviewed Repair first.')
        proof = {'schema': protocol.PROOF_SCHEMA, 'transaction': token, 'root': str(root),
                 'old': {key: original[key] for key in ('bundle_sha256', 'setup_sha256', 'owner_sha256')},
                 'new': {'bundle_sha256': receipt['bundle_manifest_sha256'],
                         'setup_sha256': receipt['setup_sha256'],
                         'source_commit': release.source_commit, 'version': release.version}}
        raw = protocol.encode_proof(proof)
        atomic_write(previous / protocol.PROOF_FILE, raw)
        sync_directory(previous.parent)
        return _sha(raw), wrapper_sha256

    def activate(self, root, stage, release, proof_file, proof_sha256, owner, cancelled):
        receipt = setup_receipt(stage, release)
        proof = protocol.decode_proof(read_bytes(proof_file, protocol.MAX_PROOF), proof_sha256)
        protocol.require_binding(proof, root, receipt['bundle_manifest_sha256'], receipt['setup_sha256'])
        if os.name != 'nt':
            raise TrustError('Actual standard setup activation requires native Windows.')
        pid, handle = owner.windows_owner()
        _cancel(cancelled)
        command = [stage / SETUP_FILE, '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART',
                   '/DIR=' + str(root), '/CARRUPGRADE=' + str(proof_file),
                   '/CARRUPGRADESHA=' + proof_sha256,
                   '/CARRLEASEPID=' + str(pid), '/CARRLEASEHANDLE=' + str(handle)]
        # The setup handles its own exact payload extraction/bootstrap. No
        # arbitrary installer options, shell, restart or external URLs are used.
        status = self.bundle._run_owned(command, root=stage, timeout=self.timeout)
        if status:
            raise TrustError('The authenticated standard setup did not complete.')
        _cancel(cancelled)

    def health(self, root, stage, release):
        receipt = setup_receipt(stage, release)
        owned = self.maintenance.owned(root, receipt['bundle_manifest_sha256'],
                                       receipt['setup_sha256'], allow_updating=True)
        if (self.maintenance.inventory(root / 'app') != (owned['files'], owned['directories'])
                or owned['maintenance'] != {**receipt['maintenance_sha256'], SETUP_FILE: receipt['setup_sha256']}):
            return False
        for name in ('unins000.exe', 'unins000.dat'):
            file_info(root / name)
        if not self.registration.health(root, release):
            return False
        return self.bundle.health(root / 'app', release)

    def restore(self, root, proof_file, proof_sha256, wrapper_sha256, owner):
        proof = protocol.decode_proof(read_bytes(proof_file, protocol.MAX_PROOF), proof_sha256)
        previous, failed = protocol.previous_path(proof), protocol.failed_path(proof)
        old = self._read_wrapper(root, previous / 'wrapper', wrapper_sha256)

        def restore_wrapper():
            # Preserve B's partial or complete wrapper too before replacing its
            # fixed entries. A retry validates the same retained capture.
            directory(failed, create=True)
            target = failed / 'wrapper'
            capture = failed / 'wrapper-sha256.txt'
            if os.path.lexists(capture):
                current_hash = read_bytes(capture, 64).decode('ascii')
                changed = self._read_wrapper(root, target, current_hash)
            else:
                current_hash = self._wrapper(root, target, allow_partial=True)
                atomic_write(capture, current_hash.encode('ascii'))
                changed = self._read_wrapper(root, target, current_hash)
            for name in UNINSTALL_FILES:
                path = root / name
                old_file, changed_file = old['files'][name], changed['files'][name]
                desired = None if old_file is None else read_bytes(previous / 'wrapper' / name, MAX_UNINSTALL_FILE)
                if desired is not None and _same_file(path, desired):
                    continue
                if os.path.lexists(path):
                    current = read_bytes(path, MAX_UNINSTALL_FILE)
                    if changed_file is None or (len(current), _sha(current)) != (changed_file['bytes'], changed_file['sha256']):
                        raise TrustError('An uninstall file changed outside the captured update; keep it for reviewed recovery.')
                elif changed_file is not None and desired is None:
                    continue
                if desired is None:
                    if os.path.lexists(path):
                        path.unlink()
                else:
                    atomic_write(path, desired)
            self.registration.restore(root, old['registration'])
            sync_directory(root)

        self.maintenance.restore_upgrade(root, proof_file=proof_file, proof_sha256=proof_sha256,
                                         owner=owner, restore_wrapper=restore_wrapper)


class ManagedUpdateTransaction(UpdateTransaction):
    """The existing trusted transaction with standard-installer wrapper recovery."""
    def __init__(self, app, installed, work, policy, anchor, adapter, *, idle_lease, clock=time.time):
        self.root = directory(installed)
        self.installed = self.root / 'app'
        self.app, self.policy, self.anchor, self.adapter = app, policy, anchor, adapter
        self.work = directory(work, create=True)
        self.idle_lease, self.clock = idle_lease, clock
        self.journal = self.work / 'managed-activation.json'
        for first, second in ((self.root, app.root), (self.work, app.root), (self.work, self.root)):
            if first.is_relative_to(second) or second.is_relative_to(first):
                raise TrustError('Program, managed recovery and research data must be separate directories.')
        if self.work.stat().st_dev != self.root.parent.stat().st_dev:
            raise TrustError('Managed update recovery must share the program filesystem.')
        if os.path.lexists(self.installed):
            directory(self.installed)
        elif not os.path.lexists(self.journal):
            raise TrustError('The managed program is absent and no recovery journal exists.')

    def _paths(self, record):
        token = record['transaction']
        return (self.work / ('stage-' + token), self.root.parent / ('.resonance-previous-' + token),
                self.root.parent / ('.resonance-failed-' + token))

    def _record(self):
        value = json_value(read_bytes(self.journal, 16384), maximum=16384)
        fields = {'version', 'kind', 'phase', 'program', 'data', 'transaction', 'backup',
                  'backup_sha256', 'proof_sha256', 'wrapper_sha256', 'before', 'release'}
        if (type(value) is not dict or set(value) != fields or type(value['version']) is not int
                or value['version'] != 1 or value['kind'] != 'standard-setup'
                or value['phase'] not in ('prepared', 'activating', 'health-checked', 'complete', 'rolled-back')
                or value['program'] != str(self.root) or value['data'] != str(self.app.root)
                or not isinstance(value['transaction'], str)
                or not re.fullmatch('[0-9a-f]{32}', value['transaction'])
                or value['backup'] != 'backup-' + value['transaction']
                or type(value['release']) is not dict or set(value['release']) != set(Release.__dataclass_fields__)):
            raise TrustError('Unsupported or retargeted managed update recovery journal.')
        before = Anchor.decode(json_bytes(value['before']))
        release = Release(**value['release'])
        if (type(release.build) is not int or release.build <= before.sequence
                or (release.product, release.channel, release.data_schema) !=
                   (self.policy.product, self.policy.channel, self.policy.data_schema)
                or type(release.length) is not int or not 0 < release.length <= self.policy.max_package_bytes):
            raise TrustError('The managed update recovery release is inconsistent.')
        for key in ('backup_sha256', 'proof_sha256', 'wrapper_sha256'):
            protocol.digest(value[key])
        protocol.digest(release.sha256)
        stage, previous, failed = self._paths(value)
        raw = read_bytes(previous / protocol.PROOF_FILE, protocol.MAX_PROOF)
        proof = protocol.decode_proof(raw, value['proof_sha256'])
        if (proof['root'] != str(self.root) or proof['transaction'] != value['transaction']
                or proof['new']['source_commit'] != release.source_commit
                or proof['new']['version'] != release.version):
            raise TrustError('The managed update proof belongs to another installation or release.')
        return value, before, release

    def _rollback(self, record, owner):
        _, previous, _ = self._paths(record)
        self.adapter.restore(self.root, previous / protocol.PROOF_FILE, record['proof_sha256'],
                             record['wrapper_sha256'], owner)
        self._write(record, 'rolled-back')

    def recover(self):
        if not os.path.lexists(self.journal):
            return 'nothing-to-recover'
        with locked(self.work, '.update.lock'), self.idle_lease(), \
                self.adapter.maintenance.installation_lease(self.root, exclusive=True) as owner:
            record, before, release = self._record()
            if record['phase'] in ('complete', 'rolled-back'):
                return record['phase']
            current = self.anchor.load()
            stage, previous, _ = self._paths(record)
            if current.sequence == release.build and current.digest == release.sha256:
                if not self.adapter.health(self.root, stage, release):
                    raise TrustError('A committed managed update needs reviewed recovery; its previous files remain retained.')
                self.adapter.maintenance.finish_upgrade(self.root,
                    proof_file=previous / protocol.PROOF_FILE, proof_sha256=record['proof_sha256'], owner=owner)
                self._write(record, 'complete')
                return 'complete'
            if current != before:
                raise TrustError('Protected update state changed; automatic rollback is refused.')
            self._rollback(record, owner)
            return 'rolled-back'

    def apply(self, package, *, cancelled=lambda: False):
        from ..library_backup import create_backup
        from ..ingest.writer_guard import exclusive_writer
        require_verified_package(package)
        release = package.release
        if (release.product, release.channel, release.data_schema) != (
                self.policy.product, self.policy.channel, self.policy.data_schema):
            raise TrustError('Update product, channel or schema does not match.')
        with locked(self.work, '.update.lock'), self.idle_lease(), \
                self.adapter.maintenance.installation_lease(self.root, exclusive=True) as owner:
            if os.path.lexists(self.journal):
                existing, _, _ = self._record()
                if existing['phase'] not in ('complete', 'rolled-back'):
                    raise TrustError('Recover the pending managed update before preparing another.')
            before, now = self.anchor.load(), int(self.clock())
            if now + 300 < before.max_utc:
                raise TrustError('The system clock moved backwards; update approval requires review.')
            if release.build <= max(before.sequence, self.policy.current_build):
                raise TrustError('Protected update state refuses a rollback or replay.')
            self.adapter.verify_current(self.root)
            token = uuid.uuid4().hex
            record = dict(version=1, kind='standard-setup', phase='prepared', program=str(self.root),
                          data=str(self.app.root), transaction=token, backup='backup-' + token,
                          backup_sha256='', proof_sha256='', wrapper_sha256='',
                          before=json_value(before.encode()), release=asdict(release))
            stage, previous, failed = self._paths(record)
            if os.path.lexists(previous) or os.path.lexists(failed):
                raise TrustError('Managed update recovery destinations already exist.')
            unpack(package, stage, self.policy, cancelled=cancelled)
            record['proof_sha256'], record['wrapper_sha256'] = self.adapter.capture(
                self.root, stage, previous, release, token)
            _cancel(cancelled)
            record['backup_sha256'] = create_backup(self.app, self.work / record['backup'], cancelled=cancelled)
            _cancel(cancelled)
            self._write(record, 'prepared')
            with exclusive_writer(self.app.root):
                try:
                    self._write(record, 'activating')
                    self.adapter.activate(self.root, stage, release, previous / protocol.PROOF_FILE,
                                          record['proof_sha256'], owner, cancelled)
                    _cancel(cancelled)
                    if not self.adapter.health(self.root, stage, release):
                        raise TrustError('The new standard installation failed its complete health check.')
                    self._write(record, 'health-checked')
                    replacement = Anchor(release.build, release.sha256, max(before.max_utc, now))
                    self.anchor.compare_and_set(before, replacement, guard=lambda: _cancel(cancelled))
                    self.adapter.maintenance.finish_upgrade(self.root,
                        proof_file=previous / protocol.PROOF_FILE, proof_sha256=record['proof_sha256'], owner=owner)
                except BaseException:
                    # Even a native setup exception is delivered only after its
                    # owned family is quiescent. A committed anchor never rolls
                    # back: recovery finishes B under this same exclusive lease.
                    if self.anchor.load() == before:
                        self._rollback(record, owner)
                    raise
            self._write(record, 'complete')
            return {'status': 'complete', 'version': release.version, 'build': release.build,
                    'source_commit': release.source_commit, 'previous': str(previous),
                    'backup': str(self.work / record['backup']), 'backup_sha256': record['backup_sha256'],
                    'layout': 'standard-setup', 'data_retargeted': False}

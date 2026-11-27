"""TUF-authenticated A-to-B upgrades through the existing standard installer.

No service is activated here. A trusted host explicitly supplies its one update
policy, protected anchor, current installer validators and application root.
The portable replacement transaction remains separate and refuses these slots.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from dataclasses import asdict
from hashlib import sha256
import importlib.util
import os
from pathlib import Path
import re
import time
import tempfile
from uuid import uuid4

from ..audio.cleanup import AudioCleanupPending
from ._files import (TrustError, atomic_write, directory, file_info, json_bytes,
                     json_value, locked, read_bytes)
from .anchors import Anchor, WindowsVaultAnchor
from .standard_recovery import StandardRecoveryAuthority
from .installation import unpack, _verified_snapshot
from .standard_state import (MAX_STATE, PENDING, PRIOR, REJECTED, PROTOCOL, begin_pending,
    clear_pending, generation_name, prior_bytes, publish_new, read_pending, replace_pending)
from .standard_windows import WindowsStandardHost, _file_digest, validate_snapshot
from .updates import Release, UpdatePolicy, _cancel, require_verified_package, _verified_package

MAX_JOURNAL = 2 * MAX_STATE + 65536
_TERMINAL = {'complete', 'rolled-back'}
_PHASES = _TERMINAL | {'prepared', 'installing', 'activated', 'health-checked',
                       'committed', 'rolling-back', 'recovery-required'}
_DIGEST = re.compile(r'[0-9a-f]{64}\Z')
_SOURCE = re.compile(r'[0-9a-f]{40}\Z')
_TOKEN = re.compile(r'[0-9a-f]{32}\Z')


def _release(value, policy, before):
    if type(value) is not dict or set(value) != set(Release.__dataclass_fields__):
        raise TrustError('Invalid signed release checkpoint.')
    result = Release(**value)
    if ((result.product, result.channel, result.data_schema) !=
            (policy.product, policy.channel, policy.data_schema)
            or type(result.build) is not int or not before.sequence < result.build <= 2**53 - 1
            or type(result.data_schema) is not int
            or type(result.length) is not int or not 0 < result.length <= policy.max_package_bytes
            or type(result.sha256) is not str or not _DIGEST.fullmatch(result.sha256)
            or type(result.source_commit) is not str or not _SOURCE.fullmatch(result.source_commit)
            or type(result.version) is not str
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.+_-]{0,63}', result.version)
            or type(result.notes) is not str or len(result.notes) > 4096):
        raise TrustError('The release checkpoint does not match this configured update policy.')
    from .updates import relative_path
    relative_path(result.target)
    return result


class RecoveryReviewRequired(TrustError):
    """A pending pre-commit recovery needs a newly verified publisher receipt."""


def review_installer(staged, release, host, *, cancelled=lambda: False):
    """Bind the current builder report, full manifest and setup to verified TUF bytes."""
    staged = directory(staged)
    expected_name = 'Resonance-Setup-' + release.source_commit[:12] + '.exe'
    if {path.name for path in staged.iterdir()} != {'dist', 'installer-build.json', 'bundle-manifest.json'}:
        raise TrustError('Use exactly the current installer, build report and complete bundle manifest.')
    dist = directory(staged / 'dist')
    if {path.name for path in dist.iterdir()} != {expected_name}:
        raise TrustError('The signed package has unexpected installer files.')
    raw_report = read_bytes(staged / 'installer-build.json', 65536)
    report = json_value(raw_report)
    required = {'schema', 'kind', 'source_commit', 'manifest_sha256', 'payload_files',
                'status', 'recipe_sha256', 'native_install_repair_uninstall',
                'authenticode_verified', 'nvda_verified', 'update_or_entitlement_authority',
                'automatic_network', 'compiler_sha256', 'installer', 'managed_update_protocol'}
    if (type(report) is not dict or set(report) != required or type(report['schema']) is not int
            or report['schema'] != 1 or report['kind'] != 'standard-windows-installer'
            or report['status'] != 'compiled' or report['source_commit'] != release.source_commit
            or type(report['payload_files']) is not int or report['payload_files'] <= 0
            or type(report['authenticode_verified']) is not bool or type(report['nvda_verified']) is not bool
            or report['managed_update_protocol'] != PROTOCOL
            or report['automatic_network'] is not False
            or report['update_or_entitlement_authority'] is not False):
        raise TrustError('The verified target is not a supported compiled standard installer.')
    for key in ('manifest_sha256', 'recipe_sha256', 'compiler_sha256'):
        if type(report[key]) is not str or not _DIGEST.fullmatch(report[key]):
            raise TrustError('Invalid standard installer build identity.')
    raw_manifest = read_bytes(staged / 'bundle-manifest.json', 16 * 1024**2)
    if sha256(raw_manifest).hexdigest() != report['manifest_sha256']:
        raise TrustError('The setup report and complete bundle manifest do not match.')
    # This is the trusted host's current create_delivery manifest + audio
    # transfer gate, not a validator imported from the proposed executable.
    manifest = host.validate_manifest(raw_manifest, release.source_commit)
    if (type(manifest) is not dict or manifest.get('source_commit') != release.source_commit
            or manifest.get('bundled_native_launcher') is not True
            or manifest.get('bundled_python') is not True
            or type(manifest.get('files')) is not dict
            or report['payload_files'] != len(manifest['files'])
            or not {'ResearchLibrary.exe', 'native-launcher-build.json', 'runtime/python.exe'}.issubset(manifest['files'])):
        raise TrustError('The signed installer lacks the complete current native payload.')
    installer = report['installer']
    if (type(installer) is not dict or set(installer) != {'path', 'sha256', 'bytes'}
            or installer['path'] != 'dist/' + expected_name
            or type(installer['bytes']) is not int or not 0 < installer['bytes'] <= 2 * 1024**3
            or type(installer['sha256']) is not str or not _DIGEST.fullmatch(installer['sha256'])):
        raise TrustError('Invalid signed standard setup identity.')
    info = file_info(staged / installer['path'])
    if (info.st_size != installer['bytes']
            or _file_digest(staged / installer['path'], cancelled=cancelled) != installer['sha256']):
        raise TrustError('The compiled setup changed after its signed package was verified.')
    return {'source_commit': release.source_commit, 'manifest_sha256': report['manifest_sha256'],
            'setup_sha256': installer['sha256'], 'installer': installer['path'],
            'report_sha256': sha256(raw_report).hexdigest()}


class StandardUpdateTransaction:
    """One configured policy and anchor, retaining A and the existing data schema."""
    def __init__(self, app, generation, work, policy, anchor, host, *, clock=time.time):
        if type(policy) is not UpdatePolicy or not isinstance(host, WindowsStandardHost):
            raise TrustError('Use the current configured update policy and standard installation adapter.')
        from ..application import LibraryApp
        from ..storage.revisions import SCHEMAS
        if not isinstance(app, LibraryApp) or policy.data_schema != max(SCHEMAS):
            raise TrustError('Standard updates support only this current library service and unchanged supported data schema.')
        self.app, self.policy, self.anchor, self.host, self.clock = app, policy, anchor, host, clock
        self.generation = directory(generation)
        self.root = directory(self.generation.parent.parent)
        self.generation_relative = self.generation.relative_to(self.root).as_posix()
        if (self.generation.parent.name != 'versions'
                or read_bytes(self.root / 'install-layout-v1.txt', 128).strip() != b'Resonance.StandardInstall/1'):
            raise TrustError('This transaction requires the existing standard versions layout.')
        raw = read_bytes(self.generation / 'bundle-manifest.json', 16 * 1024**2)
        parsed = json_value(raw, maximum=16 * 1024**2)
        self.initial_source = parsed.get('source_commit') if type(parsed) is dict else None
        if type(self.initial_source) is not str or not _SOURCE.fullmatch(self.initial_source):
            raise TrustError('The current generation needs an exact source identity.')
        generation_name(self.generation_relative, self.initial_source)
        self.initial_manifest = sha256(raw).hexdigest()
        self.work = directory(work, create=True)
        data = directory(app.root)
        for one, other in ((self.root, data), (self.work, data), (self.root, self.work)):
            if one.is_relative_to(other) or other.is_relative_to(one):
                raise TrustError('Program, signed-update staging and research data must be separate.')
        self.journal = self.work / 'standard-activation.json'
        # A mutable journal cannot authenticate native registration/shortcut
        # bytes. Retain live authority, and only an explicitly configured
        # Windows anchor can additionally seal the exact restart authority.
        self._live_prior = None
        self._recovery_authority = (StandardRecoveryAuthority(anchor, policy, self.root, data, self.work)
                                    if isinstance(anchor, WindowsVaultAnchor) else None)

    def _retain_target(self, package, destination, cancelled):
        """Retain exact verified bytes, with no overwrite or hard-link window."""
        from ..audio.jobs import publish_noreplace
        with _verified_snapshot(package, self.work, cancelled) as source:
            descriptor, name = tempfile.mkstemp(prefix='.standard-target-', dir=self.work)
            owned = os.fstat(descriptor)
            try:
                with os.fdopen(descriptor, 'wb') as target:
                    total = 0
                    for block in iter(lambda: source.read(1024 * 1024), b''):
                        _cancel(cancelled)
                        total += len(block)
                        if total > package.release.length:
                            raise TrustError('The verified target exceeded its recorded length.')
                        target.write(block)
                    if total != package.release.length:
                        raise TrustError('The verified target snapshot is incomplete.')
                    target.flush()
                    os.fsync(target.fileno())
                _cancel(cancelled)
                publish_noreplace(Path(name), destination)
            finally:
                try:
                    current = os.lstat(name)
                except FileNotFoundError:
                    pass
                else:
                    if ((current.st_dev, current.st_ino) == (owned.st_dev, owned.st_ino)
                            and current.st_nlink == 1):
                        Path(name).unlink()

    def _review_recovery_target(self, record, release, package, *, committed, cancelled):
        """Journal fields never recreate publisher approval for a fresh action."""
        if committed:
            anchor = self.anchor.load()
            archive = self.work / record['archive']
            if (anchor.sequence != release.build or anchor.digest != release.sha256
                    or _file_digest(archive, self.policy.max_package_bytes, cancelled=cancelled) != anchor.digest):
                raise TrustError('The retained update target does not match protected committed state.')
            # A protected accepted digest is authority only for these exact
            # retained bytes. Existing unpack still checks length and digest.
            package = _verified_package(release, archive, self.policy)
        else:
            if package is None:
                raise RecoveryReviewRequired('Reverify the exact pending release from an explicitly selected '
                    'signed repository before rollback. The prior program and pending state are unchanged.')
            require_verified_package(package, policy=self.policy)
            if package.release != release:
                raise TrustError('The newly verified release differs from the pending recovery.')
        with tempfile.TemporaryDirectory(prefix='.standard-recovery-', dir=self.work) as temporary:
            target = Path(temporary) / 'target'
            unpack(package, target, self.policy, cancelled=cancelled)
            candidate = review_installer(target, release, self.host, cancelled=cancelled)
        if candidate != record['candidate']:
            raise TrustError('Recovery candidate metadata differs from the authenticated target bytes.')
        if record['observed'] is not None:
            self._require_candidate(record['observed'], candidate, record['pending'])

    def pending_release(self):
        """Untrusted review hint; only the existing TUF client can approve it."""
        _record, _before, release = self._read()
        return release

    def _write(self, record, phase):
        if phase not in _PHASES:
            raise TrustError('Unsupported standard update phase.')
        record['phase'] = phase
        raw = json_bytes(record)
        if len(raw) > MAX_JOURNAL:
            raise TrustError('The standard update journal exceeds its byte allowance.')
        atomic_write(self.journal, raw)

    def _read(self):
        value = json_value(read_bytes(self.journal, MAX_JOURNAL), maximum=MAX_JOURNAL)
        fields = {'schema', 'program', 'data', 'transaction', 'phase', 'stage', 'archive', 'backup',
                  'backup_sha256', 'before', 'release', 'prior', 'observed', 'candidate', 'pending'}
        if (type(value) is not dict or set(value) != fields or value['schema'] != PROTOCOL
                or value['program'] != str(self.root) or value['data'] != str(self.app.root)
                or value['phase'] not in _PHASES or type(value['transaction']) is not str
                or not _TOKEN.fullmatch(value['transaction'])):
            raise TrustError('Invalid standard update recovery journal.')
        token = value['transaction']
        if (value['stage'] != 'standard-stage-' + token or value['backup'] != 'standard-backup-' + token
                or value['archive'] != 'standard-target-' + token + '.zip'):
            raise TrustError('Standard recovery paths do not match the transaction.')
        if type(value['backup_sha256']) is not str or not _DIGEST.fullmatch(value['backup_sha256']):
            raise TrustError('The complete research backup identity is unavailable.')
        before = Anchor.decode(json_bytes(value['before']))
        release = _release(value['release'], self.policy, before)
        prior = validate_snapshot(value['prior'], self.root)
        candidate = value['candidate']
        if (type(candidate) is not dict or set(candidate) != {'source_commit', 'manifest_sha256',
                'setup_sha256', 'installer', 'report_sha256'}
                or candidate['source_commit'] != release.source_commit
                or candidate['installer'] != 'dist/Resonance-Setup-' + release.source_commit[:12] + '.exe'):
            raise TrustError('Invalid candidate setup checkpoint.')
        for name in ('manifest_sha256', 'setup_sha256', 'report_sha256'):
            if type(candidate[name]) is not str or not _DIGEST.fullmatch(candidate[name]):
                raise TrustError('Invalid candidate setup checkpoint digest.')
        from .standard_state import validate_pending
        pending = validate_pending(value['pending'], self.root)
        expected = self._pending(token, prior, candidate)
        expected['candidate_generation'] = pending['candidate_generation']
        if pending != expected:
            raise TrustError('The pending activation does not match its reviewed transaction.')
        known = [prior]
        if value['observed'] is not None:
            observed = validate_snapshot(value['observed'], self.root)
            self._require_candidate(observed, candidate, pending)
            known.append(observed)
        if not any(item['generation'] == self.generation_relative
                   and item['source_commit'] == self.initial_source
                   and item['manifest_sha256'] == self.initial_manifest for item in known):
            raise TrustError('The recovery does not belong to this retained current generation.')
        return value, before, release

    def _pending(self, token, prior, candidate):
        return {'schema': PROTOCOL, 'transaction': token, 'program': str(self.root),
                'previous_generation': prior['generation'], 'previous_source': prior['source_commit'],
                'previous_manifest': prior['manifest_sha256'], 'previous_python_sha256': prior['python_sha256'],
                'candidate_source': candidate['source_commit'], 'candidate_manifest': candidate['manifest_sha256'],
                'candidate_setup': candidate['setup_sha256'], 'candidate_generation': '',
                'prior_sha256': sha256(json_bytes(prior)).hexdigest()}

    @staticmethod
    def _require_candidate(snapshot, candidate, pending):
        if (not pending['candidate_generation']
                or snapshot['generation'] != pending['candidate_generation']
                or snapshot['source_commit'] != candidate['source_commit']
                or snapshot['manifest_sha256'] != candidate['manifest_sha256']
                or snapshot['setup_sha256'] != candidate['setup_sha256']):
            raise TrustError('The registered installation is not the exact reviewed new generation.')

    def _pending_for(self, record):
        pending = read_pending(self.root)
        if pending is None:
            return None
        expected = dict(record['pending'], candidate_generation=pending['candidate_generation'])
        if pending != expected or (record['pending']['candidate_generation']
                and pending['candidate_generation'] != record['pending']['candidate_generation']):
            raise TrustError('Another update or generation changed the pending activation.')
        if prior_bytes(self.root, pending) != json_bytes(record['prior']):
            raise TrustError('The retained activation snapshot differs from the recovery journal.')
        record['pending'] = pending
        return pending

    def _rollback(self, record):
        pending = self._pending_for(record)
        if pending is not None and pending['candidate_generation']:
            generation = self.root.joinpath(*pending['candidate_generation'].split('/'))
            if os.path.lexists(generation):
                directory(generation)
                marker = generation / REJECTED
                raw = json_bytes({'schema': PROTOCOL, 'transaction': record['transaction'],
                    'source_commit': pending['candidate_source'], 'manifest_sha256': pending['candidate_manifest'],
                    'reason': 'Uncommitted generation retained for diagnostics after update recovery.'})
                if os.path.lexists(marker):
                    if read_bytes(marker, 16384) != raw:
                        raise TrustError('The rejected-generation diagnostic changed.')
                else:
                    publish_new(marker, raw)
        if record['observed'] is not None:
            if pending is None:
                raise TrustError('The uncommitted activation block disappeared; preserve the pending recovery.')
            self._write(record, 'rolling-back')
            self.host.restore(self.root, record['prior'], record['observed'])
        elif self.host.snapshot(self.root) != record['prior']:
            self._write(record, 'recovery-required')
            raise TrustError('An unobserved or newer activation needs owner recovery; nothing was overwritten.')
        if self.host.snapshot(self.root) != record['prior']:
            raise TrustError('The previous registered generation has not been restored exactly.')
        self._write(record, 'rolled-back')
        clear_pending(self.root, record['pending'])
        self._settle_authority(record)
        return 'rolled-back'

    def _settle_authority(self, record):
        if self._recovery_authority is not None:
            if record['phase'] not in _TERMINAL or os.path.lexists(self.root / PENDING) or os.path.lexists(self.root / PRIOR):
                raise TrustError('Protected recovery authority can clear only after terminal pending cleanup.')
            self._recovery_authority.settle(record)

    def _recover_locked(self, package=None, *, cancelled=lambda: False):
        _cancel(cancelled)
        record, before, release = self._read()
        current = self.anchor.load()
        accepted = current.sequence == release.build and current.digest == release.sha256
        if accepted:
            if record['observed'] is None:
                raise TrustError('The committed update lacks its exact activation snapshot.')
            self._review_recovery_target(record, release, None, committed=True, cancelled=cancelled)
            pending = self._pending_for(record)
            if record['phase'] != 'complete':
                from ..library_backup import verify_backup
                verify_backup(self.work / record['backup'], record['backup_sha256'], cancelled=cancelled)
            if (self.host.snapshot(self.root) != record['observed']
                    or not self.host.health(self.root, record['observed'], release, cancelled=cancelled)):
                raise TrustError('The committed generation changed or is unhealthy; preserve data for owner recovery.')
            self._write(record, 'complete')
            clear_pending(self.root, pending or record['pending'])
            self._settle_authority(record)
            return 'complete'
        if current != before:
            raise TrustError('Protected update state changed; automatic rollback is refused.')
        if record['phase'] == 'complete':
            raise TrustError('The completed journal disagrees with protected update state.')
        if record['phase'] == 'rolled-back':
            if self.host.snapshot(self.root) != record['prior']:
                raise TrustError('A newer installation changed the completed rollback.')
            if not os.path.lexists(self.root / PENDING) and not os.path.lexists(self.root / PRIOR):
                self._settle_authority(record)
                return 'rolled-back'
        if (self._recovery_authority is None
                and self._live_prior != (record['transaction'], json_bytes(record['prior']))):
            raise TrustError('The prior native activation has no retained live authority. '
                'Owner recovery is required; no restart journal can authorize replacing '
                'registration, shortcut or uninstaller bytes. The previous program and data are preserved.')
        self._review_recovery_target(record, release, package, committed=False, cancelled=cancelled)
        if self._recovery_authority is not None:
            self._recovery_authority.require(record)
            from ..library_backup import verify_backup
            verify_backup(self.work / record['backup'], record['backup_sha256'], cancelled=cancelled)
            if self.anchor.load() != before:
                raise TrustError('Protected update state changed during restart verification.')
            current_native = self.host.snapshot(self.root)
            if current_native != record['prior'] and current_native != record['observed']:
                raise TrustError('A newer native activation cannot be replaced by this protected recovery.')
        _cancel(cancelled)
        return self._rollback(record)

    def recover(self, package=None, *, cancelled=lambda: False):
        _cancel(cancelled)
        if not os.path.lexists(self.journal):
            if os.path.lexists(self.root / PENDING) or os.path.lexists(self.root / PRIOR):
                raise TrustError('Pending standard update metadata has no configured recovery journal.')
            return 'nothing-to-recover'
        stack = ExitStack()
        try:
            stack.enter_context(locked(self.work, '.standard-update.lock'))
            stack.enter_context(self.host.lifetime(self.root))
            stack.enter_context(self.host.setup_guard())
            return self._recover_locked(package, cancelled=cancelled)
        except AudioCleanupPending as error:
            error.cleanup_owner.add_quiescent_callback(stack.pop_all().close)
            raise
        finally:
            stack.close()

    def apply(self, package, *, cancelled=lambda: False):
        from ..library_backup import create_backup, verify_backup
        require_verified_package(package, policy=self.policy)
        release = package.release
        before = self.anchor.load()
        _release(asdict(release), self.policy, before)
        if release.build <= max(before.sequence, self.policy.current_build):
            raise TrustError('The protected update state refuses a downgrade or replay.')
        if release.source_commit == self.initial_source:
            raise TrustError('A same-source repair cannot be presented as an A-to-B update.')
        _cancel(cancelled)
        stack, record, setup, guarded = ExitStack(), None, None, False
        try:
            stack.enter_context(locked(self.work, '.standard-update.lock'))
            stack.enter_context(self.host.lifetime(self.root))
            setup = ExitStack()
            setup.enter_context(self.host.setup_guard())
            guarded = True
            stack.callback(setup.close)
            if os.path.lexists(self.journal):
                old, _, _ = self._read()
                if old['phase'] not in _TERMINAL:
                    raise TrustError('Recover the pending standard update before another installation.')
                self._recover_locked()
            if os.path.lexists(self.root / PENDING) or os.path.lexists(self.root / PRIOR):
                raise TrustError('Recover existing standard update coordination before starting.')
            before, now = self.anchor.load(), int(self.clock())
            if release.build <= max(before.sequence, self.policy.current_build) or now + 300 < before.max_utc:
                raise TrustError('The protected build or system clock changed during update admission.')
            prior = self.host.snapshot(self.root)
            if (prior['generation'] != self.generation_relative or prior['source_commit'] != self.initial_source
                    or prior['manifest_sha256'] != self.initial_manifest):
                raise TrustError('The active installation changed since this controller was configured.')
            token = uuid4().hex
            stage = self.work / ('standard-stage-' + token)
            unpack(package, stage, self.policy, cancelled=cancelled)
            candidate = review_installer(stage, release, self.host, cancelled=cancelled)
            archive_name = 'standard-target-' + token + '.zip'
            self._retain_target(package, self.work / archive_name, cancelled)
            _cancel(cancelled)
            backup_name = 'standard-backup-' + token
            backup_sha = create_backup(self.app, self.work / backup_name, cancelled=cancelled)
            if self.host.snapshot(self.root) != prior:
                raise TrustError('The current activation changed during backup.')
            _cancel(cancelled)
            record = {'schema': PROTOCOL, 'program': str(self.root), 'data': str(self.app.root),
                'transaction': token, 'phase': 'prepared', 'stage': stage.name, 'archive': archive_name, 'backup': backup_name,
                'backup_sha256': backup_sha, 'before': json_value(before.encode()), 'release': asdict(release),
                'prior': prior, 'observed': None, 'candidate': candidate,
                'pending': self._pending(token, prior, candidate)}
            self._live_prior = (token, json_bytes(prior))
            self._write(record, 'prepared')
            if self._recovery_authority is not None:
                verify_backup(self.work / backup_name, backup_sha, cancelled=cancelled)
                if self.host.snapshot(self.root) != prior:
                    raise TrustError('The live prior activation changed before protected recovery publication.')
                self._recovery_authority.seal(record, before, guard=lambda: _cancel(cancelled))
            begin_pending(self.root, record['pending'], prior)
            self._write(record, 'installing')
            setup.close()  # The existing Inno mutex owns the launch interval.
            guarded = False
            code = self.host.launch(self.root, stage, candidate['installer'], token, cancelled=cancelled)
            setup.enter_context(self.host.setup_guard())
            guarded = True
            pending = self._pending_for(record)
            if pending is None:
                raise TrustError('The installer lost its pending activation receipt.')
            observed = self.host.snapshot(self.root)
            if observed == prior:
                raise TrustError('The setup did not activate the reviewed new generation.')
            self._require_candidate(observed, candidate, pending)
            record['observed'] = observed
            if self._recovery_authority is not None:
                self._recovery_authority.observe(record, before, guard=lambda: _cancel(cancelled))
            self._write(record, 'activated')
            if code or not self.host.health(self.root, observed, release, cancelled=cancelled):
                raise TrustError('The new standard installation failed its isolated health checks.')
            _cancel(cancelled)
            if self.host.snapshot(self.root) != observed:
                raise TrustError('The new activation changed during its health check.')
            verify_backup(self.work / record['backup'], record['backup_sha256'], cancelled=cancelled)
            self._write(record, 'health-checked')
            replacement = Anchor(release.build, release.sha256, max(before.max_utc, now))
            self.anchor.compare_and_set(before, replacement)
            self._write(record, 'committed')
            clear_pending(self.root, record['pending'])
            self._write(record, 'complete')
            self._settle_authority(record)
            return {'status': 'complete', 'version': release.version, 'build': release.build,
                    'source_commit': release.source_commit, 'previous': str(self.generation),
                    'generation': str(self.root.joinpath(*observed['generation'].split('/'))),
                    'backup': str(self.work / backup_name), 'backup_sha256': backup_sha,
                    'installer_sha256': candidate['setup_sha256']}
        except AudioCleanupPending as error:
            # Keep the actual shared lifetime and journal lock until the same
            # child owner confirms cleanup. No rollback can race a live clone.
            error.cleanup_owner.add_quiescent_callback(stack.pop_all().close)
            raise
        except Exception:
            if record is not None and self.anchor.load() == before:
                # Reacquire the setup guard if launch failed before normal
                # return. Any competing installer leaves recovery pending.
                if setup is not None and not guarded:
                    setup.enter_context(self.host.setup_guard())
                    guarded = True
                self._rollback(record)
            raise
        finally:
            stack.close()


def _trusted_previous_host(root, pending, previous):
    """Use only the recorded A verifier; never import proposed installer code."""
    generation = directory(root.joinpath(*pending['previous_generation'].split('/')))
    raw = read_bytes(generation / 'bundle-manifest.json', 16 * 1024**2)
    if sha256(raw).hexdigest() != pending['previous_manifest']:
        raise TrustError('The previous bundle manifest changed before the installer handoff.')
    manifest = json_value(raw, maximum=16 * 1024**2)
    script = generation / 'install_windows.py'
    source = read_bytes(script, 2 * 1024**2)
    if (type(manifest) is not dict or manifest.get('source_commit') != pending['previous_source']
            or sha256(source).hexdigest() != manifest.get('files', {}).get('install_windows.py')):
        raise TrustError('The recorded previous installer verifier changed.')
    namespace = {'__name__': '_carr_previous_standard_verifier', '__file__': str(script)}
    exec(compile(source, str(script), 'exec'), namespace)
    def validate(checked_raw, source_commit):
        if checked_raw != raw or source_commit != pending['previous_source']:
            raise TrustError('The installer handoff may inspect only its exact previous bundle.')
        return manifest
    return WindowsStandardHost(namespace['verify'], validate)


def reserve_generation(root, transaction, generation, source, manifest, setup):
    """Fixed old-host handshake called while Inno owns its existing setup mutex."""
    root = directory(root)
    pending = read_pending(root)
    if (pending is None or pending['transaction'] != transaction
            or pending['candidate_source'] != source or pending['candidate_manifest'] != manifest
            or pending['candidate_setup'] != setup):
        raise TrustError('The setup does not match the reviewed pending update.')
    previous = json_value(prior_bytes(root, pending), maximum=MAX_STATE)
    validate_snapshot(previous, root)
    host = _trusted_previous_host(root, pending, previous)
    if host.snapshot(root) != previous:
        raise TrustError('The prior registered installation changed before setup.')
    relative = Path(generation).absolute().relative_to(root).as_posix()
    generation_name(relative, source)
    if pending['candidate_generation'] or os.path.lexists(root.joinpath(*relative.split('/'))):
        raise TrustError('The new update generation was already selected or created.')
    replacement = dict(pending, candidate_generation=relative)
    replace_pending(root, pending, replacement)
    return relative


def main(argv=None):
    # This fixed helper is not an update CLI or an authority provider. It can
    # only reserve the exact reviewed generation for the current native setup.
    parser = argparse.ArgumentParser(description='Existing standard installer handoff')
    parser.add_argument('--reserve-generation', action='store_true', required=True)
    parser.add_argument('root', type=Path)
    parser.add_argument('transaction')
    parser.add_argument('generation', type=Path)
    parser.add_argument('source')
    parser.add_argument('manifest')
    parser.add_argument('setup')
    args = parser.parse_args(argv)
    reserve_generation(args.root, args.transaction, args.generation,
                       args.source, args.manifest, args.setup)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

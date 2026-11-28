"""Signed standard-setup handoff for the existing generation-based installer.

The installer remains the only writer of Windows registration, shortcuts and
uninstall ownership. This module never edits those records or removes versions.
Research data uses the current complete backup and protected monotonic anchor.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
from hashlib import sha256
import os
from pathlib import Path
import re
import stat
import time
from uuid import uuid4
import zipfile

from ._files import (TrustError, atomic_write, directory, file_info, json_bytes,
                     json_value, locked, sync_directory)
from .anchors import Anchor
from .handoff import _activation_owner, _digest, _member
from .installation import (ActivationCleanupError, WindowsBundleAdapter,
                           _HEALTH_PROGRAM, _central_budget, _verified_snapshot)
from .updates import Release, _cancel, relative_path, require_verified_package
from ..portable_entry import _read, control_root, require_clean_owners
from ..installation_lease import installation_lease

LAYOUT = b'Resonance.StandardInstall/1\r\n'
REGISTRY = r'Software\Microsoft\Windows\CurrentVersion\Uninstall\Resonance.StandardInstall_is1'
MEMBERS = {'ResonanceSetup.exe', 'installer-build.json', 'bundle-manifest.json'}
MAX_GENERATIONS = 100000


def _stamp(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _copy_verified(source, destination, expected, maximum, cancelled=lambda: False):
    """Capture or attest one ordinary file through the existing safe opener."""
    from ..durable_state import _open_json_file
    source = Path(source)
    before = file_info(source)
    if not 0 < before.st_size <= maximum:
        raise TrustError('Installer bytes exceed the configured package allowance.')
    output = None
    try:
        if destination is not None:
            output = Path(destination).open('xb')
        with _open_json_file(source) as stream:
            opened = os.fstat(stream.fileno())
            if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                    or getattr(opened, 'st_file_attributes', 0) & 0x400
                    or _stamp(opened) != _stamp(before)):
                raise TrustError('The selected installer changed before capture.')
            digest, count = sha256(), 0
            while True:
                _cancel(cancelled)
                block = stream.read(min(65536, before.st_size + 1 - count))
                if not block:
                    break
                count += len(block)
                if count > before.st_size:
                    raise TrustError('The selected installer grew during capture.')
                digest.update(block)
                if output is not None:
                    output.write(block)
            if (_stamp(os.fstat(stream.fileno())) != _stamp(before)
                    or _stamp(file_info(source)) != _stamp(before)
                    or count != before.st_size or digest.hexdigest() != expected):
                raise TrustError('Installer bytes differ from their independently recorded digest.')
        if output is not None:
            output.flush()
            os.fsync(output.fileno())
        return count
    finally:
        if output is not None:
            output.close()


def _manifest(raw, source=None):
    value = json_value(raw, maximum=16 * 1024**2)
    if (type(value) is not dict or type(value.get('schema')) is not int or value['schema'] != 1
            or type(value.get('files')) is not dict or not 1 <= len(value['files']) <= 50000
            or any(not isinstance(name, str) or not isinstance(digest, str)
                   or not re.fullmatch('[0-9a-f]{64}', digest) for name, digest in value['files'].items())
            or value.get('bundled_python') is not True
            or not re.fullmatch('[0-9a-f]{40}', str(value.get('source_commit', '')))
            or (source is not None and value['source_commit'] != source)
            or 'ResearchLibrary.exe' not in value['files'] or 'runtime/python.exe' not in value['files']
            or 'portable-update.ini' in value['files']
            or any(name.split('/', 1)[0].casefold() in {'.venv', 'maintenance-setup.exe'}
                   for name in value['files'])):
        raise TrustError('The signed setup does not describe the supported standard installation layout.')
    return value


def prepare_signed_setup(package, policy, folder, *, cancelled=lambda: False):
    """Extract only the exact current builder envelope from authenticated bytes."""
    require_verified_package(package)
    folder = directory(folder, create=True)
    if package.release.length > policy.max_package_bytes:
        raise TrustError('The setup package exceeds the download allowance.')
    with _verified_snapshot(package, folder, cancelled) as snapshot:
        _central_budget(snapshot, 3)
        with zipfile.ZipFile(snapshot) as archive:
            infos = archive.infolist()
            if len(infos) != 3 or {item.filename for item in infos} != MEMBERS:
                raise TrustError('A standard update must contain exactly the setup, build record and sealed manifest.')
            report_raw = _member(archive, 'installer-build.json', 65536)
            manifest_raw = _member(archive, 'bundle-manifest.json', 16 * 1024**2)
            report = json_value(report_raw)
            manifest = _manifest(manifest_raw, package.release.source_commit)
            fields = {'schema', 'kind', 'source_commit', 'manifest_sha256', 'payload_files', 'status',
                      'recipe_sha256', 'native_install_repair_uninstall', 'authenticode_verified',
                      'nvda_verified', 'update_or_entitlement_authority', 'automatic_network',
                      'compiler_sha256', 'installer'}
            if (type(report) is not dict or set(report) != fields
                    or type(report['schema']) is not int or report['schema'] != 1
                    or report['kind'] != 'standard-windows-installer' or report['status'] != 'compiled'
                    or report['source_commit'] != package.release.source_commit
                    or report['manifest_sha256'] != sha256(manifest_raw).hexdigest()
                    or type(report['payload_files']) is not int
                    or report['payload_files'] != len(manifest['files'])
                    or report['update_or_entitlement_authority'] is not False
                    or report['automatic_network'] is not False
                    or type(report['authenticode_verified']) is not bool
                    or type(report['nvda_verified']) is not bool
                    or not isinstance(report['native_install_repair_uninstall'], str)
                    or len(report['native_install_repair_uninstall']) > 128):
                raise TrustError('The authenticated standard setup build record is inconsistent.')
            _digest(report['recipe_sha256']); _digest(report['compiler_sha256'])
            setup = report['installer']
            if (type(setup) is not dict or set(setup) != {'path', 'sha256', 'bytes'}
                    or setup['path'] != 'dist/Resonance-Setup-' + package.release.source_commit[:12] + '.exe'
                    or type(setup['bytes']) is not int or not 0 < setup['bytes'] <= policy.max_package_bytes):
                raise TrustError('The standard setup build record has an invalid executable identity.')
            _digest(setup['sha256'])
            item = archive.getinfo('ResonanceSetup.exe')
            if (item.is_dir() or item.flag_bits & 1 or item.file_size != setup['bytes']
                    or item.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
                    or stat.S_IFMT(item.external_attr >> 16) not in (0, stat.S_IFREG)):
                raise TrustError('The signed setup executable has unsupported ZIP metadata.')
            destination = folder / 'ResonanceSetup.exe'
            with archive.open(item) as source, destination.open('xb') as output:
                digest, count = sha256(), 0
                while True:
                    _cancel(cancelled)
                    block = source.read(min(65536, setup['bytes'] + 1 - count))
                    if not block:
                        break
                    count += len(block)
                    if count > setup['bytes']:
                        raise TrustError('The signed setup executable exceeded its exact length.')
                    digest.update(block); output.write(block)
                if count != setup['bytes'] or digest.hexdigest() != setup['sha256']:
                    raise TrustError('The signed setup executable does not match the build record.')
                output.flush(); os.fsync(output.fileno())
    (folder / 'installer-build.json').write_bytes(report_raw)
    (folder / 'bundle-manifest.json').write_bytes(manifest_raw)
    sync_directory(folder)
    return report, manifest_raw


def _generations(root):
    folder = directory(Path(root) / 'versions')
    result = {}
    with os.scandir(folder) as entries:
        for count, entry in enumerate(entries, 1):
            if count > MAX_GENERATIONS:
                raise TrustError('The retained installation inventory exceeds its bounded allowance.')
            info = entry.stat(follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise TrustError('A retained installation generation is redirected.')
            result[entry.name] = (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode))
    return result


class StandardSetupAdapter(WindowsBundleAdapter):
    """Execute only the existing fixed Inno recipe; inspect registration read-only."""
    def run_setup(self, executable, root, work, *, timeout=None):
        if os.name != 'nt':
            raise TrustError('Standard setup activation requires native Windows.')
        return self._run_owned([executable, '/VERYSILENT', '/SUPPRESSMSGBOXES',
            '/NORESTART', '/SP-', '/DIR=' + str(root),
            '/LOG=' + str(work / ('setup-' + uuid4().hex + '.log'))],
            root=work, timeout=self.timeout if timeout is None else timeout)

    def generation_health(self, generation, manifest_raw, version):
        self.verify_bundle(generation, manifest_bytes=manifest_raw)
        file_info(generation / '.venv' / '.installation-complete')
        python = generation / '.venv' / 'Scripts' / 'python.exe'
        file_info(python)
        return self._run_owned([python, '-I', '-B', '-X', 'utf8', '-c',
            _HEALTH_PROGRAM, str(generation), version],
            root=generation, timeout=min(180, self.timeout)) == 0

    def registered(self, root, generation, work):
        if os.name != 'nt':
            raise TrustError('Standard registration verification requires native Windows.')
        import winreg
        def registration():
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REGISTRY, 0,
                                winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
                return {name: winreg.QueryValueEx(key, name)[0]
                        for name in ('InstallLocation', 'UninstallString', 'ModifyPath')}
        before = registration()
        if (not all(isinstance(value, str) and 0 < len(value) <= 32768 for value in before.values())
                or Path(before['InstallLocation']).resolve(strict=True) != root.resolve(strict=True)):
            raise TrustError('Windows registration belongs to another installation.')
        cached = generation / 'maintenance-setup.exe'
        if before['ModifyPath'].casefold() not in {str(cached).casefold(), ('"' + str(cached) + '"').casefold()}:
            raise TrustError('Windows Modify does not select the exact reviewed setup generation.')
        uninstaller = Path(before['UninstallString'].strip('"'))
        if (uninstaller.parent.resolve(strict=True) != root.resolve(strict=True)
                or not re.fullmatch(r'unins[0-9]{3}\.exe', uninstaller.name, re.I)):
            raise TrustError('Windows Uninstall no longer belongs to the current standard installer.')
        file_info(uninstaller)
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                r'Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders') as key:
            programs = winreg.QueryValueEx(key, 'Programs')[0]
        shortcut = Path(programs) / 'Resonance.lnk'
        file_info(shortcut)
        report = work / ('shortcut-' + uuid4().hex + '.txt')
        def literal(value):
            return "'" + str(value).replace("'", "''") + "'"
        script = ("$ErrorActionPreference='Stop'; $target=(New-Object -ComObject WScript.Shell).CreateShortcut("
                  + literal(shortcut) + ").TargetPath; [System.IO.File]::WriteAllText("
                  + literal(report) + ",$target,[System.Text.UTF8Encoding]::new($false))")
        from ..speech._io import system_powershell, windows_directory
        powershell = system_powershell(windows_directory() / 'System32' / 'WindowsPowerShell' / 'v1.0' / 'powershell.exe', architecture='64')
        status = self._run_owned([powershell, '-NoProfile', '-NonInteractive',
                                  '-Command', script], root=work, timeout=min(60, self.timeout))
        if status:
            raise TrustError('The existing Start-menu shortcut could not be independently inspected.')
        target = Path(_read(report, 65536).decode('utf-8'))
        if target.resolve(strict=True) != (generation / 'ResearchLibrary.exe').resolve(strict=True):
            raise TrustError('The Start-menu shortcut does not select the reviewed generation.')
        if registration() != before:
            raise TrustError('Windows installation selection changed during verification.')
        return True


class StandardUpdateHandoff:
    """Owner-configured signed update using the actual current setup/repair path."""
    def __init__(self, app, installed, work, policy, anchor, verify_bundle, *,
                 installed_manifest_sha256, installed_setup_sha256,
                 installed_version, timeout=900, clock=time.time, adapter=None):
        self.program = directory(installed)
        self.root = directory(self.program.parent.parent)
        if (self.program.parent.name.casefold() != 'versions'
                or not re.fullmatch('[0-9a-f]{12}-[1-9][0-9]{0,4}', self.program.name)
                or _read(self.root / 'install-layout-v1.txt', 128) != LAYOUT):
            raise TrustError('Select a generation from the current standard installer.')
        self.before = _digest(installed_manifest_sha256)
        self.old_setup = _digest(installed_setup_sha256)
        if not isinstance(installed_version, str) or not re.fullmatch(r'[0-9][A-Za-z0-9.+_-]{0,95}', installed_version):
            raise TrustError('The current application version must be independently configured.')
        self.version, self.app, self.policy, self.anchor = installed_version, app, policy, anchor
        self.clock = clock
        self.work = Path(work).absolute()
        for first, second in ((self.root, app.root), (self.work, app.root), (self.work, self.root)):
            if first.is_relative_to(second) or second.is_relative_to(first):
                raise TrustError('Standard program, maintenance workspace and research data must be separate.')
        self.work = directory(self.work, create=True)
        self.adapter = adapter if adapter is not None else StandardSetupAdapter(verify_bundle, timeout=timeout)
        self.journal = self.work / 'standard-activation.json'
        self.warning = ''
        self._completion = None

    @contextmanager
    def _maintenance(self):
        control = control_root(self.root, create=True)
        with installation_lease(control, exclusive=True):
            require_clean_owners(self.root, control)
            with _activation_owner(self.root, control):
                yield

    def _original(self):
        raw = _read(self.program / 'bundle-manifest.json', 16 * 1024**2)
        if sha256(raw).hexdigest() != self.before:
            raise TrustError('The original generation differs from its independently configured manifest.')
        value = _manifest(raw)
        if not self.program.name.startswith(value['source_commit'][:12] + '-'):
            raise TrustError('The configured standard generation has another source identity.')
        self.adapter.verify_bundle(self.program, manifest_bytes=raw)
        _copy_verified(self.program / 'maintenance-setup.exe', None, self.old_setup, self.policy.max_package_bytes)
        return raw, value

    def _write(self, record, phase):
        record['phase'] = phase
        atomic_write(self.journal, json_bytes(record))

    def _journal(self):
        raw = _read(self.journal, 32768)
        value = json_value(raw, maximum=32768)
        keys = {'schema', 'phase', 'root', 'original', 'manifest_before', 'setup_before',
                'transaction', 'candidate', 'restored', 'before', 'release', 'backup_sha256'}
        if (type(value) is not dict or set(value) != keys or value['schema'] != 'carr-standard-update/1'
                or value['phase'] not in ('prepared', 'setup-started', 'setup-complete',
                                           'health-checked', 'complete', 'rolled-back')
                or value['root'] != str(self.root)
                or not re.fullmatch('[0-9a-f]{32}', str(value['transaction']))
                or any(not re.fullmatch('[0-9a-f]{12}-[1-9][0-9]{0,4}', str(value[name]))
                       for name in ('original', 'candidate'))
                or (value['restored'] is not None and
                    not re.fullmatch('[0-9a-f]{12}-[1-9][0-9]{0,4}', str(value['restored'])))):
            raise TrustError('The standard recovery journal has an invalid operation identity.')
        for name in ('manifest_before', 'setup_before', 'backup_sha256'):
            _digest(value[name])
        release = value['release']
        if (type(release) is not dict or set(release) != set(Release.__dataclass_fields__)
                or (release['product'], release['channel'], release['data_schema'])
                != (self.policy.product, self.policy.channel, self.policy.data_schema)
                or type(release['data_schema']) is not int
                or type(release['build']) is not int or release['build'] <= 0
                or type(release['length']) is not int or not 0 < release['length'] <= self.policy.max_package_bytes
                or not isinstance(release['version'], str)
                or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9.+_-]{0,63}', release['version'])
                or not re.fullmatch('[0-9a-f]{40}', str(release['source_commit']))
                or not isinstance(release['notes'], str) or len(release['notes']) > 4096
                or any(ord(character) < 32 and character not in '\n\t' for character in release['notes'])
                or not value['candidate'].startswith(release['source_commit'][:12] + '-')):
            raise TrustError('The standard recovery journal has an invalid release identity.')
        _digest(release['sha256'])
        relative_path(release['target'])
        before = Anchor.decode(json_bytes(value['before']))
        if release['build'] <= before.sequence:
            raise TrustError('The standard recovery journal does not advance protected state.')
        return raw, value, before

    def _record(self, package):
        _raw, value, before = self._journal()
        if ((value['original'], value['manifest_before'], value['setup_before'])
                != (self.program.name, self.before, self.old_setup)
                or value['release'] != asdict(package.release)):
            raise TrustError('The standard recovery journal differs from its configured signed operation.')
        return value, before

    def _archive_terminal(self):
        """Retain prior evidence only after independently proving current selection.

        A journal may describe history; it never selects a program, rollback
        setup, signing root or protected state for the next update.
        """
        from ..durable_state import atomic_bytes
        raw, value, before = self._journal()
        if value['phase'] not in ('complete', 'rolled-back'):
            raise TrustError('Recover the interrupted standard update first.')
        current = self.anchor.load()
        manifest_raw, manifest = self._original()
        release = value['release']
        if value['phase'] == 'complete':
            valid = (value['candidate'] == self.program.name
                     and (current.sequence, current.digest) == (release['build'], release['sha256'])
                     and (release['source_commit'], release['version'])
                     == (manifest['source_commit'], self.version))
        else:
            valid = (current == before
                     and (value['restored'] or value['original']) == self.program.name
                     and (value['manifest_before'], value['setup_before']) == (self.before, self.old_setup))
        if not valid:
            raise TrustError('The terminal journal does not match the independently configured current generation.')
        self._health(self.program, manifest_raw, self.version, self.old_setup)
        if self.anchor.load() != current or _read(self.journal, 32768) != raw:
            raise TrustError('Protected state or the terminal journal changed before archival.')
        archive = self.work / ('history-' + value['transaction'] + '.json')
        if os.path.lexists(archive):
            if _read(archive, 32768) != raw:
                raise TrustError('A different retained journal already occupies this transaction identity.')
        else:
            atomic_bytes(archive, raw, replace=False)
        # A failed acknowledgement leaves the original journal in place; retry
        # requires the exact archived bytes and never replaces previous history.
        if (_read(archive, 32768) != raw or _read(self.journal, 32768) != raw
                or self.anchor.load() != current):
            raise TrustError('The preserved terminal journal changed before the next operation.')
        self.adapter.registered(self.root, self.program, self.work)
        self._seal(self.program, manifest_raw, self.old_setup)
        self.journal.unlink()
        sync_directory(self.work)


    def _next(self, source, existing):
        for index in range(1, 100000):
            name = source[:12] + '-' + str(index)
            if name not in existing:
                return self.root / 'versions' / name
        raise TrustError('No unused standard installation generation is available.')

    def _seal(self, generation, manifest_raw, setup_sha256):
        if _read(generation / 'bundle-manifest.json', 16 * 1024**2) != manifest_raw:
            raise TrustError('The setup published a different sealed manifest.')
        self.adapter.verify_bundle(generation, manifest_bytes=manifest_raw)
        _copy_verified(generation / 'maintenance-setup.exe', None, setup_sha256, self.policy.max_package_bytes)

    def _health(self, generation, manifest_raw, version, setup_sha256):
        self._seal(generation, manifest_raw, setup_sha256)
        if not self.adapter.generation_health(generation, manifest_raw, version):
            raise TrustError('The standard setup generation failed its installed health check.')
        self.adapter.registered(self.root, generation, self.work)
        self._seal(generation, manifest_raw, setup_sha256)

    def _invoke(self, executable, generation, source, setup_sha256):
        existing = _generations(self.root)
        if self._next(source, existing) != generation:
            raise TrustError('Another installer consumed the reviewed generation before launch.')
        _copy_verified(executable, None, setup_sha256, self.policy.max_package_bytes)
        status = self.adapter.run_setup(executable, self.root, self.work)
        if status:
            raise TrustError('The existing standard installer did not complete successfully.')
        observed = _generations(self.root)
        if (set(observed) - set(existing) != {generation.name}
                or any(observed.get(name) != identity for name, identity in existing.items())):
            raise TrustError('The standard installer did not add exactly one fresh generation while preserving prior entries.')

    def _rollback(self, record, before, package, prepared):
        if self.anchor.load() != before:
            raise TrustError('Protected update state changed; standard rollback is refused.')
        old_raw, old_manifest = self._original()
        candidate = self.root / 'versions' / record['candidate']
        # If setup never changed selection, no repair is necessary. An unrelated
        # selection is never overwritten merely because a mutable journal exists.
        try:
            self.adapter.registered(self.root, self.program, self.work)
            self._health(self.program, old_raw, self.version, self.old_setup)
            restored = self.program
        except (TrustError, OSError):
            self.adapter.registered(self.root, candidate, self.work)
            original_copy = prepared / 'OriginalSetup.exe'
            if not os.path.lexists(original_copy):
                _copy_verified(self.program / 'maintenance-setup.exe', original_copy,
                               self.old_setup, self.policy.max_package_bytes)
            restored = self._next(old_manifest['source_commit'], _generations(self.root))
            self._invoke(original_copy, restored, old_manifest['source_commit'], self.old_setup)
            self._health(restored, old_raw, self.version, self.old_setup)
        record['restored'] = restored.name
        self._write(record, 'rolled-back')
        return {'status': 'rolled-back', 'program': str(restored), 'retained_candidate': str(candidate),
                'backup': str(self.work / ('backup-' + record['transaction']))}

    def _completed(self, record, package, report, manifest_raw, *, warning='', health=True):
        candidate = self.root / 'versions' / record['candidate']
        protected = self.anchor.load()
        if protected.sequence != package.release.build or protected.digest != package.release.sha256:
            raise TrustError('The standard update has no matching protected commit.')
        if health:
            self._health(candidate, manifest_raw, package.release.version, report['installer']['sha256'])
        else:
            self._seal(candidate, manifest_raw, report['installer']['sha256'])
            self.adapter.registered(self.root, candidate, self.work)
            self._seal(candidate, manifest_raw, report['installer']['sha256'])
        return {'status': 'complete', 'version': package.release.version, 'build': package.release.build,
                'source_commit': package.release.source_commit, 'program': str(candidate),
                'previous': str(self.program),
                'backup': str(self.work / ('backup-' + record['transaction'])),
                'backup_sha256': record['backup_sha256'],
                'installed_manifest_sha256': report['manifest_sha256'],
                'installed_setup_sha256': report['installer']['sha256'],
                'completion_confirmed': not bool(warning), 'warning': warning,
                'restart': 'not-requested'}

    def _apply(self, package, *, cancelled=lambda: False):
        from ..library_backup import create_backup
        from ..ingest.writer_guard import exclusive_writer
        require_verified_package(package)
        if (package.release.product, package.release.channel, package.release.data_schema) != (
                self.policy.product, self.policy.channel, self.policy.data_schema):
            raise TrustError('The signed setup targets another product, channel or data schema.')
        self.warning = ''
        with locked(self.work, '.standard-update.lock'):
            before, now = self.anchor.load(), int(self.clock())
            if now + 300 < before.max_utc or package.release.build <= max(before.sequence, self.policy.current_build):
                raise TrustError('Protected state refuses rollback, replay or an unreviewed backward clock.')
            old_raw, old_manifest = self._original()
            self.adapter.registered(self.root, self.program, self.work)
            if os.path.lexists(self.journal):
                self._archive_terminal()
            token = uuid4().hex
            prepared = self.work / ('setup-' + token)
            prepared.mkdir()
            report, manifest_raw = prepare_signed_setup(package, self.policy, prepared, cancelled=cancelled)
            _copy_verified(self.program / 'maintenance-setup.exe', prepared / 'OriginalSetup.exe',
                           self.old_setup, self.policy.max_package_bytes, cancelled)
            backup = self.work / ('backup-' + token)
            checksum = create_backup(self.app, backup, cancelled=cancelled)
            candidate = self._next(package.release.source_commit, _generations(self.root))
            record = dict(schema='carr-standard-update/1', phase='prepared', root=str(self.root),
                          original=self.program.name, manifest_before=self.before, setup_before=self.old_setup,
                          transaction=token, candidate=candidate.name, restored=None,
                          before=json_value(before.encode()), release=asdict(package.release), backup_sha256=checksum)
            self._write(record, 'prepared')
            with exclusive_writer(self.app.root):
                _cancel(cancelled)
                self._original()
                self.adapter.registered(self.root, self.program, self.work)
                self._write(record, 'setup-started')
                try:
                    _cancel(cancelled)
                    self._invoke(prepared / 'ResonanceSetup.exe', candidate,
                                 package.release.source_commit, report['installer']['sha256'])
                    self._write(record, 'setup-complete')
                    self._health(candidate, manifest_raw, package.release.version, report['installer']['sha256'])
                    self._original()
                    self._write(record, 'health-checked')
                    self._completion = record, package, report, manifest_raw
                    self.anchor.compare_and_set(before, Anchor(package.release.build, package.release.sha256,
                                                               max(before.max_utc, now)))
                except ActivationCleanupError:
                    raise
                except (Exception, KeyboardInterrupt):
                    if self.anchor.load() == before:
                        self._rollback(record, before, package, prepared)
                    raise
            try:
                self._write(record, 'complete')
            except (Exception, KeyboardInterrupt) as error:
                self.warning = ('The signed standard update is committed; final journal acknowledgement '
                                'is incomplete. Preserve the recovery workspace. ' + str(error)[:1000])
            return self._completed(record, package, report, manifest_raw, warning=self.warning)

    def _recover(self, package, *, cancelled=lambda: False):
        from ..ingest.writer_guard import exclusive_writer
        require_verified_package(package)
        if not os.path.lexists(self.journal):
            return {'status': 'nothing-to-recover'}
        with locked(self.work, '.standard-update.lock'), exclusive_writer(self.app.root):
            record, before = self._record(package)
            prepared = self.work / ('recovery-' + uuid4().hex)
            prepared.mkdir()
            report, manifest_raw = prepare_signed_setup(package, self.policy, prepared, cancelled=cancelled)
            current = self.anchor.load()
            if (current.sequence, current.digest) == (package.release.build, package.release.sha256):
                self._completion = record, package, report, manifest_raw
                result = self._completed(record, package, report, manifest_raw)
                self._write(record, 'complete')
                return result
            if current != before:
                raise TrustError('Protected state changed; automatic standard recovery is refused.')
            if record['phase'] == 'rolled-back':
                old_raw, _ = self._original()
                restored = self.root / 'versions' / (record['restored'] or self.program.name)
                self._health(restored, old_raw, self.version, self.old_setup)
                return {'status': 'rolled-back', 'program': str(restored)}
            return self._rollback(record, before, package, prepared)

    def _operation(self, operation, package, cancelled):
        self._completion, self.warning = None, ''
        try:
            with self._maintenance():
                return operation(package, cancelled=cancelled)
        except (Exception, KeyboardInterrupt) as error:
            if self._completion is None:
                raise
            warning = ('The signed standard update has a protected commit and the exact selected '
                       'generation is present; final acknowledgement or cleanup is incomplete. '
                       'Keep the maintenance workspace and owner receipts. ' + str(error)[:1000])
            try:
                result = self._completed(*self._completion, warning=warning, health=False)
            except (Exception, KeyboardInterrupt):
                raise error
            self.warning = warning
            return result

    def apply(self, package, *, cancelled=lambda: False):
        return self._operation(self._apply, package, cancelled)

    def recover(self, package, *, cancelled=lambda: False):
        return self._operation(self._recover, package, cancelled)

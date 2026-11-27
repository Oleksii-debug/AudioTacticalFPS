"""Stage authenticated bundles and replace only an idle program directory.

The existing library backup and installer verifier remain authoritative. The
host supplies an idle lease that prevents new jobs for the whole transaction.
No source file, library root, model directory or prior output is retargeted.
Failed/crashed activation retains the previous installation and backup.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
import hashlib
import os
from pathlib import Path
import shutil
import stat
import struct
import subprocess
import tempfile
import time
from typing import Protocol
import uuid
import zipfile

from ..audio.engines import _start_process, _stop
from ..audio.jobs import publish_noreplace, verified_reader
from ._files import TrustError, atomic_write, component, directory, file_info, json_bytes, json_value, locked, read_bytes, sync_directory
from .anchors import Anchor, AnchorStore
from .updates import Release, UpdatePolicy, VerifiedPackage, _cancel, relative_path, require_verified_package


class BundleAdapter(Protocol):
    def verify(self, root: Path) -> None: ...
    def prepare(self, root: Path, release: Release) -> None: ...
    def activate(self, root: Path, release: Release) -> None: ...
    def health(self, root: Path, release: Release) -> bool: ...


def verify_package(package: VerifiedPackage):
    """Verify exact receipt-bound bytes without exposing a mutable pathname."""
    require_verified_package(package)
    with _verified_snapshot(package, Path(package.path).parent, lambda: False):
        pass


@contextmanager
def _verified_snapshot(package, parent, cancelled):
    require_verified_package(package)
    _cancel(cancelled)
    info = file_info(package.path)
    if info.st_size != package.release.length:
        raise TrustError('The reviewed update package length changed.')
    parent = directory(parent)
    if shutil.disk_usage(parent).free < info.st_size + 16 * 1024**2:
        raise TrustError('Not enough free space to verify the update package.')
    with tempfile.TemporaryFile(dir=parent) as snapshot:
        with verified_reader(package.path) as source:
            opened = os.fstat(source.fileno())
            if (opened.st_dev, opened.st_ino, opened.st_size) != (info.st_dev, info.st_ino, info.st_size):
                raise TrustError('The reviewed update package changed while opening.')
            digest, count = hashlib.sha256(), 0
            while block := source.read(65536):
                _cancel(cancelled)
                count += len(block)
                if count > package.release.length:
                    raise TrustError('The reviewed update package length changed.')
                snapshot.write(block)
                digest.update(block)
        _cancel(cancelled)
        if count != package.release.length or digest.hexdigest() != package.release.sha256:
            raise TrustError('The reviewed update package checksum changed.')
        # Only the anonymous private snapshot is used below. A path replacement
        # or in-place rewrite of the downloaded file cannot substitute bytes.
        snapshot.flush()
        snapshot.seek(0)
        yield snapshot


def _central_budget(stream, max_files):
    """Bound the central directory before ZipFile allocates its entry objects.

    CPython's bounded EOCD/ZIP64 reader locates the directory. Fixed central
    headers are scanned without constructing ZipInfo objects or reading data.
    Only ordinary, single-volume ZIPs are supported by this update transport.
    """
    end = zipfile._EndRecData(stream)
    if (end is None or end[zipfile._ECD_DISK_NUMBER] != 0
            or end[zipfile._ECD_DISK_START] != 0
            or not 1 <= end[zipfile._ECD_ENTRIES_TOTAL] <= max_files
            or end[zipfile._ECD_ENTRIES_THIS_DISK] != end[zipfile._ECD_ENTRIES_TOTAL]
            or not 0 < end[zipfile._ECD_SIZE] <= 16 * 1024**2):
        raise TrustError('Update archive central directory exceeds its entry budget or format policy.')
    offset, size = end[zipfile._ECD_OFFSET], end[zipfile._ECD_SIZE]
    if offset < 0 or offset + size > end[zipfile._ECD_LOCATION]:
        raise TrustError('Invalid update archive central directory bounds.')
    stream.seek(offset)
    count = 0
    while stream.tell() < offset + size:
        header = stream.read(zipfile.sizeCentralDir)
        if len(header) != zipfile.sizeCentralDir:
            raise TrustError('Truncated update archive central directory.')
        fields = struct.unpack(zipfile.structCentralDir, header)
        if fields[zipfile._CD_SIGNATURE] != zipfile.stringCentralDir:
            raise TrustError('Invalid update archive central directory record.')
        count += 1
        if count > max_files or fields[zipfile._CD_FILENAME_LENGTH] > 4096:
            raise TrustError('Update archive central directory exceeds its entry budget.')
        following = (fields[zipfile._CD_FILENAME_LENGTH] + fields[zipfile._CD_EXTRA_FIELD_LENGTH]
                     + fields[zipfile._CD_COMMENT_LENGTH])
        if stream.tell() + following > offset + size:
            raise TrustError('Truncated update archive central directory fields.')
        stream.seek(following, 1)
    if stream.tell() != offset + size or count != end[zipfile._ECD_ENTRIES_TOTAL]:
        raise TrustError('Update archive central directory count mismatch.')


def unpack(package, destination, policy, *, cancelled=lambda: False):
    """Bounded extraction of a TUF-verified ZIP into a new local directory."""
    require_verified_package(package, policy=policy)
    if not 0 < package.release.length <= policy.max_package_bytes:
        raise TrustError('Update target exceeds the download budget.')
    destination = Path(destination).absolute()
    directory(destination.parent)
    component(destination.name)
    if os.path.lexists(destination):
        raise FileExistsError('Update staging must use a new directory.')
    with _verified_snapshot(package, destination.parent, cancelled) as snapshot:
        _central_budget(snapshot, policy.max_files)
        with zipfile.ZipFile(snapshot) as archive:
            infos = archive.infolist()
            if not 1 <= len(infos) <= policy.max_files:
                raise TrustError('Update archive exceeds its entry budget.')
            seen, directories, spellings, total = set(), set(), {}, 0
            members = []
            for info in infos:
                _cancel(cancelled)
                name = info.filename[:-1] if info.is_dir() else info.filename
                parts = relative_path(name)
                for end in range(1, len(parts) + 1):
                    spelling = '/'.join(parts[:end])
                    prior = spellings.setdefault(spelling.casefold(), spelling)
                    if prior != spelling:
                        raise TrustError('Update archive aliases a directory or file spelling.')
                key = '/'.join(parts).casefold()
                if key in seen:
                    raise TrustError('Update archive contains duplicate or aliased paths.')
                seen.add(key)
                kind = stat.S_IFMT(info.external_attr >> 16)
                if (info.flag_bits & 1 or kind not in (0, stat.S_IFREG, stat.S_IFDIR)
                        or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
                        or (kind == stat.S_IFDIR) != info.is_dir() and kind != 0):
                    raise TrustError('Update archive contains an encrypted, linked or unsupported entry.')
                if parts[0].casefold() == '.venv':
                    raise TrustError('A bundle cannot inject an already installed environment.')
                for end in range(1, len(parts)):
                    directories.add('/'.join(parts[:end]).casefold())
                if info.is_dir():
                    directories.add(key)
                total += info.file_size
                if info.file_size < 0 or total > policy.max_unpacked_bytes:
                    raise TrustError('Update archive exceeds its unpacked-byte budget.')
                members.append((info, parts))
            files = {('/'.join(parts)).casefold() for info, parts in members if not info.is_dir()}
            if files & directories:
                raise TrustError('Update archive aliases a file with a directory.')
            if shutil.disk_usage(destination.parent).free < total + 16 * 1024**2:
                raise TrustError('Not enough free space to stage the update.')
            destination.mkdir(mode=0o700)
            # Never publish this partial directory as the current application.
            for info, parts in members:
                _cancel(cancelled)
                output = destination.joinpath(*parts)
                directory(output.parent, create=True)
                if info.is_dir():
                    output.mkdir(exist_ok=True)
                    continue
                written = 0
                with archive.open(info) as source, output.open('xb') as target:
                    while block := source.read(65536):
                        _cancel(cancelled)
                        written += len(block)
                        if written > info.file_size:
                            raise TrustError('Update member exceeded its declared length.')
                        target.write(block)
                    if written != info.file_size:
                        raise TrustError('Update member length mismatch.')
                    target.flush()
                    os.fsync(target.fileno())
            sync_directory(destination)
    return destination


def _owned_process(command, *, cwd, env, timeout):
    """Use the application's sole native process ownership boundary."""
    process = _start_process(command, cwd=cwd, env=env,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
    try:
        return process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        raise TrustError('The offline installation process exceeded its time budget.') from None
    finally:
        # The same owned Job/session retains descendants even if its leader
        # already exited. A failed installer cannot leave orphaned pip/helpers.
        _stop(process)


def _verify_release_payload(root, release, verify_bundle):
    """Read-only payload checks shared by independently owned update paths.

    This verifies bytes and release identity; it grants no installation or
    activation authority. Each caller first enforces its own layout and owner.
    """
    root = directory(root)
    # Bind the trusted verifier to the same bounded manifest snapshot whose
    # source identity we compare with the authenticated TUF target metadata.
    raw = read_bytes(root / 'bundle-manifest.json', 16 * 1024**2)
    manifest = json_value(raw, maximum=16 * 1024**2)
    if type(manifest) is not dict or manifest.get('source_commit') != release.source_commit:
        raise TrustError('The bundle source commit differs from the signed release.')
    verify_bundle(root, manifest_bytes=raw)
    # The refresh producer also includes its existing exact-wheel receipt.
    # Standard bundles need no additional provenance schema here.
    proof_path = root / 'application-source.json'
    files = manifest['files']
    if os.path.lexists(proof_path) or 'application-source.json' in files:
        proof_raw = read_bytes(proof_path, 16384)
        if hashlib.sha256(proof_raw).hexdigest() != files.get('application-source.json'):
            raise TrustError('The application source receipt is outside the verified inventory.')
        proof = json_value(proof_raw, maximum=16384)
        wheels = [digest for name, digest in files.items()
                  if name.startswith('wheelhouse/universal_research_library-') and name.endswith('.whl')]
        if (type(proof) is not dict or proof.get('source_commit') != release.source_commit
                or proof.get('project_name') != 'universal-research-library'
                or proof.get('project_version') != release.version
                or len(wheels) != 1 or proof.get('wheel_sha256') != wheels[0]):
            raise TrustError('The application source receipt differs from the signed release or wheel.')

    return raw


class WindowsBundleAdapter:
    """Use the installed, reviewed packaging verifier and signed bundle installer.

    verify_bundle is packaging/install_windows.py:verify from the trusted host
    installation, not code imported from the proposed bundle during preview.
    Actual Windows release/installer acceptance is a separate native gate.
    """
    def __init__(self, verify_bundle, *, bootstrap_python: Path | None = None, timeout=900):
        if not 1 <= timeout <= 1800:
            raise TrustError('Invalid installer timeout.')
        self.verify_bundle = verify_bundle
        self.bootstrap_python = Path(bootstrap_python) if bootstrap_python is not None else None
        self.timeout = timeout

    @staticmethod
    def _require_portable(root):
        root = Path(root).absolute()
        legacy = (root.name.casefold() == 'app'
                  and os.path.lexists(root.parent / 'managed-install.ini'))
        versioned = (root.parent.name.casefold() == 'versions'
                     and os.path.lexists(root.parent.parent / 'install-layout-v1.txt'))
        if legacy or versioned:
            raise TrustError('This managed installation requires its reviewed setup/maintenance procedure. '
                             'A program-only update cannot replace installer ownership receipts.')

    def verify(self, root):
        self._require_portable(root)
        self.verify_bundle(root)

    def _verify_release(self, root, release):
        self._require_portable(root)
        return _verify_release_payload(root, release, self.verify_bundle)

    @staticmethod
    def _environment():
        env = {key: value for key, value in os.environ.items()
               if not key.upper().startswith(('PYTHON', 'PIP_'))
               and key.upper() not in {'FFREPORT', 'SSLKEYLOGFILE'}}
        env.update(PIP_NO_INDEX='1', PYTHONDONTWRITEBYTECODE='1', PYTHONNOUSERSITE='1')
        return env

    def prepare(self, root, release):
        self._require_portable(root)
        if os.name != 'nt':
            raise TrustError('The production bundle installer requires native Windows.')
        self._verify_release(root, release)
        python = root / 'runtime' / 'python.exe'
        if not python.is_file():
            python = self.bootstrap_python
        if python is None:
            raise TrustError('No reviewed installation Python is available.')
        file_info(python)
        if os.path.lexists(root / '.venv'):
            raise TrustError('A staged update cannot contain an existing virtual environment.')

    def activate(self, root, release):
        # The existing installer embeds absolute venv/runtime paths. Build the
        # environment only at its final path, while the host holds its idle
        # lease and A is retained for rollback. A running UI must use an owner
        # restart helper outside the program directory to call this method.
        self.prepare(root, release)
        python = root / 'runtime' / 'python.exe'
        if not python.is_file():
            python = self.bootstrap_python
        code = _owned_process([str(python), '-I', '-B', '-X', 'utf8', str(root / 'install_windows.py')],
            cwd=root, env=self._environment(), timeout=self.timeout)
        if code or not (root / '.venv' / '.installation-complete').is_file():
            raise TrustError('The offline installer did not complete. The installed application is unchanged.')

    def health(self, root, release):
        self._require_portable(root)
        python = root / '.venv' / 'Scripts' / 'python.exe'
        file_info(python)
        self._verify_release(root, release)
        if not (root / '.venv' / '.installation-complete').is_file():
            return False
        for arguments in (
                ['-m', 'carr_library.native_checks', '--bundle-root', str(root)],
                ['-m', 'carr_library', '--self-test'],
                ['-c', "from importlib.metadata import version; import sys; sys.exit(version('universal-research-library') != sys.argv[1])",
                 release.version]):
            code = _owned_process([str(python), '-I', '-B', '-X', 'utf8', *arguments],
                cwd=root, env=self._environment(), timeout=min(180, self.timeout))
            if code:
                return False
        return True


class UpdateTransaction:
    def __init__(self, app, installed, work, policy: UpdatePolicy,
                 anchor: AnchorStore, adapter: BundleAdapter, *, idle_lease, clock=time.time):
        self.app = app
        self.installed = Path(installed).absolute()
        directory(self.installed.parent)
        component(self.installed.name)
        if os.path.lexists(self.installed):
            directory(self.installed)
        self.work = directory(work, create=True)
        self.policy, self.anchor, self.adapter = policy, anchor, adapter
        self.idle_lease, self.clock = idle_lease, clock
        for first, second in ((self.installed, app.root), (self.work, app.root), (self.work, self.installed)):
            if first.is_relative_to(second) or second.is_relative_to(first):
                raise TrustError('Program, update workspace and research data must be separate directories.')
        if self.work.stat().st_dev != self.installed.parent.stat().st_dev:
            raise TrustError('Update workspace must share the program filesystem for atomic activation.')
        self.journal = self.work / 'activation.json'
        if not self.installed.exists() and not self.journal.exists():
            raise TrustError('The installed program is absent and no update recovery journal exists.')

    def _write(self, record, phase):
        record['phase'] = phase
        atomic_write(self.journal, json_bytes(record))

    def _record(self):
        value = json_value(read_bytes(self.journal, 16384), maximum=16384)
        keys = {'version', 'phase', 'program', 'data', 'transaction', 'stage', 'previous', 'failed',
                'backup', 'backup_sha256', 'before', 'release'}
        if (type(value) is not dict or set(value) != keys
                or type(value['version']) is not int or value['version'] != 1):
            raise TrustError('Unsupported update recovery journal.')
        if value['phase'] not in ('prepared', 'old-moved', 'new-active', 'health-checked', 'complete', 'rolled-back'):
            raise TrustError('Invalid update recovery phase.')
        if value['program'] != str(self.installed) or value['data'] != str(self.app.root):
            raise TrustError('Update recovery belongs to a different program or data root.')
        token = value['transaction']
        if not isinstance(token, str) or len(token) != 32 or any(c not in '0123456789abcdef' for c in token):
            raise TrustError('Invalid update transaction identity.')
        expected = {'stage': 'stage-' + token, 'previous': '.carr-previous-' + token,
                    'failed': 'failed-' + token, 'backup': 'backup-' + token}
        if any(value[key] != name for key, name in expected.items()):
            raise TrustError('Update recovery paths do not match their transaction.')
        before = Anchor.decode(json_bytes(value['before']))
        if not isinstance(value['release'], dict) or set(value['release']) != set(Release.__dataclass_fields__):
            raise TrustError('Invalid update release checkpoint.')
        release = Release(**value['release'])
        if (release.product, release.channel, release.data_schema) != (
                self.policy.product, self.policy.channel, self.policy.data_schema):
            raise TrustError('Update recovery product, channel or schema does not match.')
        if type(release.build) is not int or release.build <= before.sequence:
            raise TrustError('Invalid update recovery build.')
        for digest in (release.sha256, value['backup_sha256']):
            if not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
                raise TrustError('Invalid update recovery digest.')
        return value, before, release

    def _paths(self, record):
        return (self.work / record['stage'], self.installed.parent / record['previous'],
                self.work / record['failed'])

    def _rollback(self, record):
        stage, previous, failed = self._paths(record)
        if os.path.lexists(previous):
            directory(previous)
            self.adapter.verify(previous)
            if os.path.lexists(self.installed):
                directory(self.installed)
                if os.path.lexists(failed):
                    raise TrustError('Failed update directory already exists. Manual recovery is required.')
                publish_noreplace(self.installed, failed)
            publish_noreplace(previous, self.installed)
            sync_directory(self.installed.parent)
        elif not self.installed.is_dir():
            raise TrustError('Previous installation is unavailable. Keep the backup and ask the owner to recover.')
        self._write(record, 'rolled-back')

    def recover(self):
        """After restart, finish a committed healthy update or roll back to A."""
        WindowsBundleAdapter._require_portable(self.installed)
        if not os.path.lexists(self.journal):
            return 'nothing-to-recover'
        with locked(self.work, '.update.lock'), self.idle_lease():
            record, before, release = self._record()
            if record['phase'] in ('complete', 'rolled-back'):
                return record['phase']
            current = self.anchor.load()
            if current.sequence == release.build and current.digest == release.sha256:
                directory(self.installed)
                self.adapter.verify(self.installed)
                if not self.adapter.health(self.installed, release):
                    raise TrustError('A committed update failed recovery health checks. Preserve data and use owner recovery.')
                self._write(record, 'complete')
                return 'complete'
            if current != before:
                raise TrustError('Protected update state changed. Automatic rollback is refused.')
            self._rollback(record)
            return 'rolled-back'

    def apply(self, package: VerifiedPackage, *, cancelled=lambda: False):
        WindowsBundleAdapter._require_portable(self.installed)
        from ..library_backup import create_backup
        from ..ingest.writer_guard import exclusive_writer
        require_verified_package(package)
        release = package.release
        if (release.product, release.channel, release.data_schema) != (
                self.policy.product, self.policy.channel, self.policy.data_schema):
            raise TrustError('Update product, channel or schema does not match.')
        with locked(self.work, '.update.lock'), self.idle_lease():
            if os.path.lexists(self.journal):
                record, _, _ = self._record()
                if record['phase'] not in ('complete', 'rolled-back'):
                    raise TrustError('Recover the interrupted update before preparing another.')
            before, now = self.anchor.load(), int(self.clock())
            if now + 300 < before.max_utc:
                raise TrustError('The system clock moved backwards; update approval requires review.')
            if release.build <= max(before.sequence, self.policy.current_build):
                raise TrustError('Protected update state refuses a rollback or replay.')
            self.adapter.verify(self.installed)
            token = uuid.uuid4().hex
            record = dict(version=1, phase='prepared', program=str(self.installed), data=str(self.app.root),
                          transaction=token, stage='stage-' + token, previous='.carr-previous-' + token,
                          failed='failed-' + token, backup='backup-' + token, backup_sha256='',
                          before=json_value(before.encode()), release=asdict(release))
            stage, previous, failed = self._paths(record)
            unpack(package, stage, self.policy, cancelled=cancelled)
            self.adapter.verify(stage)
            self.adapter.prepare(stage, release)
            _cancel(cancelled)
            record['backup_sha256'] = create_backup(self.app, self.work / record['backup'], cancelled=cancelled)
            _cancel(cancelled)
            if os.path.lexists(previous) or os.path.lexists(failed):
                raise TrustError('Update transaction destination unexpectedly exists.')
            self._write(record, 'prepared')
            with exclusive_writer(self.app.root):
                try:
                    publish_noreplace(self.installed, previous)
                    sync_directory(self.installed.parent)
                    self._write(record, 'old-moved')
                    publish_noreplace(stage, self.installed)
                    sync_directory(self.installed.parent)
                    self._write(record, 'new-active')
                    self.adapter.activate(self.installed, release)
                    if not self.adapter.health(self.installed, release):
                        raise TrustError('The new application failed its health check.')
                    self._write(record, 'health-checked')
                    replacement = Anchor(release.build, release.sha256, max(before.max_utc, now))
                    self.anchor.compare_and_set(before, replacement)
                except Exception:
                    # A committed anchor must never be rolled back after a
                    # final-journal write failure. Recover() can finalize it.
                    current = self.anchor.load()
                    if current == before:
                        self._rollback(record)
                    raise
            self._write(record, 'complete')
            return {'status': 'complete', 'version': release.version, 'build': release.build,
                    'backup': str(self.work / record['backup']), 'backup_sha256': record['backup_sha256'],
                    'previous': str(previous), 'source_commit': release.source_commit}

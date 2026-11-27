"""Explicit external activation for the sealed portable ownership protocol.

This is the host integration for the existing authenticated UpdateTransaction,
not a downloader, automatic updater, installer, or alternate trust store.
A configured helper must run from outside the program and retain its trusted
configuration and verifier across replacement/recovery. Managed setup metadata
requires its authenticated setup handoff and is deliberately not migrated here.
"""
from __future__ import annotations

from contextlib import contextmanager
from hashlib import sha256
import os
from pathlib import Path
import stat
import subprocess
import sys
import time
import zipfile

from .. import installation_lease as leases
from ..portable_entry import (
    PORTABLE_CONTRACT, PORTABLE_MARKER, control_root, installed_ownership, managed_layout, portable_contract,
)
from ._files import TrustError, directory, json_value, read_bytes
from .installation import (
    UpdateTransaction, WindowsBundleAdapter, _central_budget, _verified_snapshot,
)
from .updates import _cancel, require_verified_package


def _digest(value):
    if (not isinstance(value, str) or len(value) != 64
            or any(character not in '0123456789abcdef' for character in value)):
        raise TrustError('The current portable manifest requires its independently retained SHA256.')
    return value


def _external_runtime(program, work, data, verifier):
    """Never move the helper's runtime, imports, verifier, or working directory."""
    code = getattr(verifier, '__code__', None)
    if code is None:
        raise TrustError('The external helper requires an explicit trusted file-based bundle verifier.')
    places = (Path(sys.executable), Path(sys.prefix), Path(__file__),
              Path(leases.__file__), Path(code.co_filename), Path.cwd())
    for value in places:
        locations = (value.absolute(), value.resolve(strict=True))
        if any(location.is_relative_to(root) for location in locations for root in (program, work, data)):
            raise TrustError('The update helper, verifier, runtime and working directory must remain outside program, update workspace and research data.')


def _member(archive, name, maximum):
    matches = [info for info in archive.infolist() if info.filename.casefold() == name.casefold()]
    if len(matches) != 1 or matches[0].filename != name:
        raise TrustError('The authenticated package has ambiguous or missing portable metadata.')
    info = matches[0]
    if (info.is_dir() or info.flag_bits & 1 or not 0 <= info.file_size <= maximum
            or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
            or stat.S_IFMT(info.external_attr >> 16) not in (0, stat.S_IFREG)):
        raise TrustError('The portable package metadata is linked, encrypted or outside its bound.')
    with archive.open(info) as stream:
        raw = stream.read(maximum + 1)
    if len(raw) != info.file_size or len(raw) > maximum:
        raise TrustError('The portable package metadata exceeded its declared length.')
    return raw


def candidate_manifest(package, policy, workspace, *, cancelled=lambda: False):
    """Bind B's manifest to the same private hash-verified package snapshot."""
    require_verified_package(package)
    if not 0 < package.release.length <= policy.max_package_bytes:
        raise TrustError('Update target exceeds the download budget.')
    with _verified_snapshot(package, workspace, cancelled) as snapshot:
        _central_budget(snapshot, policy.max_files)
        with zipfile.ZipFile(snapshot) as archive:
            raw = _member(archive, 'bundle-manifest.json', 16 * 1024**2)
            marker = _member(archive, PORTABLE_MARKER, 256)
    _cancel(cancelled)
    manifest = json_value(raw, maximum=16 * 1024**2)
    if (marker != PORTABLE_CONTRACT or type(manifest) is not dict
            or type(manifest.get('schema')) is not int or manifest['schema'] != 1
            or manifest.get('bundled_python') is not True
            or manifest.get('bundled_launcher') is not True
            or type(manifest.get('files')) is not dict
            or manifest['files'].get(PORTABLE_MARKER) != sha256(marker).hexdigest()
            or manifest.get('source_commit') != package.release.source_commit):
        raise TrustError('The signed candidate does not preserve the portable child-ownership contract and source identity.')
    return sha256(raw).hexdigest()


class _PinnedAdapter(WindowsBundleAdapter):
    def __init__(self, verifier, program, before, after, release, *, timeout):
        super().__init__(verifier, timeout=timeout)
        self.program, self.before, self.after, self.release = program, before, after, release

    def _manifest(self, root, allowed):
        raw = read_bytes(root / 'bundle-manifest.json', 16 * 1024**2)
        if sha256(raw).hexdigest() not in allowed:
            raise TrustError('The application manifest differs from its authenticated activation identity.')
        portable_contract(root)
        return raw

    def verify(self, root):
        root = Path(root).absolute()
        # The retained rollback installation is always A. A journal/path change
        # must not present another internally consistent B as the previous app.
        previous = (root != self.program and root.parent == self.program.parent
                    and root.name.startswith('.carr-previous-'))
        allowed = {self.before} if previous else (
            {self.before, self.after} if root == self.program else {self.after})
        raw = self._manifest(root, allowed)
        self.verify_bundle(root, manifest_bytes=raw)
        if previous or root == self.program:
            installed_ownership(root, raw)

    def verify_rollback_current(self, root):
        root = Path(root).absolute()
        if root != self.program:
            raise TrustError('Original-installation verification belongs to its configured program path.')
        raw = self._manifest(root, {self.before})
        self.verify_bundle(root, manifest_bytes=raw)
        installed_ownership(root, raw)

    def health(self, root, release):
        raw = self._manifest(root, {self.after})
        installed_ownership(root, raw)
        return super().health(root, release)

    def _verify_release(self, root, release, *, manifest_bytes=None):
        if release != self.release:
            raise TrustError('The recovery release differs from the authenticated package.')
        raw = self._manifest(root, {self.after})
        return super()._verify_release(root, release, manifest_bytes=raw)


class PortableUpdateHandoff:
    """Owner-configured external helper; default desktop/polling never calls it.

    Retain installed_manifest_sha256 independently of the mutable target folder.
    Reacquire the SAME authenticated VerifiedPackage for recover(); journal JSON
    cannot grant a new release, source identity, or application manifest.
    """
    def __init__(self, app, installed, work, policy, anchor, verify_bundle, *,
                 installed_manifest_sha256, timeout=900, clock=time.time):
        self.program = Path(installed).absolute()
        if str(self.program).startswith(('//', '\\\\')) or '..' in self.program.parts:
            raise TrustError('Portable activation requires a plain local program path.')
        leases.plain(self.program.parent, directory=True)
        if managed_layout(self.program):
            raise TrustError('Managed updates require an authenticated setup handoff that preserves Repair and Uninstall ownership.')
        self.before = _digest(installed_manifest_sha256)
        self.work = Path(work).absolute()
        if '..' in self.work.parts or '..' in self.program.parts:
            raise TrustError('Update roots cannot contain parent traversal.')
        self.app, self.policy, self.anchor, self.verify_bundle = app, policy, anchor, verify_bundle
        self.timeout, self.clock = timeout, clock
        for first, second in ((self.program, app.root), (self.work, app.root),
                              (self.work, self.program)):
            if first.is_relative_to(second) or second.is_relative_to(first):
                raise TrustError('Program, update workspace and research data must be separate directories.')
        _external_runtime(self.program, self.work, app.root, verify_bundle)
        directory(self.work, create=True)
        self._completed = None

    def _transaction(self, package, *, recovering, cancelled):
        require_verified_package(package)
        after = candidate_manifest(package, self.policy, self.work, cancelled=cancelled)
        adapter = _PinnedAdapter(self.verify_bundle, self.program, self.before, after,
                                 package.release, timeout=self.timeout)

        @contextmanager
        def idle():
            _external_runtime(self.program, self.work, self.app.root, self.verify_bundle)
            if managed_layout(self.program):
                raise TrustError('Managed updates require an authenticated setup handoff that preserves Repair and Uninstall ownership.')
            control = control_root(self.program, create=True)
            with leases.installation_lease(control, exclusive=True):
                _cancel(cancelled)
                if recovering:
                    record, before, release = transaction._record()
                    if release != package.release:
                        raise TrustError('The recovery journal differs from the authenticated package.')
                    if record['phase'] == 'complete':
                        protected = self.anchor.load()
                        if (protected.sequence != release.build or protected.digest != release.sha256
                                or not adapter.health(self.program, release)):
                            raise TrustError('The completed update no longer matches its protected state and health identity.')
                    elif record['phase'] == 'rolled-back':
                        raw = portable_contract(self.program)
                        if self.anchor.load() != before or sha256(raw).hexdigest() != self.before:
                            raise TrustError('The rolled-back installation differs from its protected original identity.')
                        self.verify_bundle(self.program, manifest_bytes=raw)
                        installed_ownership(self.program, raw)
                else:
                    raw = portable_contract(self.program)
                    if sha256(raw).hexdigest() != self.before:
                        raise TrustError('The installed portable source differs from its configured manifest identity.')
                    installed_ownership(self.program, raw)
                yield

        transaction = UpdateTransaction(self.app, self.program, self.work, self.policy,
                                        self.anchor, adapter, idle_lease=idle, clock=self.clock)
        return transaction, after

    def apply(self, package, *, cancelled=lambda: False):
        self._completed = None
        transaction, after = self._transaction(package, recovering=False, cancelled=cancelled)
        result = transaction.apply(package, cancelled=cancelled)
        # Publication is already committed; no late cancellation can relabel it.
        self._completed = (package.release, after)
        return dict(result, installed_manifest_sha256=after, restart='not-requested')

    def recover(self, package, *, cancelled=lambda: False):
        self._completed = None
        transaction, after = self._transaction(package, recovering=True, cancelled=cancelled)
        result = transaction.recover()
        if result == 'complete':
            self._completed = (package.release, after)
        return result

    def restart(self):
        """Explicit restart after verified commit, through the existing launcher.

        The application acquires its own shared lease and verified lifetime Job.
        No arbitrary command, argument string, library path or engine is launched.
        A returned PID is only launch acknowledgement, not desktop/NVDA acceptance.
        """
        if os.name != 'nt' or self._completed is None:
            raise TrustError('Restart requires a completed native portable activation or recovery.')
        release, after = self._completed
        _external_runtime(self.program, self.work, self.app.root, self.verify_bundle)
        with leases.installation_lease(control_root(self.program)):
            anchor = self.anchor.load()
            if anchor.sequence != release.build or anchor.digest != release.sha256:
                raise TrustError('Protected update state changed before restart.')
            adapter = _PinnedAdapter(self.verify_bundle, self.program, self.before, after,
                                     release, timeout=self.timeout)
            adapter._verify_release(self.program, release)
            installed_ownership(self.program, read_bytes(self.program / 'bundle-manifest.json', 16 * 1024**2))
            executable = self.program / 'ResearchLibrary.exe'
            leases.plain(executable)
            process = subprocess.Popen([str(executable)], cwd=self.program,
                                       env=adapter._environment(), close_fds=True)
        return {'status': 'restart-requested', 'pid': process.pid, 'source_commit': release.source_commit}

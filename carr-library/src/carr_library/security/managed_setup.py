"""Authenticated, nonmutating admission for the managed Inno update profile.

A normal incoming package still requires the existing TUF capability. A retained
package can be authenticated against the live protected installed anchor for
recovery, but that separate capability cannot authorize an incoming update.
These functions only inspect private snapshots: they do not extract executable
code, install, advance protected state, or clear any maintenance owner receipt.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import zipfile

from ._files import TrustError, directory, file_info, json_value
from .anchors import Anchor, AnchorStore
from .installation import _central_budget, _verified_snapshot
from .updates import UpdatePolicy, VerifiedPackage, _cancel, relative_path, require_verified_package


_MANIFEST = 'managed-setup.json'
_SETUP = 'ResonanceSetup.exe'
_HELPERS = ('maintenance.py', 'installation_lease.py', 'bundle_verifier.py', 'windows_process.py')
_BUNDLE_MANIFEST = 'bundle/bundle-manifest.json'
_RESERVED = frozenset(('.venv', 'portable-update.ini', 'managed-install.ini',
                       'owned-files.json', 'installation.lease', 'bootstrap-owner.json'))
_PROOF = object()


@dataclass(frozen=True)
class ManagedSetup:
    product: str
    channel: str
    version: str
    build: int
    source_commit: str
    data_schema: int
    bundle_manifest_sha256: str
    setup_sha256: str
    setup_bytes: int
    helpers: tuple[tuple[str, str], ...]
    files: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class ReviewedManagedPackage:
    """The fixed layout of an incoming, genuinely TUF-authenticated package."""
    package: VerifiedPackage
    setup: ManagedSetup
    policy: UpdatePolicy
    _proof: object = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        if (type(self.package) is not VerifiedPackage or type(self.setup) is not ManagedSetup
                or type(self.policy) is not UpdatePolicy
                or self._proof != (_PROOF, self.package, self.setup, self.policy)):
            raise TrustError('Review the managed package through its TUF capability.')
        require_verified_package(self.package)


@dataclass(frozen=True)
class InstalledManagedPackage:
    """Authentication of protected installed bytes, never new-build permission."""
    path: Path
    setup: ManagedSetup
    anchor: Anchor
    policy: UpdatePolicy
    _store: AnchorStore = field(repr=False, compare=False)
    _proof: object = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        if (not isinstance(self.path, Path) or self.path != self.path.absolute()
                or type(self.setup) is not ManagedSetup or type(self.anchor) is not Anchor
                or type(self.policy) is not UpdatePolicy
                or self._proof != (_PROOF, self.path, self.setup, self.anchor, self.policy, id(self._store))):
            raise TrustError('Authenticate installed bytes against the live protected anchor.')


@dataclass(frozen=True)
class ManagedAdmission:
    """A review result; a later mutation boundary must revalidate it freshly."""
    installed: InstalledManagedPackage
    incoming: ReviewedManagedPackage
    _proof: object = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        if (type(self.installed) is not InstalledManagedPackage
                or type(self.incoming) is not ReviewedManagedPackage
                or self._proof != (_PROOF, self.installed, self.incoming)):
            raise TrustError('Use the explicit managed update admission boundary.')
        self.installed.__post_init__()
        self.incoming.__post_init__()


def _digest(value):
    if type(value) is not str or not re.fullmatch('[0-9a-f]{64}', value):
        raise TrustError('Invalid managed package checksum.')
    return value


def _policy(policy):
    if type(policy) is not UpdatePolicy:
        raise TrustError('A validated update policy is required.')
    policy.__post_init__()


def _metadata(raw, policy):
    value = json_value(raw, maximum=16384)
    keys = {'schema', 'kind', 'product', 'channel', 'version', 'build', 'source_commit',
            'data_schema', 'bundle_manifest_sha256', 'setup_sha256', 'setup_bytes', 'helpers'}
    if (type(value) is not dict or set(value) != keys
            or type(value['schema']) is not int or value['schema'] != 1
            or value['kind'] != 'carr-managed-setup'
            or (value['product'], value['channel']) != (policy.product, policy.channel)
            or type(value['build']) is not int or not 0 < value['build'] <= 2**53 - 1
            or type(value['data_schema']) is not int or value['data_schema'] != policy.data_schema
            or type(value['version']) is not str
            or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9.+_-]{0,63}', value['version'])
            or type(value['source_commit']) is not str
            or not re.fullmatch('[0-9a-f]{40}', value['source_commit'])
            or type(value['setup_bytes']) is not int
            or not 0 < value['setup_bytes'] <= policy.max_unpacked_bytes
            or type(value['helpers']) is not dict or set(value['helpers']) != set(_HELPERS)):
        raise TrustError('Unsupported managed package identity or fixed layout.')
    for checksum in (value['bundle_manifest_sha256'], value['setup_sha256'], *value['helpers'].values()):
        _digest(checksum)
    return value


def _inspect(snapshot, policy, cancelled):
    _central_budget(snapshot, policy.max_files)
    with zipfile.ZipFile(snapshot) as archive:
        members, spellings, parents, total = {}, {}, set(), 0
        for info in archive.infolist():
            _cancel(cancelled)
            parts = relative_path(info.filename)
            if (info.is_dir() or info.flag_bits & 1
                    or stat.S_IFMT(info.external_attr >> 16) not in (0, stat.S_IFREG)
                    or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)):
                raise TrustError('Managed packages require ordinary unlinked file members.')
            if info.filename in members:
                raise TrustError('Duplicate managed package member.')
            for end in range(1, len(parts) + 1):
                spelling = '/'.join(parts[:end])
                previous = spellings.setdefault(spelling.casefold(), spelling)
                if previous != spelling:
                    raise TrustError('Aliased managed package path.')
            parents.update('/'.join(parts[:end]) for end in range(1, len(parts)))
            if (parts[0] == 'bundle' and len(parts) > 1 and parts[1].casefold() in _RESERVED):
                raise TrustError('Managed payload must be pristine and cannot carry installation state.')
            total += info.file_size
            if info.file_size < 0 or total > policy.max_unpacked_bytes:
                raise TrustError('Managed package exceeds its unpacked-byte budget.')
            members[info.filename] = info
        if set(members) & parents:
            raise TrustError('Managed package aliases a file with a directory.')

        def metadata(name, maximum):
            info = members.get(name)
            if info is None or info.file_size > maximum:
                raise TrustError('Managed package metadata is missing or exceeds its budget.')
            with archive.open(info) as stream:
                raw = stream.read(info.file_size + 1)
            _cancel(cancelled)
            if len(raw) != info.file_size:
                raise TrustError('Managed package metadata length changed.')
            return raw

        value = _metadata(metadata(_MANIFEST, 16384), policy)
        bundle_raw = metadata(_BUNDLE_MANIFEST, 16 * 1024**2)
        if hashlib.sha256(bundle_raw).hexdigest() != value['bundle_manifest_sha256']:
            raise TrustError('Managed bundle manifest checksum differs.')
        bundle = json_value(bundle_raw, maximum=16 * 1024**2)
        if (type(bundle) is not dict or bundle.get('schema') != 1
                or type(bundle.get('schema')) is not int
                or bundle.get('bundled_python') is not True or bundle.get('bundled_launcher') is not True
                or bundle.get('source_commit') != value['source_commit']
                or type(bundle.get('files')) is not dict or not bundle['files']
                or len(bundle['files']) > policy.max_files):
            raise TrustError('Managed package needs the complete sealed bundle and exact source identity.')
        expected = {_BUNDLE_MANIFEST: value['bundle_manifest_sha256'],
                    _SETUP: value['setup_sha256'],
                    **{'helpers/' + name: checksum for name, checksum in value['helpers'].items()}}
        for name, checksum in bundle['files'].items():
            parts = relative_path(name)
            if parts[0].casefold() in _RESERVED or name == 'bundle-manifest.json':
                raise TrustError('Managed bundle contains installation state or a recursive manifest.')
            expected['bundle/' + name] = _digest(checksum)
        if set(members) != set(expected) | {_MANIFEST}:
            raise TrustError('Managed package contains missing or undeclared files.')
        if members[_SETUP].file_size != value['setup_bytes']:
            raise TrustError('Managed setup length differs from the fixed package manifest.')
        for name, checksum in expected.items():
            _cancel(cancelled)
            count, digest = 0, hashlib.sha256()
            with archive.open(members[name]) as stream:
                while block := stream.read(65536):
                    _cancel(cancelled)
                    count += len(block)
                    if count > members[name].file_size:
                        raise TrustError('Managed member exceeded its declared length.')
                    digest.update(block)
            if count != members[name].file_size or digest.hexdigest() != checksum:
                raise TrustError('Managed member checksum or length differs: ' + name)
        _cancel(cancelled)
        return ManagedSetup(value['product'], value['channel'], value['version'], value['build'],
                            value['source_commit'], value['data_schema'], value['bundle_manifest_sha256'],
                            value['setup_sha256'], value['setup_bytes'],
                            tuple(sorted(value['helpers'].items())), tuple(sorted(expected.items())))


def review_managed_package(package, policy, *, cancelled=lambda: False):
    """Inspect signed incoming bytes without extracting or running their code."""
    _policy(policy)
    require_verified_package(package)
    if not 0 < package.release.length <= policy.max_package_bytes:
        raise TrustError('Managed target exceeds the download budget.')
    with _verified_snapshot(package, package.path.parent, cancelled) as snapshot:
        setup = _inspect(snapshot, policy, cancelled)
    release = package.release
    if ((setup.product, setup.channel, setup.version, setup.build, setup.source_commit, setup.data_schema)
            != (release.product, release.channel, release.version, release.build,
                release.source_commit, release.data_schema)):
        raise TrustError('Managed package identity differs from the authenticated TUF release.')
    return ReviewedManagedPackage(package, setup, policy, (_PROOF, package, setup, policy))


@contextmanager
def _installed_snapshot(path, anchor, policy, cancelled):
    """Authenticate retained bytes to protected state, without minting TUF approval."""
    _cancel(cancelled)
    info = file_info(path)
    if not 0 < info.st_size <= policy.max_package_bytes:
        raise TrustError('Retained managed package exceeds its size budget.')
    parent = directory(path.parent)
    if shutil.disk_usage(parent).free < info.st_size + 16 * 1024**2:
        raise TrustError('Not enough free space to verify the retained managed package.')
    with tempfile.TemporaryFile(dir=parent) as snapshot:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0))
        with os.fdopen(fd, 'rb') as stream:
            opened = os.fstat(stream.fileno())
            if ((opened.st_dev, opened.st_ino, opened.st_size) != (info.st_dev, info.st_ino, info.st_size)
                    or not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                    or getattr(opened, 'st_file_attributes', 0) & 0x400):
                raise TrustError('Retained managed package changed while opening.')
            digest, count = hashlib.sha256(), 0
            while block := stream.read(min(65536, info.st_size + 1 - count)):
                _cancel(cancelled)
                count += len(block)
                if count > info.st_size:
                    raise TrustError('Retained managed package grew while reading.')
                digest.update(block)
                snapshot.write(block)
        if count != info.st_size or digest.hexdigest() != anchor.digest:
            raise TrustError('Retained managed package does not match the protected anchor.')
        _cancel(cancelled)
        snapshot.flush()
        snapshot.seek(0)
        yield snapshot


def authenticate_installed_package(path, policy, anchors, *, cancelled=lambda: False):
    """Allow recovery of the protected build, including B after a committed CAS.

    Owner provisioning of the protected anchor is a separate trust operation.
    Neither a local receipt nor this function creates or resets that state.
    """
    _policy(policy)
    _cancel(cancelled)
    anchor = anchors.load()
    if type(anchor) is not Anchor or anchor.sequence <= 0:
        raise TrustError('A separately provisioned installed-package anchor is required.')
    path = Path(path).absolute()
    with _installed_snapshot(path, anchor, policy, cancelled) as snapshot:
        setup = _inspect(snapshot, policy, cancelled)
    if setup.build != anchor.sequence:
        raise TrustError('Retained managed build differs from the protected anchor.')
    _cancel(cancelled)
    if anchors.load() != anchor:
        raise TrustError('Protected installed identity changed during authentication.')
    return InstalledManagedPackage(path, setup, anchor, policy, anchors,
                                   (_PROOF, path, setup, anchor, policy, id(anchors)))


def admit_managed_update(installed, incoming, policy, anchors, *, reviewed_sha256,
                         cancelled=lambda: False):
    """Fresh A/B admission only; every actual mutation must revalidate again."""
    _policy(policy)
    if type(installed) is not InstalledManagedPackage or type(incoming) is not ReviewedManagedPackage:
        raise TrustError('Independent installed and TUF incoming capabilities are required.')
    installed.__post_init__()
    incoming.__post_init__()
    if (installed._store is not anchors or installed.policy != policy or incoming.policy != policy
            or reviewed_sha256 != incoming.package.release.sha256):
        raise TrustError('Managed update does not match this owner, policy, and explicit review.')
    _cancel(cancelled)
    if anchors.load() != installed.anchor:
        raise TrustError('Protected installed identity changed after review.')
    current = authenticate_installed_package(installed.path, policy, anchors, cancelled=cancelled)
    proposed = review_managed_package(incoming.package, policy, cancelled=cancelled)
    if current != installed or proposed != incoming:
        raise TrustError('Managed package identity changed after review.')
    if proposed.setup.build <= max(current.anchor.sequence, policy.current_build):
        raise TrustError('A managed update must advance the protected installed build.')
    _cancel(cancelled)
    if anchors.load() != current.anchor:
        raise TrustError('Protected installed identity changed before admission.')
    return ManagedAdmission(current, proposed, (_PROOF, current, proposed))


def revalidate_managed_admission(admission, *, cancelled=lambda: False):
    if type(admission) is not ManagedAdmission:
        raise TrustError('An explicit managed update admission is required.')
    admission.__post_init__()
    return admit_managed_update(admission.installed, admission.incoming, admission.installed.policy,
                                admission.installed._store,
                                reviewed_sha256=admission.incoming.package.release.sha256,
                                cancelled=cancelled)

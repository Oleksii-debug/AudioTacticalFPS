"""Offline TUF repository client: publisher trust is an independent input.

python-tuf performs all role/signature/version/expiry/length/hash verification.
This adapter has no network fetcher or production endpoint. A chosen local
repository can only supply metadata and signed targets beneath its own folders.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
from urllib.parse import unquote, urlsplit

from ..storage.revisions import SCHEMAS
from ._files import TrustError, component, directory, file_info, locked, read_bytes, sync_directory


class UpdateCancelled(TrustError):
    pass


def _cancel(cancelled):
    if cancelled():
        raise UpdateCancelled('Update preparation cancelled. The installed program and library are unchanged.')


def relative_path(value):
    if not isinstance(value, str) or not value or len(value) > 1024 or value.startswith('/'):
        raise TrustError('Invalid signed target path.')
    parts = value.split('/')
    for part in parts:
        component(part)
    return tuple(parts)


@dataclass(frozen=True)
class UpdatePolicy:
    product: str
    channel: str
    bootstrap: bytes
    bootstrap_sha256: str
    current_build: int
    data_schema: int = max(SCHEMAS)
    max_package_bytes: int = 2 * 1024**3
    max_unpacked_bytes: int = 4 * 1024**3
    max_files: int = 50000

    def __post_init__(self):
        for value in (self.product, self.channel):
            if not isinstance(value, str) or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9._-]{0,127}', value):
                raise TrustError('Invalid update product or channel.')
        if (not isinstance(self.bootstrap, bytes) or not 1 <= len(self.bootstrap) <= 512000
                or hashlib.sha256(self.bootstrap).hexdigest() != self.bootstrap_sha256):
            raise TrustError('Bootstrap trust does not match the independently pinned root digest.')
        for name in ('current_build', 'data_schema', 'max_package_bytes', 'max_unpacked_bytes', 'max_files'):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise TrustError('Invalid update budget or build identity.')
        if self.max_files > 100000 or self.max_package_bytes > 16 * 1024**3 or self.max_unpacked_bytes > 32 * 1024**3:
            raise TrustError('Update resource budget is too large.')


@dataclass(frozen=True)
class Release:
    product: str
    channel: str
    version: str
    build: int
    source_commit: str
    data_schema: int
    target: str
    length: int
    sha256: str
    notes: str


_TUF_VERIFIED = object()


@dataclass(frozen=True)
class VerifiedPackage:
    release: Release
    path: Path
    _proof: object = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        if (type(self.release) is not Release or not isinstance(self.path, Path)
                or self.path != self.path.absolute()
                or type(self._proof) is not tuple or len(self._proof) != 3
                or self._proof[0] is not _TUF_VERIFIED
                or self._proof[1:] != (self.release, self.path)):
            raise TrustError('Obtain the verified package through the TUF client; a pathname and checksum are not publisher approval.')


def require_verified_package(package):
    if type(package) is not VerifiedPackage:
        raise TrustError('A TUF-verified package receipt is required.')
    package.__post_init__()
    return package


def _verified_package(release, path):
    path = Path(path).absolute()
    # Bind the capability to the precise metadata and path. dataclasses.replace
    # must not carry a generic approval sentinel onto different release bytes.
    return VerifiedPackage(release, path, (_TUF_VERIFIED, release, path))


def _release(info, policy):
    value = info.custom
    keys = {'schema', 'kind', 'product', 'channel', 'version', 'build', 'source_commit', 'data_schema', 'notes'}
    if type(value) is not dict or set(value) != keys or type(value['schema']) is not int or value['schema'] != 1:
        raise TrustError('Unsupported signed release description.')
    if value['kind'] != 'carr-application' or (value['product'], value['channel']) != (policy.product, policy.channel):
        raise TrustError('Update belongs to a different product or channel.')
    if type(value['build']) is not int or value['build'] <= policy.current_build:
        raise TrustError('An update must advance the installed build; rollback packages are refused.')
    if type(value['data_schema']) is not int or value['data_schema'] != policy.data_schema:
        raise TrustError('This update requires an unsupported data migration. Owner review is required.')
    if not isinstance(value['version'], str) or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9.+_-]{0,63}', value['version']):
        raise TrustError('Invalid release version.')
    if not isinstance(value['source_commit'], str) or not re.fullmatch('[0-9a-f]{40}', value['source_commit']):
        raise TrustError('Signed release provenance must identify an exact source commit.')
    if (not isinstance(value['notes'], str) or len(value['notes']) > 4096
            or any(ord(c) < 32 and c not in '\n\t' for c in value['notes'])):
        raise TrustError('Invalid signed release notes.')
    relative_path(info.path)
    digest = info.hashes.get('sha256')
    if not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest):
        raise TrustError('A SHA256 target identity is required.')
    if type(info.length) is not int or not 0 < info.length <= policy.max_package_bytes:
        raise TrustError('Update target exceeds the download budget.')
    return Release(value['product'], value['channel'], value['version'], value['build'],
                   value['source_commit'], value['data_schema'], info.path, info.length, digest, value['notes'])


def _metadata_cache(root):
    """Permit only TUF top-level cache and its exact internal root alias.

    TUF 7 uses a symlink on POSIX or a same-inode hardlink on unprivileged
    Windows for root.json. It is never trusted as bootstrap by this client.
    """
    root = directory(root, create=True)
    history = root / 'root_history'
    directory(history, create=True)
    alias = root / 'root.json'
    alias_target = None
    if alias.is_symlink():
        text = os.readlink(alias)
        if not re.fullmatch(r'root_history/[1-9][0-9]*\.root\.json', text.replace('\\', '/')):
            raise TrustError('Redirected TUF root alias.')
        alias_target = history / Path(text).name
        file_info(alias_target)
    elif os.path.lexists(alias):
        info = alias.lstat()
        if not stat.S_ISREG(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise TrustError('Unsafe TUF root alias.')
    count = 0
    for entry in history.iterdir():
        count += 1
        if count > 1024 or not re.fullmatch(r'[1-9][0-9]*\.root\.json', entry.name):
            raise TrustError('Unexpected or excessive TUF root history.')
        info = entry.lstat()
        if (not stat.S_ISREG(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400
                or info.st_size > 512000):
            raise TrustError('Unsafe TUF root history.')
        if info.st_nlink != 1:
            if not (info.st_nlink == 2 and alias.exists() and not alias.is_symlink()
                    and os.path.samefile(entry, alias)):
                raise TrustError('Unexpected linked TUF root history.')
            alias_target = entry
    for entry in root.iterdir():
        if entry.name == 'root_history' or entry.name == 'root.json':
            continue
        if entry.name not in ('timestamp.json', 'snapshot.json', 'targets.json'):
            raise TrustError('Unexpected TUF metadata cache entry.')
        if file_info(entry).st_size > 5_000_000:
            raise TrustError('Oversized TUF metadata cache.')
    if os.path.lexists(alias) and not alias.is_symlink() and alias.lstat().st_nlink != 1 and alias_target is None:
        raise TrustError('Unbound TUF root alias.')


def _local_fetcher(repository, cancelled, *, max_target_bytes=2 * 1024**3):
    # Lazy optional import preserves the existing private installation.
    from tuf.api.exceptions import DownloadHTTPError
    from tuf.ngclient.fetcher import FetcherInterface

    class LocalOnlyFetcher(FetcherInterface):
        def _fetch(self, url):
            parsed = urlsplit(url)
            if parsed.scheme != 'file' or parsed.netloc or parsed.query or parsed.fragment:
                raise TrustError('Only the explicitly selected offline repository is allowed.')
            path = unquote(parsed.path, errors='strict')
            if not path.startswith(('/metadata/', '/targets/')):
                raise TrustError('Unknown offline repository path.')
            parts = relative_path(path[1:])
            maximum = max_target_bytes
            if parts[0] == 'metadata':
                match = re.fullmatch(r'(?:[1-9][0-9]*\.)?(root|timestamp|snapshot|targets)\.json', parts[-1])
                if len(parts) != 2 or match is None:
                    raise TrustError('Unsupported offline metadata role.')
                maximum = {'root': 512000, 'timestamp': 16384, 'snapshot': 2_000_000, 'targets': 5_000_000}[match[1]]
            source = repository.joinpath(*parts)
            try:
                before = file_info(source)
            except FileNotFoundError:
                raise DownloadHTTPError('Offline metadata or target is absent.', 404) from None
            if before.st_size > maximum:
                raise TrustError('Offline repository object exceeds its resource budget.')
            fd = os.open(source, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
            with os.fdopen(fd, 'rb') as stream:
                if (os.fstat(stream.fileno()).st_ino, os.fstat(stream.fileno()).st_dev) != (before.st_ino, before.st_dev):
                    raise TrustError('Offline repository file changed while opening.')
                used = 0
                while block := stream.read(65536):
                    _cancel(cancelled)
                    used += len(block)
                    if used > maximum:
                        raise TrustError('Offline repository object exceeded its resource budget.')
                    yield block
                after = os.fstat(stream.fileno())
                if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
                    raise TrustError('Offline repository file changed while reading.')
    return LocalOnlyFetcher()


class LocalRepositoryClient:
    def __init__(self, cache, policy: UpdatePolicy):
        self.cache, self.policy = directory(cache, create=True), policy
        self.metadata = directory(self.cache / 'metadata', create=True)
        self.downloads = directory(self.cache / 'downloads', create=True)

    def _updater(self, repository, cancelled):
        from tuf.ngclient import Updater
        from tuf.ngclient.config import UpdaterConfig
        repository = directory(repository)
        directory(repository / 'metadata')
        directory(repository / 'targets')
        if self.cache.is_relative_to(repository) or repository.is_relative_to(self.cache):
            raise TrustError('Update cache and offline repository must be separate.')
        _metadata_cache(self.metadata)
        return Updater(str(self.metadata), 'file:///metadata/', str(self.downloads), 'file:///targets/',
                       fetcher=_local_fetcher(repository, cancelled, max_target_bytes=self.policy.max_package_bytes),
                       config=UpdaterConfig(max_root_rotations=32, max_delegations=0,
                                            snapshot_max_length=2_000_000, targets_max_length=5_000_000),
                       bootstrap=self.policy.bootstrap)

    def check(self, repository, target, *, cancelled=lambda: False):
        relative_path(target)
        with locked(self.cache):
            updater = self._updater(repository, cancelled)
            updater.refresh()
            info = updater.get_targetinfo(target)
            if info is None:
                raise TrustError('No trusted release exists at the requested target.')
            return _release(info, self.policy)

    def download(self, repository, target, *, reviewed_sha256, cancelled=lambda: False):
        relative_path(target)
        with locked(self.cache):
            updater = self._updater(repository, cancelled)
            updater.refresh()
            info = updater.get_targetinfo(target)
            if info is None:
                raise TrustError('No trusted release exists at the requested target.')
            release = _release(info, self.policy)
            if release.sha256 != reviewed_sha256:
                raise TrustError('The release changed after review. Review the new signed target first.')
            _cancel(cancelled)
            destination = self.downloads / (release.sha256 + '.zip')
            if os.path.lexists(destination):
                file_info(destination)
                if updater.find_cached_target(info, str(destination)):
                    return _verified_package(release, destination)
                raise TrustError('Cached package is corrupt. Remove it explicitly before retrying.')
            if shutil.disk_usage(self.cache).free < release.length + 16 * 1024**2:
                raise TrustError('Not enough free space to prepare the update.')
            with tempfile.TemporaryDirectory(prefix='.download-', dir=self.downloads) as tmp:
                candidate = Path(tmp) / 'package.zip'
                updater.download_target(info, str(candidate))
                _cancel(cancelled)
                with candidate.open('r+b') as stream:
                    info.verify_length_and_hashes(stream)
                    os.fsync(stream.fileno())
                os.link(candidate, destination)
                sync_directory(self.downloads)
            return _verified_package(release, destination)

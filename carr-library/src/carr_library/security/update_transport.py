"""Explicit HTTPS TUF transport with bounded owned requests and resumable bytes.

Publisher identity is still UpdatePolicy's independently pinned root. The site
address is owner configuration, never release metadata or a fallback. Redirects,
environment proxies and credentials are not used. Cancellation retains only an
unverified private prefix; every resumed target is fully reauthenticated by TUF.
"""
from __future__ import annotations

from contextlib import contextmanager
from hashlib import sha256
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import time

from ._files import (TrustError, atomic_write, directory, file_info, json_bytes,
                     json_value, locked, read_bytes, sync_directory)
from .updates import (LocalRepositoryClient, UpdateCancelled, _cancel, _metadata_cache,
                      _release, _verified_package, relative_path)
from .update_http_worker import _directories, _endpoint, _file_identity


class HttpsRepositoryClient(LocalRepositoryClient):
    """One explicitly configured site; no contact occurs during construction."""
    def __init__(self, cache, policy, *, endpoint, timeout=1800, ca_pem=None):
        _endpoint(endpoint)
        if type(timeout) not in (int, float) or not 1 <= timeout <= 3600:
            raise TrustError('Invalid signed-update transfer deadline.')
        if ca_pem is not None:
            if type(ca_pem) is not str or not 1 <= len(ca_pem) <= 16384:
                raise TrustError('Invalid configured TLS certificate authority.')
            import ssl
            ssl.create_default_context(cadata=ca_pem)
        super().__init__(cache, policy)
        self.endpoint, self.timeout, self.ca_pem = endpoint, float(timeout), ca_pem
        self.partials = directory(self.downloads / 'partial', create=True)
        self._active_fetcher = None

    def _updater(self, repository, cancelled):
        from tuf.ngclient import Updater
        from tuf.ngclient.config import UpdaterConfig
        if repository != self.endpoint:
            raise TrustError('Online checks require the exact configured repository.')
        _metadata_cache(self.metadata)
        self._active_fetcher = _fetcher(self, cancelled)
        return Updater(str(self.metadata), self.endpoint + 'metadata/', str(self.downloads),
            self.endpoint + 'targets/', fetcher=self._active_fetcher,
            config=UpdaterConfig(max_root_rotations=32, max_delegations=0,
                                 snapshot_max_length=2_000_000, targets_max_length=5_000_000),
            bootstrap=self.policy.bootstrap)

    def download(self, repository, target, *, reviewed_sha256, cancelled=lambda: False,
                 progress=None):
        relative_path(target)
        with locked(self.cache):
            updater = self._updater(repository, cancelled)
            updater.refresh()
            info = updater.get_targetinfo(target)
            if info is None:
                raise TrustError('No trusted release exists at the requested target.')
            release = _release(info, self.policy)
            if release.sha256 != reviewed_sha256:
                raise TrustError('The release changed after review. Review the signed target again.')
            _cancel(cancelled)
            destination = self.downloads / (release.sha256 + '.zip')
            if os.path.lexists(destination):
                file_info(destination)
                if updater.find_cached_target(info, str(destination)):
                    return _verified_package(release, destination)
                raise TrustError('Cached package is corrupt. Remove it explicitly before retrying.')
            # TUF's verified staging copy coexists with the resumable prefix.
            if shutil.disk_usage(self.cache).free < 2 * release.length + 16 * 1024**2:
                raise TrustError('Not enough free space for a resumable and verified update.')
            fetcher = self._active_fetcher
            fetcher.admit(info, progress)
            with tempfile.TemporaryDirectory(prefix='.verified-', dir=self.downloads) as temporary:
                candidate = Path(temporary) / 'package.zip'
                updater.download_target(info, str(candidate))
                _cancel(cancelled)
                with candidate.open('r+b') as stream:
                    info.verify_length_and_hashes(stream)
                    os.fsync(stream.fileno())
                _cancel(cancelled)
                os.link(candidate, destination)
                sync_directory(self.downloads)
            # The verified destination is committed. Prefix cleanup cannot revoke it.
            package = _verified_package(release, destination)
            try:
                fetcher.forget_prefix()
            except Exception:
                self.notice = 'The signed package is verified and saved. Its unverified download prefix needs cleanup.'
            return package


def _fetcher(client, cancelled):
    from tuf.api.exceptions import DownloadHTTPError
    from tuf.ngclient.fetcher import FetcherInterface

    class FixedSiteFetcher(FetcherInterface):
        def __init__(self):
            self.deadline = time.monotonic() + client.timeout
            self.target = None
            self.progress = None
            self.prefix = None
            self.prefix_identity = None

        def check(self):
            _cancel(cancelled)
            if time.monotonic() >= self.deadline:
                raise TimeoutError('The signed update reached its transfer deadline.')

        def admit(self, info, progress):
            self.check()
            _release(info, client.policy)
            if '%' in info.path or '?' in info.path or '#' in info.path:
                raise TrustError('The signed target uses an unsupported URL spelling.')
            self.target, self.progress = info, progress

        def _invoke(self, url, parent, *, maximum, total=0):
            from ..audio.models import AudioBudget, AudioCancelled, AudioCleanupPending, AudioError
            from ..audio.process import run_process
            from .installation import _wait_for_cleanup
            self.check()
            rows = _directories(parent)
            response = parent / 'response.part'
            if not os.path.lexists(response):
                descriptor = os.open(response, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(descriptor)
            before = file_info(response)
            identity = _file_identity(before)
            offset = before.st_size
            if offset > maximum or not total and offset:
                raise TrustError('The resumable prefix exceeds its reviewed bound.')
            if total and offset == total:
                return response, identity
            request = {'schema': 1, 'endpoint': client.endpoint, 'url': url,
                'maximum': maximum, 'total': total, 'offset': offset,
                'timeout': max(.05, self.deadline - time.monotonic()),
                'directories': rows, 'file_identity': identity, 'ca_pem': client.ca_pem}
            raw = json_bytes(request)
            if len(raw) > 32768:
                raise TrustError('The private transfer request exceeds its bound.')
            request_path = parent / 'request.json'
            atomic_write(request_path, raw)
            worker = Path(__file__).with_name('update_http_worker.py')
            worker_before = read_bytes(worker, 65536)
            last = -1

            def control():
                nonlocal last
                self.check()
                if _directories(parent) != rows:
                    raise TrustError('The update staging directory changed.')
                current = file_info(response)
                if _file_identity(current) != identity or current.st_size > maximum:
                    raise TrustError('The private update response changed.')
                if self.progress is not None and total and current.st_size != last:
                    last = current.st_size
                    self.progress(last, total, 'Downloading signed package; bytes are not yet verified')
                return False

            environment = {k: v for k, v in os.environ.items()
                           if not k.upper().startswith(('PYTHON', 'PIP'))}
            environment.update(PYTHONNOUSERSITE='1', PYTHONDONTWRITEBYTECODE='1')
            try:
                result = run_process([str(Path(sys.executable).absolute()), '-I', '-B', str(worker),
                    '--request', str(request_path), '--sha256', sha256(raw).hexdigest()],
                    cwd=str(parent), env=environment, cancelled=control,
                    owned_windows_job=os.name == 'nt', allow_failure=True,
                    watch=(parent,), budget=AudioBudget(
                        timeout=max(.05, self.deadline - time.monotonic()),
                        max_output_bytes=maximum + 65536, max_log_bytes=16384,
                        reserve_disk_bytes=16 * 1024**2, max_staging_files=8, threads=1))
            except AudioCleanupPending as error:
                _wait_for_cleanup(error)
                raise
            except AudioCancelled:
                raise UpdateCancelled('Download paused. Its private prefix can be resumed; no application was changed.') from None
            except AudioError as error:
                raise TrustError('The bounded update transfer failed. Its unverified prefix can be retried.') from error
            self.check()
            if read_bytes(worker, 65536) != worker_before or _directories(parent) != rows:
                raise TrustError('The update transfer source or destination changed.')
            after = file_info(response)
            if _file_identity(after) != identity:
                raise TrustError('The private response file changed.')
            reply = json_value(result.stdout, maximum=16384)
            if (result.returncode or result.stderr or type(reply) is not dict
                    or set(reply) != {'schema', 'status', 'bytes', 'start'}
                    or type(reply['schema']) is not int or reply['schema'] != 1
                    or type(reply['status']) is not int
                    or type(reply['bytes']) is not int or type(reply['start']) is not int
                    or not 0 <= reply['start'] <= reply['bytes'] <= maximum
                    or reply['bytes'] != after.st_size):
                raise TrustError('The isolated HTTPS request failed or returned an invalid receipt.')
            if reply['status'] == 404:
                raise DownloadHTTPError('Configured update object is absent.', 404)
            if reply['status'] not in (200, 206) or total and after.st_size != total:
                raise TrustError('The HTTPS target did not reach its reviewed byte length.')
            return response, identity

        @contextmanager
        def source(self, url):
            self.check()
            prefix = client.endpoint + 'metadata/'
            if url.startswith(prefix):
                name = url[len(prefix):]
                match = re.fullmatch(r'(?:[1-9][0-9]*\.)?(root|timestamp|snapshot|targets)\.json', name)
                if match is None:
                    raise TrustError('Unsupported signed metadata role.')
                maximum = {'root': 512000, 'timestamp': 16384,
                           'snapshot': 2_000_000, 'targets': 5_000_000}[match[1]]
                with tempfile.TemporaryDirectory(prefix='.https-', dir=client.cache) as temporary:
                    yield self._invoke(url, Path(temporary), maximum=maximum)
                return
            info = self.target
            if info is None:
                raise TrustError('A target must first be authenticated by TUF.')
            directory_part, separator, basename = info.path.rpartition('/')
            allowed = {client.endpoint + 'targets/' + info.path}
            for digest in info.hashes.values():
                if type(digest) is str and re.fullmatch('[0-9a-f]{32,128}', digest):
                    allowed.add(client.endpoint + 'targets/' + directory_part + separator + digest + '.' + basename)
            if url not in allowed:
                raise TrustError('The transfer URL is not the authenticated target.')
            parent = directory(client.partials / info.hashes['sha256'], create=True)
            path, identity = self._invoke(url, parent, maximum=info.length, total=info.length)
            self.prefix, self.prefix_identity = path, identity
            yield path, identity

        def _fetch(self, url):
            with self.source(url) as (path, identity):
                before = file_info(path)
                if _file_identity(before) != identity:
                    raise TrustError('The isolated response changed before verification.')
                descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
                with os.fdopen(descriptor, 'rb') as stream:
                    if _file_identity(os.fstat(descriptor)) != identity:
                        raise TrustError('The isolated response changed while opening.')
                    size = 0
                    while block := stream.read(65536):
                        self.check()
                        size += len(block)
                        if size > before.st_size:
                            raise TrustError('The isolated response grew during verification.')
                        yield block
                    after = os.fstat(descriptor)
                    if (size, after.st_size, after.st_mtime_ns) != (
                            before.st_size, before.st_size, before.st_mtime_ns):
                        raise TrustError('The isolated response changed during verification.')
                if _file_identity(file_info(path)) != identity:
                    raise TrustError('The isolated response path changed during verification.')

        def forget_prefix(self):
            if self.prefix is not None and _file_identity(file_info(self.prefix)) == self.prefix_identity:
                self.prefix.unlink()
                request = self.prefix.with_name('request.json')
                if os.path.lexists(request):
                    file_info(request)
                    request.unlink()
                self.prefix.parent.rmdir()
                sync_directory(client.partials)

    return FixedSiteFetcher()

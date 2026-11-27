"""Explicit reviewed HTTPS transport for the existing python-tuf verifier.

No endpoint, root key or network operation is enabled by default. The selected
origin and optional TLS CA are caller-owned configuration; TUF remains the sole
release authority. Every request runs under the existing owned process deadline.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import math
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import time
from urllib.parse import quote, unquote, urlsplit

from ._files import TrustError, atomic_write, directory, file_info, json_bytes, json_value, read_bytes
from .updates import LocalRepositoryClient, _cancel, _metadata_cache, relative_path


@dataclass(frozen=True)
class HTTPSRepository:
    url: str
    timeout_seconds: float = 120.0
    ca_pem: bytes | None = field(default=None, repr=False)
    ca_sha256: str | None = None

    def __post_init__(self):
        if (not isinstance(self.url, str) or not 1 <= len(self.url) <= 2048
                or any(ord(c) < 33 or ord(c) > 126 or c == '\\' for c in self.url)):
            raise TrustError('Choose one explicit HTTPS repository address.')
        try:
            parsed = urlsplit(self.url)
            host = parsed.hostname
            if (parsed.scheme != 'https' or not host or parsed.username is not None
                    or parsed.password is not None or parsed.query or parsed.fragment
                    or not 1 <= (443 if parsed.port is None else parsed.port) <= 65535 or host.endswith('.')
                    or '%' in host or not self.url.endswith('/')):
                raise ValueError()
            decoded = unquote(parsed.path, errors='strict')
            if decoded != parsed.path or (decoded != '/' and relative_path(decoded.strip('/')) is None):
                raise ValueError()
            if '//' in parsed.path or not parsed.path.startswith('/'):
                raise ValueError()
            # Reject ambiguous host spellings while permitting explicit IP HTTPS
            # sources whose certificate contains that IP (including local tests).
            if any(c in host for c in '/\\@') or len(host) > 253:
                raise ValueError()
        except (ValueError, UnicodeError):
            raise TrustError('Use a plain HTTPS origin and repository directory, without credentials, query or redirects.') from None
        if (type(self.timeout_seconds) not in (int, float)
                or not math.isfinite(self.timeout_seconds) or not .05 <= self.timeout_seconds <= 1800):
            raise TrustError('Invalid total update network deadline.')
        if (self.ca_pem is None) != (self.ca_sha256 is None):
            raise TrustError('An optional TLS CA and its independent SHA256 must be supplied together.')
        if self.ca_pem is not None:
            if (not isinstance(self.ca_pem, bytes) or not 0 < len(self.ca_pem) <= 128 * 1024
                    or sha256(self.ca_pem).hexdigest() != self.ca_sha256):
                raise TrustError('The reviewed TLS CA differs from its pinned identity.')
            try:
                self.ca_pem.decode('ascii')
            except UnicodeError:
                raise TrustError('TLS CA must be ASCII PEM.') from None

    def object_limit(self, url, target_limit):
        if not isinstance(url, str) or not url.startswith(self.url):
            raise TrustError('TUF requested an unreviewed origin or directory.')
        parsed, origin = urlsplit(url), urlsplit(self.url)
        if (parsed.scheme, parsed.netloc) != (origin.scheme, origin.netloc) or parsed.query or parsed.fragment:
            raise TrustError('TUF requested an unreviewed origin or directory.')
        suffix = url[len(self.url):]
        decoded = unquote(suffix, errors='strict')
        if quote(decoded, safe='/._-~') != suffix or '%' in decoded:
            raise TrustError('Ambiguous repository object path.')
        parts = relative_path(decoded)
        if parts[0] == 'metadata':
            match = re.fullmatch(r'(?:[1-9][0-9]*\.)?(root|timestamp|snapshot|targets)\.json', parts[-1])
            if len(parts) != 2 or match is None:
                raise TrustError('Only the configured top-level TUF roles may be requested.')
            return {'root': 512000, 'timestamp': 16384, 'snapshot': 2_000_000, 'targets': 5_000_000}[match[1]]
        if parts[0] != 'targets' or len(parts) < 2:
            raise TrustError('Unknown reviewed repository object.')
        return target_limit


def _worker_command(root):
    if getattr(sys, 'frozen', False):
        raise TrustError('This update transport requires the reviewed installed Python runtime.')
    # The child imports stdlib only. A Windows venv redirector must not obscure
    # the real child PID from the existing native Job owner.
    interpreter = Path(getattr(sys, '_base_executable', sys.executable)).resolve(strict=True)
    file_info(interpreter)
    worker = Path(__file__).with_name('_https_worker.py').resolve(strict=True)
    file_info(worker)
    return [str(interpreter), '-I', '-S', '-B', '-X', 'utf8', str(worker), str(root)]


def _stamp(value):
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns, value.st_nlink


def _fetcher(source, cache, cancelled, target_limit):
    from tuf.api.exceptions import DownloadHTTPError
    from tuf.ngclient.fetcher import FetcherInterface
    from ..audio.cleanup import retained_temporary_directory
    from ..audio.models import AudioBudget, AudioCancelled
    from ..audio.process import run_process
    from ..durable_state import _open_json_file

    deadline = time.monotonic() + source.timeout_seconds
    requests = 0

    def check():
        _cancel(cancelled)
        if time.monotonic() >= deadline:
            raise TrustError('The update network operation exceeded its total deadline.')

    class ReviewedHTTPSFetcher(FetcherInterface):
        def _fetch(self, url):
            nonlocal requests
            check()
            requests += 1
            if requests > 40:
                raise TrustError('The update operation exceeded its request budget.')
            maximum = source.object_limit(url, target_limit)
            if shutil.disk_usage(cache).free < maximum + 16 * 1024**2:
                raise TrustError('Not enough free space for the bounded update transfer.')
            with retained_temporary_directory(prefix='.https-', dir=cache) as name:
                root = Path(name)
                request = dict(url=url, maximum=maximum,
                    timeout=max(.001, deadline - time.monotonic()),
                    ca_pem=None if source.ca_pem is None else source.ca_pem.decode('ascii'))
                atomic_write(root / 'request.json', json_bytes(request))
                try:
                    result = run_process(_worker_command(root), cwd=root,
                        env={k: v for k, v in os.environ.items() if not k.upper().startswith(('PYTHON', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY'))},
                        owned_tree=True, allow_failure=True,
                        cancelled=lambda: cancelled() or time.monotonic() >= deadline,
                        budget=AudioBudget(timeout=max(.05, deadline - time.monotonic()), max_log_bytes=8192))
                except AudioCancelled:
                    check()
                    raise TrustError('The update transfer was interrupted.') from None
                check()
                reply = json_value(result.stdout, maximum=8192)
                if (result.returncode != 0 or result.stderr or type(reply) is not dict):
                    raise TrustError('The verified HTTPS transfer did not complete.')
                if set(reply) == {'status', 'code'} and reply['status'] == 'http':
                    code = reply['code']
                    if type(code) is not int or not 100 <= code <= 599:
                        raise TrustError('Invalid HTTPS status.')
                    # No redirect is followed and no alternate source is tried.
                    raise DownloadHTTPError('The reviewed HTTPS source refused this object.', code)
                if (set(reply) != {'status', 'bytes', 'sha256'} or reply['status'] != 'complete'
                        or type(reply['bytes']) is not int or not 0 <= reply['bytes'] <= maximum
                        or not isinstance(reply['sha256'], str)
                        or not re.fullmatch('[0-9a-f]{64}', reply['sha256'])):
                    raise TrustError('Invalid bounded HTTPS transfer result.')
                path = root / 'response.bin'
                before = file_info(path)
                if before.st_size != reply['bytes']:
                    raise TrustError('Downloaded object length changed.')
                with _open_json_file(path) as stream:
                    opened = os.fstat(stream.fileno())
                    if _stamp(opened) != _stamp(before) or not stat.S_ISREG(opened.st_mode):
                        raise TrustError('Downloaded object changed before reading.')
                    digest, used = sha256(), 0
                    while block := stream.read(min(65536, maximum + 1 - used)):
                        check()
                        used += len(block)
                        if used > maximum:
                            raise TrustError('Downloaded object exceeded its limit.')
                        digest.update(block)
                        yield block
                    if (used != reply['bytes'] or digest.hexdigest() != reply['sha256']
                            or _stamp(os.fstat(stream.fileno())) != _stamp(before)
                            or _stamp(file_info(path)) != _stamp(before)):
                        raise TrustError('Downloaded object changed while reading.')
    return ReviewedHTTPSFetcher()


class HTTPSRepositoryClient(LocalRepositoryClient):
    """Same TUF refresh/download/VerifiedPackage authority, explicit HTTPS only."""
    def __init__(self, cache, policy, repository):
        if type(repository) is not HTTPSRepository:
            raise TrustError('A reviewed HTTPS repository configuration is required.')
        self.repository = repository
        super().__init__(cache, policy)

    def _updater(self, repository, cancelled):
        from tuf.ngclient import Updater
        from tuf.ngclient.config import UpdaterConfig
        if repository != self.repository or type(repository) is not HTTPSRepository:
            raise TrustError('The requested source differs from the configured HTTPS repository.')
        _cancel(cancelled)
        expected = json_bytes(dict(schema='carr-reviewed-update-source/1',
            url=repository.url, ca_sha256=repository.ca_sha256,
            bootstrap_sha256=self.policy.bootstrap_sha256,
            product=self.policy.product, channel=self.policy.channel))
        binding = self.cache / 'https-source.json'
        if os.path.lexists(binding):
            if read_bytes(binding, 8192) != expected:
                raise TrustError('This update cache belongs to another reviewed source.')
        else:
            atomic_write(binding, expected)
        _metadata_cache(self.metadata)
        return Updater(str(self.metadata), repository.url + 'metadata/',
            str(self.downloads), repository.url + 'targets/',
            fetcher=_fetcher(repository, self.cache, cancelled, self.policy.max_package_bytes),
            config=UpdaterConfig(max_root_rotations=32, max_delegations=0,
                snapshot_max_length=2_000_000, targets_max_length=5_000_000),
            bootstrap=self.policy.bootstrap)

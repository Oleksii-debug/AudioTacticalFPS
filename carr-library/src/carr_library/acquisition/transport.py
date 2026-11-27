"""Explicit, narrowly scoped HTTPS reads for public bibliographic acquisition.

No process-wide network policy is changed. Only GET is supported, proxies and
cookies are never consulted, and every connection is pinned to a checked public
IP while TLS still authenticates the original hostname. Provider responses are
untrusted data. This module never executes document content or uploads a body.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
import http.client
import ipaddress
import math
import json
import os
from pathlib import Path
import re
import socket
import ssl
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import monotonic, sleep
from urllib.parse import urlencode, urljoin, urlsplit


PROVIDER_HOSTS = {
    'crossref': 'api.crossref.org',
    'unpaywall': 'api.unpaywall.org',
    'europe_pmc': 'www.ebi.ac.uk',
    'openalex': 'api.openalex.org',
    'openlibrary': 'openlibrary.org',
}
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class NetworkConsent:
    allowed: bool = False
    purpose: str = 'metadata'
    approved_hosts: tuple[str, ...] = ()
    max_bytes: int = MAX_JSON_BYTES
    timeout: float = 30.0


@dataclass(frozen=True)
class DownloadInfo:
    url: str
    sha256: str
    size: int
    content_type: str


class AcquisitionNetworkError(ValueError):
    """A safe, user-readable failure with no request URL or contact address."""


def validate_https_url(url, approved_hosts):
    """Return an exact approved hostname and ASCII request target."""
    if (not isinstance(url, str) or not 0 < len(url) <= 8192
            or any(ord(c) < 33 or ord(c) == 127 or c == '\\' for c in url)):
        raise AcquisitionNetworkError('The source URL is not a valid HTTPS address.')
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        if (parsed.scheme != 'https' or not host or parsed.username is not None
                or parsed.password is not None or parsed.port not in (None, 443)
                or parsed.fragment or host.endswith('.') or '%' in host):
            raise ValueError()
        host = host.encode('idna').decode('ascii').lower()
        if (len(host) > 253 or not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', host)
                or any(not label or len(label) > 63 or label.startswith('-')
                       or label.endswith('-') for label in host.split('.'))):
            raise ValueError()
    except (ValueError, UnicodeError, TypeError):
        raise AcquisitionNetworkError('The source URL is not a valid HTTPS address.') from None
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise AcquisitionNetworkError('Direct IP addresses are not permitted for acquisition.')
    try:
        target = parsed.path or '/'
        if parsed.query:
            target += '?' + parsed.query
        target.encode('ascii')
        if re.search(r'%(?![0-9a-fA-F]{2})', target):
            raise ValueError()
    except (ValueError, UnicodeError):
        raise AcquisitionNetworkError('The source URL is not a valid HTTPS address.') from None
    if host not in approved_hosts:
        raise AcquisitionNetworkError('This host was not approved for this operation. Review the source again.')
    return host, target


def _public_addresses(host, resolver):
    try:
        records = resolver(host, 443, type=socket.SOCK_STREAM)
        addresses = tuple(dict.fromkeys(item[4][0] for item in records))
        if not addresses or len(addresses) > 32:
            raise ValueError()
        for address in addresses:
            ip = ipaddress.ip_address(address)
            if (not ip.is_global or ip.is_multicast or ip.is_unspecified
                    or getattr(ip, 'ipv4_mapped', None) is not None
                    or getattr(ip, 'sixtofour', None) is not None
                    or getattr(ip, 'teredo', None) is not None):
                raise ValueError()
        return addresses
    except (OSError, ValueError, IndexError, TypeError):
        raise AcquisitionNetworkError('The host does not resolve exclusively to public Internet addresses.') from None


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, addresses, timeout):
        super().__init__(host, 443, timeout=timeout, context=ssl.create_default_context())
        self.addresses = addresses

    def connect(self):
        # A literal IP prevents a second resolver result from changing the peer.
        last = None
        for address in self.addresses[:2]:
            plain = None
            try:
                plain = socket.create_connection((address, 443), self.timeout)
                self.sock = self._context.wrap_socket(plain, server_hostname=self.host)
                return
            except OSError as exc:
                last = exc
                if plain is not None:
                    plain.close()
        raise AcquisitionNetworkError('Could not establish a verified HTTPS connection.') from last


@contextmanager
def _open_https(url, timeout, addresses):
    host, target = validate_https_url(url, (urlsplit(url).hostname,))
    connection = _PinnedHTTPSConnection(host, addresses, timeout)
    try:
        connection.request('GET', target, headers={
            'User-Agent': 'UniversalResearchLibrary/0.2 (explicit public DOI acquisition)',
            'Accept': 'application/json, application/pdf;q=0.9',
            'Accept-Encoding': 'identity',
            'Connection': 'close',
        })
        response = connection.getresponse()
        try:
            yield response
        finally:
            response.close()
    finally:
        connection.close()


class SafeTransport:
    """One bounded operation, reusable only within its original consent."""
    def __init__(self, consent, *, opener=None, resolver=None, clock=monotonic, sleeper=sleep,
                 _in_worker=False):
        if not isinstance(consent, NetworkConsent):
            raise ValueError('A scoped network permission is required.')
        if (type(consent.allowed) is not bool or consent.purpose not in ('metadata', 'download')
                or not isinstance(consent.approved_hosts, tuple)
                or not all(isinstance(h, str) for h in consent.approved_hosts)
                or type(consent.max_bytes) is not int
                or not 1 <= consent.max_bytes <= MAX_DOWNLOAD_BYTES
                or type(consent.timeout) not in (float, int)
                or not math.isfinite(consent.timeout) or not 0 < consent.timeout <= 120):
            raise ValueError('Invalid scoped acquisition limits.')
        self.consent = consent
        self.opener = opener or _open_https
        self.resolver = resolver or socket.getaddrinfo
        self.clock, self.sleeper = clock, sleeper
        self.deadline = clock() + consent.timeout
        self.requests = 0
        self.isolated = not _in_worker and opener is None and resolver is None

    def _check(self, cancelled):
        if self.consent.allowed is not True:
            raise PermissionError('Online access is disabled. Approve this lookup or download explicitly.')
        if cancelled():
            raise InterruptedError('Acquisition cancelled.')
        if self.clock() >= self.deadline:
            raise TimeoutError('The online operation timed out. No automatic background retry was started.')

    def _wait(self, seconds, cancelled):
        until = self.clock() + seconds
        while self.clock() < until:
            self._check(cancelled)
            self.sleeper(min(0.1, until - self.clock()))

    def __call__(self, request, *, cancelled=lambda: False):
        self._check(cancelled)
        if self.consent.purpose != 'metadata':
            raise PermissionError('This permission does not allow a metadata lookup.')
        if request.provider not in PROVIDER_HOSTS:
            raise AcquisitionNetworkError('Unsupported metadata provider.')
        host = PROVIDER_HOSTS[request.provider]
        validate_https_url(request.endpoint, (host,))
        url = request.endpoint
        if request.query:
            if urlsplit(url).query:
                raise AcquisitionNetworkError('Invalid metadata request.')
            url += '?' + urlencode(request.query)
        import io
        output = io.BytesIO()
        self._transfer(url, output, cancelled=cancelled, metadata=True)
        return output.getvalue()

    def download(self, url, fileobj, *, cancelled=lambda: False):
        self._check(cancelled)
        if self.consent.purpose != 'download':
            raise PermissionError('Metadata permission does not allow document downloads.')
        return self._transfer(url, fileobj, cancelled=cancelled, metadata=False)

    def _transfer(self, url, output, *, cancelled, metadata):
        initial_host, _ = validate_https_url(url, self.consent.approved_hosts)
        # Selection of several providers does not authorize one provider to
        # redirect its contact details to another. Narrow each metadata request
        # without mutating the reusable consent or its cumulative budgets.
        request_hosts = (initial_host,) if metadata else self.consent.approved_hosts
        if self.isolated:
            return self._isolated_transfer(url, output, cancelled=cancelled, metadata=metadata,
                                           approved_hosts=request_hosts)
        redirects, retries = 0, 0
        while True:
            self._check(cancelled)
            host, _ = validate_https_url(url, request_hosts)
            if self.requests >= 12:
                raise AcquisitionNetworkError('The approved operation reached its request budget.')
            addresses = _public_addresses(host, self.resolver)
            self._check(cancelled)
            self.requests += 1
            timeout = min(8.0, self.deadline - self.clock())
            try:
                with self.opener(url, timeout, addresses) as response:
                    self._check(cancelled)
                    status = response.status
                    if status in (301, 302, 303, 307, 308):
                        if redirects >= 3:
                            raise AcquisitionNetworkError('The source returned too many redirects.')
                        location = response.headers.get('Location')
                        if not isinstance(location, str) or len(location) > 8192:
                            raise AcquisitionNetworkError('The source returned an invalid redirect.')
                        candidate = urljoin(url, location)
                        validate_https_url(candidate, request_hosts)
                        url = candidate
                        redirects += 1
                        continue
                    if status in (429, 503):
                        retry = response.headers.get('Retry-After', '1')
                        if retries >= 2 or not str(retry).isdigit() or int(retry) > 5:
                            raise AcquisitionNetworkError('The provider is busy or rate limited. Try again later.')
                        retries += 1
                        wait = max(1, int(retry))
                    elif status != 200:
                        if status == 404:
                            raise AcquisitionNetworkError('This DOI or source was not found by the selected provider.')
                        raise AcquisitionNetworkError('The provider could not complete this request.')
                    else:
                        encoding = response.headers.get('Content-Encoding', 'identity').lower().strip()
                        if encoding not in ('', 'identity'):
                            raise AcquisitionNetworkError('Compressed transport responses are not accepted.')
                        content_type = response.headers.get('Content-Type', '').split(';')[0].strip().lower()
                        accepted = ('application/json', 'application/vnd.api+json') if metadata else (
                            'application/pdf', 'application/octet-stream', '')
                        if content_type not in accepted:
                            raise AcquisitionNetworkError('The source did not return the expected JSON or PDF file.')
                        raw_size = response.headers.get('Content-Length')
                        expected_size = None
                        maximum = min(self.consent.max_bytes, MAX_JSON_BYTES) if metadata else self.consent.max_bytes
                        if raw_size is not None:
                            if not str(raw_size).isdigit():
                                raise AcquisitionNetworkError('Invalid response size.')
                            expected_size = int(raw_size)
                            if not 0 < expected_size <= maximum:
                                raise AcquisitionNetworkError('The response exceeds the approved size limit or is empty.')
                        digest, size = sha256(), 0
                        prefix = b''
                        while True:
                            self._check(cancelled)
                            chunk = response.read(min(65536, maximum - size + 1))
                            self._check(cancelled)
                            if not chunk:
                                break
                            if not isinstance(chunk, bytes):
                                raise AcquisitionNetworkError('Invalid response data.')
                            size += len(chunk)
                            if size > maximum:
                                raise AcquisitionNetworkError('The response exceeds the approved size limit.')
                            if len(prefix) < 5:
                                prefix = (prefix + chunk)[:5]
                            if not metadata and len(prefix) == 5 and prefix != b'%PDF-':
                                raise AcquisitionNetworkError('The download is not a PDF document. A sign-in or landing page cannot be imported.')
                            digest.update(chunk)
                            output.write(chunk)
                        if size == 0 or expected_size is not None and expected_size != size:
                            raise AcquisitionNetworkError('The response was empty or truncated.')
                        if not metadata and prefix != b'%PDF-':
                            raise AcquisitionNetworkError('The download is not a PDF document.')
                        self._check(cancelled)
                        return DownloadInfo(url, digest.hexdigest(), size, content_type)
                self._wait(wait, cancelled)
            except (PermissionError, InterruptedError, TimeoutError, AcquisitionNetworkError):
                raise
            except (OSError, http.client.HTTPException, UnicodeError):
                raise AcquisitionNetworkError('The secure transfer failed. No automatic alternate source was contacted.') from None

    def _isolated_transfer(self, url, output, *, cancelled, metadata, approved_hosts):
        """Parent-enforced deadline also bounds DNS, TLS and a trickling body."""
        self._check(cancelled)
        if self.requests >= 12:
            raise AcquisitionNetworkError('The approved operation reached its request budget.')
        validate_https_url(url, approved_hosts)
        worker = Path(__file__).with_name('network_worker.py')
        with TemporaryDirectory(prefix='carr-public-acquisition-') as temporary:
            payload_file = Path(temporary) / 'response.bin'
            request = json.dumps(dict(url=url, metadata=metadata,
                approved_hosts=approved_hosts,
                max_bytes=self.consent.max_bytes,
                spent_requests=self.requests,
                timeout=min(120.0, self.deadline - self.clock()),
                output=str(payload_file)), ensure_ascii=True).encode('ascii')
            process = subprocess.Popen([sys.executable, '-I', '-B', str(worker)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), close_fds=True)
            first = True
            try:
                while True:
                    self._check(cancelled)
                    try:
                        stdout, stderr = process.communicate(input=request if first else None, timeout=0.1)
                        break
                    except subprocess.TimeoutExpired:
                        first = False
                self._check(cancelled)
                if len(stdout) > 32768 or stderr:
                    raise AcquisitionNetworkError('The isolated transfer returned an invalid result.')
                try:
                    result = json.loads(stdout)
                except (ValueError, UnicodeError):
                    raise AcquisitionNetworkError('The isolated transfer did not complete.') from None
                if not isinstance(result, dict):
                    raise AcquisitionNetworkError('The isolated transfer returned an invalid result.')
                spent = result.get('requests', self.requests)
                if type(spent) is not int or not self.requests <= spent <= 12:
                    raise AcquisitionNetworkError('The isolated transfer exceeded its request budget.')
                self.requests = spent
                if process.returncode != 0 or result.get('ok') is not True:
                    code = result.get('error')
                    if code == 'timeout':
                        raise TimeoutError('The online operation timed out.')
                    if code == 'cancelled':
                        raise InterruptedError('Acquisition cancelled.')
                    safe_messages = {
                        'not_found': 'This DOI or source was not found by the selected provider.',
                        'rate_limit': 'The provider is busy or rate limited. Try again later.',
                        'redirect': 'The source redirected to an unapproved host. Review a fresh provider source.',
                        'size': 'The response exceeds the approved size limit.',
                        'format': 'The source did not return the expected JSON or PDF file.',
                    }
                    raise AcquisitionNetworkError(safe_messages.get(code,
                        'The secure transfer failed. No alternate source was contacted.'))
                info = result.get('info', {})
                if not isinstance(info, dict):
                    raise AcquisitionNetworkError('The isolated transfer returned an invalid manifest.')
                validate_https_url(info.get('url'), approved_hosts)
                file_info = payload_file.lstat()
                if (not stat.S_ISREG(file_info.st_mode) or file_info.st_nlink != 1
                        or getattr(file_info, 'st_file_attributes', 0) & 0x400
                        or not 0 < file_info.st_size <= self.consent.max_bytes
                        or info.get('size') != file_info.st_size
                        or not isinstance(info.get('sha256'), str)
                        or not re.fullmatch('[0-9a-f]{64}', info['sha256'])):
                    raise AcquisitionNetworkError('The downloaded payload could not be verified.')
                digest, size = sha256(), 0
                with payload_file.open('rb') as stream:
                    while True:
                        self._check(cancelled)
                        chunk = stream.read(65536)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > self.consent.max_bytes:
                            raise AcquisitionNetworkError('The downloaded payload exceeds its budget.')
                        digest.update(chunk)
                if size != info['size'] or digest.hexdigest() != info['sha256']:
                    raise AcquisitionNetworkError('The downloaded payload changed before publication.')
                # Verify the entire staged payload before exposing any byte to
                # the caller. Then copy in bounded chunks from the same private
                # completed worker output, checking cancellation throughout.
                with payload_file.open('rb') as stream:
                    while True:
                        self._check(cancelled)
                        chunk = stream.read(65536)
                        if not chunk:
                            break
                        output.write(chunk)
                self._check(cancelled)
                return DownloadInfo(info['url'], info['sha256'], size, info.get('content_type', ''))
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.communicate(timeout=1.0)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.communicate(timeout=1.0)


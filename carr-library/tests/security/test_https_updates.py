"""Actual TLS and synthetic TUF authority. Run before outbound-denial gates.

No production host, publisher key or entitlement is used. Every repository GET
is made by the real isolated HTTPS child and checked by the pinned python-tuf.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
from pathlib import Path
import ssl
import threading
import time
from urllib.parse import unquote

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
import pytest

from carr_library.security._files import TrustError
from carr_library.security.https import HTTPSRepository, HTTPSRepositoryClient
from carr_library.security.updates import UpdateCancelled


@contextmanager
def tls_repository(repository, tmp_path, *, mode='normal'):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Synthetic TUF test only')])
    now = datetime.now(timezone.utc)
    certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), critical=False)
        .sign(key, hashes.SHA256()))
    ca = certificate.public_bytes(serialization.Encoding.PEM)
    cert, private = tmp_path / 'tls-cert.pem', tmp_path / 'tls-key.pem'
    cert.write_bytes(ca)
    private.write_bytes(key.private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    requested, started, release = [], threading.Event(), threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            requested.append(self.path)
            if not self.path.startswith('/reviewed/'):
                self.send_error(400)
                return
            relative = unquote(self.path[len('/reviewed/'):])
            path = repository.root / relative
            if not path.is_relative_to(repository.root) or not path.is_file():
                self.send_error(404)
                return
            raw = path.read_bytes()
            is_timestamp = relative == 'metadata/timestamp.json'
            try:
                if mode == 'redirect' and is_timestamp:
                    self.send_response(302)
                    self.send_header('Location', '/other/metadata/timestamp.json')
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header('Content-Type', 'application/octet-stream')
                self.send_header('Content-Length',
                    str(10**20 if mode == 'oversize' and is_timestamp else len(raw)))
                if mode == 'encoded' and is_timestamp:
                    self.send_header('Content-Encoding', 'gzip')
                self.end_headers()
                if mode == 'slow' and is_timestamp:
                    self.wfile.write(raw[:1])
                    self.wfile.flush()
                    started.set()
                    release.wait(10)
                    raw = raw[1:]
                if mode == 'truncated' and is_timestamp:
                    raw = raw[:-1]
                self.wfile.write(raw)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ssl.SSLError):
                pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, private)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .05})
    thread.start()
    source = HTTPSRepository(f'https://127.0.0.1:{server.server_port}/reviewed/',
        ca_pem=ca, ca_sha256=sha256(ca).hexdigest(), timeout_seconds=20)
    try:
        yield source, requested, started, release
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(5)
        assert not thread.is_alive()


def test_real_https_tuf_roundtrip_returns_only_authenticated_package(repository, tmp_path, monkeypatch):
    package = b'synthetic signed update bytes'
    repository.publish(package)
    # Ambient proxy variables must not receive the selected repository request.
    monkeypatch.setenv('HTTPS_PROXY', 'http://127.0.0.1:1')
    with tls_repository(repository, tmp_path) as (source, requested, _, _):
        client = HTTPSRepositoryClient(tmp_path / 'cache', repository.policy(), source)
        assert requested == []
        release = client.check(source, repository.target_path)
        verified = client.download(source, repository.target_path, reviewed_sha256=release.sha256)
        assert verified.path.read_bytes() == package
        assert verified.release == release
        assert release.sha256 == sha256(package).hexdigest()
        assert requested and all(path.startswith('/reviewed/') for path in requested)
        assert any(path.endswith('.release.zip') for path in requested)
        assert not tuple(client.cache.glob('.https-*'))
        assert (client.cache / 'https-source.json').is_file()


@pytest.mark.parametrize('mode', ['redirect', 'oversize', 'encoded', 'truncated'])
def test_transport_rejects_unreviewed_or_ambiguous_response_before_package(repository, tmp_path, mode):
    repository.publish(b'synthetic bytes')
    with tls_repository(repository, tmp_path, mode=mode) as (source, requested, _, _):
        client = HTTPSRepositoryClient(tmp_path / 'cache', repository.policy(), source)
        with pytest.raises(Exception):
            client.check(source, repository.target_path)
        assert not tuple(client.downloads.glob('*.zip'))
        assert all(path.startswith('/reviewed/') for path in requested)
        assert not tuple(client.cache.glob('.https-*'))


def test_actual_tls_certificate_is_verified_before_any_repository_get(repository, tmp_path):
    repository.publish(b'synthetic bytes')
    with tls_repository(repository, tmp_path) as (reviewed, requested, _, _):
        untrusted = HTTPSRepository(reviewed.url, timeout_seconds=10)
        client = HTTPSRepositoryClient(tmp_path / 'cache', repository.policy(), untrusted)
        with pytest.raises(Exception):
            client.check(untrusted, repository.target_path)
        assert requested == []
        assert not tuple(client.downloads.glob('*.zip'))


@pytest.mark.parametrize('mutation', ['signature', 'expired', 'channel', 'target'])
def test_https_never_replaces_tuf_authority(repository, tmp_path, mutation):
    package = b'authenticated bytes'
    info = repository.publish(package)
    if mutation == 'signature':
        path = repository.root / 'metadata' / 'timestamp.json'
        raw = path.read_bytes()
        # Alter signed content without re-signing it.
        assert b'"version": 1' in raw or b'"version":1' in raw
        raw = raw.replace(b'"version": 1', b'"version": 9').replace(b'"version":1', b'"version":9')
        path.write_bytes(raw)
    elif mutation == 'expired':
        repository.md['timestamp'].signed.expires = datetime.now(timezone.utc) - timedelta(days=1)
        repository.flush()
    elif mutation == 'channel':
        repository.publish(package, custom={'channel': 'unreviewed'})
    else:
        (repository.root / 'targets' / (info.hashes['sha256'] + '.release.zip')).write_bytes(b'foreign equal-size')
    with tls_repository(repository, tmp_path) as (source, _, _, _):
        client = HTTPSRepositoryClient(tmp_path / 'cache', repository.policy(), source)
        with pytest.raises(Exception):
            if mutation == 'target':
                reviewed = client.check(source, repository.target_path)
                client.download(source, repository.target_path, reviewed_sha256=reviewed.sha256)
            else:
                client.check(source, repository.target_path)
        assert not tuple(client.downloads.glob('*.zip'))


def test_reviewed_hash_and_origin_cannot_be_substituted(repository, tmp_path):
    repository.publish(b'first')
    with tls_repository(repository, tmp_path) as (source, requested, _, _):
        client = HTTPSRepositoryClient(tmp_path / 'cache', repository.policy(), source)
        release = client.check(source, repository.target_path)
        before = len(requested)
        with pytest.raises(TrustError):
            client.check(HTTPSRepository(source.url.replace('/reviewed/', '/elsewhere/')),
                         repository.target_path)
        assert len(requested) == before
        repository.advance()
        repository.publish(b'second')
        with pytest.raises(TrustError, match='changed after review'):
            client.download(source, repository.target_path, reviewed_sha256=release.sha256)
        assert not tuple(client.downloads.glob('*.zip'))


def test_real_owned_transfer_cancellation_waits_for_child_cleanup(repository, tmp_path):
    repository.publish(b'synthetic')
    cancel, finished = threading.Event(), threading.Event()
    errors = []
    with tls_repository(repository, tmp_path, mode='slow') as (source, requested, started, release):
        client = HTTPSRepositoryClient(tmp_path / 'cache', repository.policy(), source)
        def work():
            try:
                client.check(source, repository.target_path, cancelled=cancel.is_set)
            except Exception as error:
                errors.append(error)
            finally:
                finished.set()
        thread = threading.Thread(target=work)
        thread.start()
        try:
            assert started.wait(10), requested
            cancel.set()
            assert finished.wait(10)
        finally:
            cancel.set()
            release.set()
            thread.join(15)
        assert not thread.is_alive()
        assert len(errors) == 1 and isinstance(errors[0], UpdateCancelled)
        assert not tuple(client.cache.glob('.https-*'))
        assert not tuple(client.downloads.glob('*.zip'))


def test_real_trickling_tls_body_has_one_total_deadline(repository, tmp_path):
    repository.publish(b'synthetic')
    with tls_repository(repository, tmp_path, mode='slow') as (source, requested, started, release):
        source = HTTPSRepository(source.url, timeout_seconds=5,
                                 ca_pem=source.ca_pem, ca_sha256=source.ca_sha256)
        client = HTTPSRepositoryClient(tmp_path / 'cache', repository.policy(), source)
        began = time.monotonic()
        with pytest.raises(Exception):
            client.check(source, repository.target_path)
        assert started.is_set(), requested
        assert time.monotonic() - began < 15
        assert not tuple(client.cache.glob('.https-*'))
        assert not tuple(client.downloads.glob('*.zip'))

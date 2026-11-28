"""Real local TLS/TUF transfers; explicit loopback-only test gate is required.

Synthetic keys/certificates and packages stay in tmp_path. These cases do not
contact a publisher, install an application, or qualify production TLS setup.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import http.server
import os
from pathlib import Path
import socket
import ssl
import threading
import time

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from carr_library.security._files import TrustError
from carr_library.security.update_transport import HttpsRepositoryClient
from carr_library.security.updates import UpdateCancelled

from .conftest import Repository


@contextmanager
def site(tmp_path, repository, *, behavior='ordinary'):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Synthetic localhost update test')])
    now = datetime.now(timezone.utc)
    certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName('localhost')]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(key, hashes.SHA256()))
    pem = certificate.public_bytes(serialization.Encoding.PEM)
    cert, private = tmp_path / 'tls.crt', tmp_path / 'tls.key'
    cert.write_bytes(pem)
    private.write_bytes(key.private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    observations = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            observations.append((self.path, self.headers.get('Range')))
            if not self.path.startswith('/updates/'):
                self.send_error(404)
                return
            relative = self.path[len('/updates/'):]
            source = repository.root / relative
            if not source.is_file():
                self.send_error(404)
                return
            data = source.read_bytes()
            target = relative.startswith('targets/')
            if target and behavior == 'redirect':
                self.send_response(302)
                self.send_header('Location', 'https://unapproved.invalid/never-request')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            if target and behavior == 'compressed':
                self.send_response(200)
                self.send_header('Content-Length', str(len(data)))
                self.send_header('Content-Encoding', 'gzip')
                self.end_headers()
                return
            if target and behavior == 'tamper':
                data = bytes([data[0] ^ 1]) + data[1:]
            offset = 0
            if self.headers.get('Range') and behavior != 'ignore-range':
                value = self.headers['Range']
                assert value.startswith('bytes=') and value.endswith('-')
                offset = int(value[6:-1])
                self.send_response(206)
                self.send_header('Content-Range', f'bytes {offset}-{len(data) - 1}/{len(data)}')
            else:
                self.send_response(200)
            length = len(data) - offset
            self.send_header('Content-Length', str(length))
            self.end_headers()
            try:
                for start in range(offset, len(data), 8192):
                    self.wfile.write(data[start:start + 8192])
                    self.wfile.flush()
                    if target and behavior in ('slow', 'ignore-range'):
                        time.sleep(.01)
            except (BrokenPipeError, ConnectionResetError, ssl.SSLError):
                pass  # The owned client was really cancelled or refused framing.

    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = False
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, private)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .02})
    thread.start()
    try:
        yield f'https://localhost:{server.server_address[1]}/updates/', pem.decode('ascii'), observations
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


def test_real_tls_tuf_download_authenticates_exact_target_and_reuses_verified_cache(tmp_path):
    repository = Repository(tmp_path / 'repo')
    package = b'Synthetic signed update bytes' * 3000
    repository.publish(package)
    with site(tmp_path, repository) as (endpoint, ca, requests):
        client = HttpsRepositoryClient(tmp_path / 'cache', repository.policy(),
                                       endpoint=endpoint, ca_pem=ca, timeout=30)
        review = client.check(endpoint, repository.target_path)
        result = client.download(endpoint, repository.target_path, reviewed_sha256=review.sha256)
        assert result.path.read_bytes() == package and result.release == review
        targets = [row for row in requests if '/targets/' in row[0]]
        assert targets == [('/updates/targets/' + sha256(package).hexdigest() + '.release.zip', None)]
        assert not tuple(client.partials.iterdir())
        again = client.download(endpoint, repository.target_path, reviewed_sha256=review.sha256)
        assert again == result
        assert [row for row in requests if '/targets/' in row[0]] == targets


@pytest.mark.parametrize('behavior', ['slow', 'ignore-range'])
def test_cancelled_real_transfer_retains_prefix_and_resumes_with_full_signed_verification(tmp_path, behavior):
    repository = Repository(tmp_path / 'repo')
    package = bytes(range(256)) * 3500
    repository.publish(package)
    stopped = threading.Event()
    with site(tmp_path, repository, behavior=behavior) as (endpoint, ca, requests):
        client = HttpsRepositoryClient(tmp_path / 'cache', repository.policy(),
                                       endpoint=endpoint, ca_pem=ca, timeout=30)
        review = client.check(endpoint, repository.target_path)
        def progress(completed, total, phase):
            assert total == len(package) and 'not yet verified' in phase
            if 65536 <= completed < total:
                stopped.set()
        with pytest.raises(UpdateCancelled):
            client.download(endpoint, repository.target_path, reviewed_sha256=review.sha256,
                            cancelled=stopped.is_set, progress=progress)
        partial = client.partials / review.sha256 / 'response.part'
        prefix = partial.read_bytes()
        assert 0 < len(prefix) < len(package) and prefix == package[:len(prefix)]
        assert not (client.downloads / (review.sha256 + '.zip')).exists()
        stopped.clear()
        result = client.download(endpoint, repository.target_path, reviewed_sha256=review.sha256)
        assert result.path.read_bytes() == package
        targets = [row for row in requests if '/targets/' in row[0]]
        assert len(targets) == 2 and targets[1][1] == f'bytes={len(prefix)}-'
        assert not partial.exists()


@pytest.mark.parametrize('behavior', ['redirect', 'compressed', 'tamper'])
def test_real_https_refuses_redirect_encoded_or_tampered_body_without_activating(tmp_path, behavior):
    repository = Repository(tmp_path / 'repo')
    repository.publish(b'Exact reviewed synthetic bytes')
    with site(tmp_path, repository, behavior=behavior) as (endpoint, ca, requests):
        client = HttpsRepositoryClient(tmp_path / 'cache', repository.policy(),
                                       endpoint=endpoint, ca_pem=ca, timeout=30)
        review = client.check(endpoint, repository.target_path)
        with pytest.raises(Exception):
            client.download(endpoint, repository.target_path, reviewed_sha256=review.sha256)
        assert not (client.downloads / (review.sha256 + '.zip')).exists()
        assert all(path.startswith('/updates/') for path, _ in requests)
        assert len([row for row in requests if '/targets/' in row[0]]) == 1


def test_actual_tls_certificate_is_required_before_any_signed_metadata(tmp_path):
    repository = Repository(tmp_path / 'repo')
    repository.publish(b'Synthetic')
    with site(tmp_path, repository) as (endpoint, ca, requests):
        client = HttpsRepositoryClient(tmp_path / 'cache', repository.policy(),
                                       endpoint=endpoint, timeout=15)
        with pytest.raises(TrustError):
            client.check(endpoint, repository.target_path)
        assert requests == [] and not tuple(client.downloads.glob('*.zip'))


def test_review_change_refuses_before_target_transfer_and_never_uses_alternate_site(tmp_path):
    repository = Repository(tmp_path / 'repo')
    repository.publish(b'First reviewed package')
    with site(tmp_path, repository) as (endpoint, ca, requests):
        client = HttpsRepositoryClient(tmp_path / 'cache', repository.policy(),
                                       endpoint=endpoint, ca_pem=ca, timeout=30)
        review = client.check(endpoint, repository.target_path)
        repository.advance()
        repository.publish(b'Another package', build=3)
        with pytest.raises(TrustError, match='changed after review'):
            client.download(endpoint, repository.target_path, reviewed_sha256=review.sha256)
        count = len(requests)
        with pytest.raises(TrustError, match='exact configured'):
            client.check(endpoint.replace('/updates/', '/elsewhere/'), repository.target_path)
        assert len(requests) == count and not any('/targets/' in row[0] for row in requests)


def test_parent_deadline_reaps_actual_tls_worker_and_preserves_unverified_prefix(tmp_path):
    repository = Repository(tmp_path / 'repo')
    repository.publish(bytes(range(256)) * 3500)
    with site(tmp_path, repository, behavior='slow') as (endpoint, ca, requests):
        client = HttpsRepositoryClient(tmp_path / 'cache', repository.policy(),
                                       endpoint=endpoint, ca_pem=ca, timeout=30)
        review = client.check(endpoint, repository.target_path)
        client.timeout = 1
        started = time.monotonic()
        with pytest.raises((TrustError, TimeoutError)):
            client.download(endpoint, repository.target_path, reviewed_sha256=review.sha256)
        assert time.monotonic() - started < 8
        assert not (client.downloads / (review.sha256 + '.zip')).exists()
        # run_process must settle its real Job/process group before returning.
        partial = client.partials / review.sha256 / 'response.part'
        if partial.exists():
            before = partial.read_bytes()
            time.sleep(.1)
            assert partial.read_bytes() == before


@pytest.mark.parametrize('bad', ['http://localhost/', 'https://localhost/',
    'https://name.invalid/%2e/', 'https://name.invalid/path/?secret=x',
    'https://user:password@name.invalid/', 'https://name.invalid/no-trailing-slash'])
def test_network_configuration_is_explicit_and_cannot_supply_unsafe_url_authority(tmp_path, bad):
    repository = Repository(tmp_path / 'repo')
    if bad == 'https://localhost/':
        # A legitimate hostname is not consent for a different repository argument.
        client = HttpsRepositoryClient(tmp_path / 'cache', repository.policy(), endpoint=bad)
        with pytest.raises(TrustError):
            client.check('https://another.invalid/', 'release.zip')
    else:
        with pytest.raises(TrustError):
            HttpsRepositoryClient(tmp_path / 'cache', repository.policy(), endpoint=bad)

"""Real loopback TLS, signed TUF and byte ranges through the existing owned child.

The authored repository and temporary CA grant no production trust. These cases
run before egress-denial native gates; no public endpoint is contacted.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import re
import ssl
import threading
from types import SimpleNamespace
from urllib.parse import unquote

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
import pytest
from tuf.api.exceptions import RepositoryError

from carr_library.security._files import TrustError
from carr_library.security.download_state import PausedDownload
from carr_library.security.https import HTTPSRepository, HTTPSRepositoryClient
from carr_library.security.updates import UpdateCancelled, require_verified_package


@contextmanager
def range_repository(repository, tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Authored range test only')])
    now = datetime.now(timezone.utc)
    certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.SubjectAlternativeName(
            [x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), critical=False)
        .sign(key, hashes.SHA256()))
    ca = certificate.public_bytes(serialization.Encoding.PEM)
    cert, private = tmp_path / 'range-cert.pem', tmp_path / 'range-key.pem'
    cert.write_bytes(ca)
    private.write_bytes(key.private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    control = SimpleNamespace(mode='hold-first', held=False, requested=[],
                              body_started=threading.Event(), release=threading.Event())

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            requested_range = self.headers.get('Range')
            control.requested.append((self.path, requested_range))
            if not self.path.startswith('/reviewed/'):
                self.send_error(400); return
            relative = unquote(self.path[len('/reviewed/'):])
            path = repository.root / relative
            if not path.is_relative_to(repository.root) or not path.is_file():
                self.send_error(404); return
            raw = path.read_bytes()
            target = relative.startswith('targets/')
            offset, mode = 0, control.mode
            if requested_range is not None:
                match = re.fullmatch(r'bytes=([0-9]+)-', requested_range)
                if not target or match is None or not 0 < int(match[1]) < len(raw):
                    self.send_error(416); return
                offset = int(match[1])
            body = raw[offset:]
            response = 206 if offset else 200
            if offset and mode == 'ignored-range':
                response, body = 200, raw
            try:
                self.send_response(response)
                if response == 206:
                    start = offset + (1 if mode == 'wrong-start' else 0)
                    total = len(raw) + (1 if mode == 'wrong-total' else 0)
                    value = f'bytes {start}-{len(raw)-1}/{total}'
                    self.send_header('Content-Range', value)
                    if mode == 'duplicate-range':
                        self.send_header('Content-Range', value)
                self.send_header('Content-Type', 'application/octet-stream')
                self.send_header('Content-Length', str(len(body)))
                if offset and mode == 'encoded':
                    self.send_header('Content-Encoding', 'gzip')
                self.end_headers()
                if target and mode == 'hold-first' and not control.held:
                    control.held = True
                    prefix = body[:196608]
                    self.wfile.write(prefix); self.wfile.flush()
                    control.body_started.set()
                    control.release.wait(30)
                    body = body[len(prefix):]
                elif offset and mode == 'truncated':
                    body = body[:65536]
                elif offset and mode == 'changed-remainder':
                    body = b'X' * len(body)
                self.wfile.write(body); self.wfile.flush()
                self.close_connection = True
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
        ca_pem=ca, ca_sha256=sha256(ca).hexdigest(), timeout_seconds=60)
    try:
        yield source, control
    finally:
        control.release.set()
        server.shutdown(); server.server_close()
        thread.join(5)
        assert not thread.is_alive()


def initial(repository, tmp_path, source, control, *, cancelled=None):
    raw = bytes(range(256)) * 4096
    target = repository.publish(raw)
    original = repository.root / 'targets' / (target.hashes['sha256'] + '.release.zip')
    client = HTTPSRepositoryClient(tmp_path / 'cache', repository.policy(), source)
    review = client.check(source, repository.target_path)
    pause = threading.Event()
    reports = []
    def progress(received, total):
        reports.append((received, total))
        if received >= 65536:
            pause.set()
    partial = client.download(source, review.target, reviewed_sha256=review.sha256,
        paused=pause.is_set, progress=progress, cancelled=cancelled or (lambda: False))
    assert type(partial) is PausedDownload and partial.state == 'paused'
    assert control.body_started.is_set()
    assert 65536 <= partial.received < len(raw)
    assert not tuple(client.downloads.glob('*.zip'))
    assert not tuple(client.cache.glob('.https-*')), 'The actual HTTPS child must release its staging before pause completes.'
    roots = tuple(client.downloads.glob('.paused-*'))
    assert len(roots) == 1
    folder = roots[0]
    assert (folder / 'package.part').read_bytes() == raw[:partial.received]
    assert original.read_bytes() == raw
    control.mode = 'normal'
    control.release.set()
    return raw, client, review, original, partial, folder, reports


def test_real_tls_pause_reopen_and_exact_range_return_one_fully_verified_package(repository, tmp_path):
    with range_repository(repository, tmp_path) as (source, control):
        raw, client, review, original, partial, folder, reports = initial(repository, tmp_path, source, control)
        count = len(control.requested)
        assert client.paused_download(source, review) == partial
        assert len(control.requested) == count, 'Inspecting inert cached bytes cannot make a network request.'
        reopened = HTTPSRepositoryClient(client.cache, client.policy, source)
        assert reopened.check(source, review.target) == review
        result = reopened.download(source, review.target, reviewed_sha256=review.sha256,
                                   resume=True, paused=lambda: False)
        assert require_verified_package(result) is result and result.path.read_bytes() == raw
        targets = [row for row in control.requested if '/targets/' in row[0]]
        assert len(targets) == 2
        assert targets[0][1] is None and targets[1][1] == f'bytes={partial.received}-'
        assert all(row[0].startswith('/reviewed/') for row in control.requested)
        assert not folder.exists() and original.read_bytes() == raw
        assert not tuple(client.cache.glob('.https-*'))
        assert reports and all(total == len(raw) and 0 <= done <= total for done, total in reports)
        assert [done for done, _ in reports] == sorted(done for done, _ in reports)


@pytest.mark.parametrize('mode', ['ignored-range', 'wrong-start', 'wrong-total', 'duplicate-range', 'encoded'])
def test_real_server_cannot_append_an_unmatched_or_ambiguous_range(repository, tmp_path, mode):
    with range_repository(repository, tmp_path) as (source, control):
        raw, client, review, original, partial, folder, _reports = initial(repository, tmp_path, source, control)
        before = {path.name: path.read_bytes() for path in folder.iterdir()}
        control.mode = mode
        with pytest.raises((TrustError, RepositoryError)):
            client.download(source, review.target, reviewed_sha256=review.sha256,
                            resume=True, paused=lambda: False)
        assert {path.name: path.read_bytes() for path in folder.iterdir()} == before
        assert not tuple(client.downloads.glob('*.zip')) and original.read_bytes() == raw
        assert not tuple(client.cache.glob('.https-*'))
        assert [value for path, value in control.requested if '/targets/' in path][-1] == f'bytes={partial.received}-'


def test_real_truncated_range_keeps_only_valid_prefix_and_can_resume_again(repository, tmp_path):
    with range_repository(repository, tmp_path) as (source, control):
        raw, client, review, original, partial, folder, _reports = initial(repository, tmp_path, source, control)
        control.mode = 'truncated'
        interrupted = client.download(source, review.target, reviewed_sha256=review.sha256,
                                      resume=True, paused=lambda: False)
        assert type(interrupted) is PausedDownload and interrupted.state == 'interrupted'
        assert interrupted.received == partial.received + 65536
        assert (folder / 'package.part').read_bytes() == raw[:interrupted.received]
        assert not tuple(client.downloads.glob('*.zip'))
        control.mode = 'normal'
        result = client.download(source, review.target, reviewed_sha256=review.sha256,
                                 resume=True, paused=lambda: False)
        assert result.path.read_bytes() == raw and original.read_bytes() == raw
        assert [value for path, value in control.requested if '/targets/' in path] == [
            None, f'bytes={partial.received}-', f'bytes={interrupted.received}-']


def test_matching_range_headers_never_replace_signed_full_hash_authority(repository, tmp_path):
    with range_repository(repository, tmp_path) as (source, control):
        raw, client, review, original, partial, folder, _reports = initial(repository, tmp_path, source, control)
        control.mode = 'changed-remainder'
        with pytest.raises(RepositoryError):
            client.download(source, review.target, reviewed_sha256=review.sha256,
                            resume=True, paused=lambda: False)
        assert not tuple(client.downloads.glob('*.zip')) and original.read_bytes() == raw
        assert (folder / 'package.part').read_bytes() == raw[:partial.received] + b'X' * (len(raw) - partial.received)
        with pytest.raises(TrustError, match='verified package'):
            require_verified_package(client.paused_download(source, review))


def test_expired_current_metadata_blocks_resume_before_any_new_target_request(repository, tmp_path):
    with range_repository(repository, tmp_path) as (source, control):
        _raw, client, review, _original, _partial, folder, _reports = initial(repository, tmp_path, source, control)
        before = {path.name: path.read_bytes() for path in folder.iterdir()}
        target_count = sum('/targets/' in path for path, _ in control.requested)
        repository.advance()
        repository.md['timestamp'].signed.expires = datetime.now(timezone.utc) - timedelta(seconds=1)
        repository.flush()
        with pytest.raises(RepositoryError):
            client.download(source, review.target, reviewed_sha256=review.sha256,
                            resume=True, paused=lambda: False)
        assert sum('/targets/' in path for path, _ in control.requested) == target_count
        assert {path.name: path.read_bytes() for path in folder.iterdir()} == before
        assert not tuple(client.downloads.glob('*.zip'))


def test_actual_tls_cancel_waits_for_owned_exit_before_discarding_the_fragment(repository, tmp_path):
    with range_repository(repository, tmp_path) as (source, control):
        raw = bytes(range(256)) * 4096
        repository.publish(raw)
        client = HTTPSRepositoryClient(tmp_path / 'cache', repository.policy(), source)
        review = client.check(source, repository.target_path)
        cancel = threading.Event()
        def progress(received, total):
            if received >= 65536:
                cancel.set()
        with pytest.raises(UpdateCancelled):
            client.download(source, review.target, reviewed_sha256=review.sha256,
                            cancelled=cancel.is_set, paused=lambda: False, progress=progress)
        assert control.body_started.is_set()
        assert not tuple(client.downloads.glob('.paused-*'))
        assert not tuple(client.downloads.glob('*.zip'))
        assert not tuple(client.cache.glob('.https-*'))
        assert len([path for path, _ in control.requested if '/targets/' in path]) == 1



def test_pending_real_https_cleanup_keeps_cache_lock_and_forbids_partial_consumption(
        repository, tmp_path, monkeypatch):
    from carr_library import durable_state
    from carr_library.audio import process as owned_process
    from carr_library.audio.models import AudioCleanupPending
    from carr_library.security._files import BusyError
    permit, hold = threading.Event(), threading.Event()
    attempted, consumed = [], []
    # The actual native process and its result are unchanged. Only confirmation
    # of its real owner's cleanup is withheld until this test releases it.
    if os.name == 'nt':
        from carr_library.audio.windows_job import JobProcess
        real_cleanup = JobProcess.close
        def cleanup(owner):
            if hold.is_set() and not permit.is_set():
                attempted.append(owner)
                raise OSError('The selected real HTTPS child has not confirmed cleanup.')
            return real_cleanup(owner)
        monkeypatch.setattr(JobProcess, 'close', cleanup)
    else:
        real_cleanup = owned_process._wait_posix_group
        def cleanup(group, *args, **kwargs):
            if hold.is_set() and not permit.is_set():
                attempted.append(group)
                raise OSError('The selected real HTTPS group has not confirmed cleanup.')
            return real_cleanup(group, *args, **kwargs)
        monkeypatch.setattr(owned_process, '_wait_posix_group', cleanup)
    actual_open = durable_state._open_json_file
    def opened(path):
        if hold.is_set() and not permit.is_set() and Path(path).name == 'response.bin':
            consumed.append(str(path))
        return actual_open(path)
    monkeypatch.setattr(durable_state, '_open_json_file', opened)
    pending = None
    with range_repository(repository, tmp_path) as (source, control):
        raw = bytes(range(256)) * 4096
        repository.publish(raw)
        client = HTTPSRepositoryClient(tmp_path / 'cache', repository.policy(), source)
        review = client.check(source, repository.target_path)
        pause = threading.Event()
        def progress(received, total):
            if received >= 65536:
                hold.set(); pause.set()
        try:
            with pytest.raises(AudioCleanupPending) as caught:
                client.download(source, review.target, reviewed_sha256=review.sha256,
                                paused=pause.is_set, progress=progress)
            pending = caught.value.cleanup_owner
            assert attempted and not pending.quiescent
            assert len({id(item) if os.name == 'nt' else item for item in attempted}) == 1
            assert consumed == [], 'No response bytes may be consumed before actual owned cleanup.'
            folders = tuple(client.cache.glob('.https-*'))
            assert len(folders) == 1 and (folders[0] / 'response.bin').stat().st_size >= 65536
            fragments = tuple(client.downloads.glob('.paused-*'))
            assert len(fragments) == 1
            assert json.loads((fragments[0] / 'checkpoint.json').read_bytes())['received'] == 0
            assert (fragments[0] / 'package.part').read_bytes() == b''
            with pytest.raises(BusyError):
                client.paused_download(source, review)
            with pytest.raises(BusyError):
                client.cancel_download(source, review)
            assert not tuple(client.downloads.glob('*.zip'))
        finally:
            permit.set(); control.release.set(); control.mode = 'normal'
            if pending is not None:
                assert pending.quiescent_event.wait(15), 'Release the same retained native owner before fixture cleanup.'
                # Its native event precedes its bounded consumer cleanup callbacks.
                from time import monotonic
                deadline = monotonic() + 5
                while tuple(client.cache.glob('.https-*')):
                    assert monotonic() < deadline
                    threading.Event().wait(.01)
        from time import monotonic
        deadline = monotonic() + 5
        while True:
            try:
                state = client.paused_download(source, review)
                break
            except BusyError:
                assert monotonic() < deadline, 'The same cache lock must release after native quiescence.'
                threading.Event().wait(.01)
        assert state.state == 'interrupted' and state.received == 0
        result = client.download(source, review.target, reviewed_sha256=review.sha256,
                                 resume=True, paused=lambda: False)
        assert result.path.read_bytes() == raw and not tuple(client.downloads.glob('.paused-*'))

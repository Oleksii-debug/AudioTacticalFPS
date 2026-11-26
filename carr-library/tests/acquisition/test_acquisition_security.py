"""Independent acquisition boundaries; no sockets, servers or external requests.

The fake DNS and HTTP boundaries expose destination and timing decisions while
the production transport performs permission, URL, address and body validation.
The strict offline runner remains enabled for every test in this module.
"""
from hashlib import sha256
import io
import json
import os
from pathlib import Path
import socket
import sys
from time import monotonic
from types import SimpleNamespace

import pytest

from carr_library.acquisition.transport import (
    NetworkConsent, SafeTransport, validate_https_url,
)
from carr_library.offline_policy import OFFLINE_ENV


HOST = 'api.crossref.org'
URL = 'https://api.crossref.org/works/10.1234%2Fsynthetic'
PUBLIC = '93.184.216.34'
DENIED = (PermissionError, ValueError)


def request(endpoint=URL):
    return SimpleNamespace(provider='crossref', endpoint=endpoint, query=())


def consent(**changes):
    settings = dict(allowed=True, purpose='metadata', approved_hosts=(HOST,),
                    max_bytes=4096, timeout=30.0)
    settings.update(changes)
    return NetworkConsent(**settings)


def address(ip):
    if ':' in ip:
        return socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', (ip, 443, 0, 0)
    return socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', (ip, 443)


class Response:
    def __init__(self, payload=b'{"message": {}}', *, status=200, headers=None,
                 on_read=None, piece_size=None):
        self.payload = payload
        self.status = status
        self.headers = ({'Content-Type': 'application/json',
                         'Content-Length': str(len(payload))} if headers is None else headers)
        self.on_read = on_read
        self.piece_size = piece_size
        self.offset = 0
        self.read_sizes = []
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def read(self, size):
        assert type(size) is int and size > 0, 'A network response must be read in bounded chunks.'
        self.read_sizes.append(size)
        length = min(size, self.piece_size) if self.piece_size else size
        part = self.payload[self.offset:self.offset + length]
        self.offset += len(part)
        if self.on_read:
            self.on_read()
        return part


class Boundary:
    def __init__(self, *responses, ips=(PUBLIC,)):
        self.responses = list(responses)
        self.ips = ips
        self.resolutions = []
        self.opens = []

    def resolve(self, host, port, *, type):
        self.resolutions.append((host, port, type))
        assert port == 443 and type == socket.SOCK_STREAM
        return [address(ip) for ip in self.ips]

    def open(self, url, timeout, addresses):
        self.opens.append((url, timeout, addresses))
        assert isinstance(addresses, tuple) and addresses
        return self.responses.pop(0)

    def transport(self, permission=None, **kwargs):
        return SafeTransport(consent() if permission is None else permission,
                             opener=self.open, resolver=self.resolve, **kwargs)


def test_default_denial_happens_before_dns_opener_or_url_interpretation():
    boundary = Boundary()
    transport = boundary.transport(NetworkConsent())
    for endpoint in (URL, 'not a URL', None):
        with pytest.raises(DENIED):
            transport(request(endpoint))
    with pytest.raises(DENIED):
        transport.download(URL, io.BytesIO())
    assert boundary.resolutions == boundary.opens == []


@pytest.mark.parametrize('allowed', (1, 0, 'yes', 'false', None, [], object()))
def test_permission_requires_an_actual_boolean(allowed):
    boundary = Boundary()
    with pytest.raises(DENIED):
        boundary.transport(consent(allowed=allowed))(request())
    assert boundary.resolutions == boundary.opens == []


def test_metadata_and_document_download_are_separate_permissions():
    boundary = Boundary()
    with pytest.raises(DENIED):
        boundary.transport(consent(purpose='download'))(request())
    with pytest.raises(DENIED):
        boundary.transport().download(URL, io.BytesIO())
    assert boundary.resolutions == boundary.opens == []


@pytest.mark.parametrize('url', (
    'http://api.crossref.org/works/10.1234/synthetic',
    '//api.crossref.org/works/10.1234/synthetic',
    'file:///etc/passwd',
    'ftp://api.crossref.org/source',
    'https://secret-user:secret-password@api.crossref.org/source',
    'https://api.crossref.org:80/source',
    'https://api.crossref.org:444/source',
    'https://api.crossref.org/source#private-fragment',
    'https://api.crossref.org.evil.invalid/source',
    'https://evil.invalid/source',
    '\r\nhttps://api.crossref.org/source',
    'https://api.crossref.org/source\r\nInjected: value',
    'https://api.crossref.org/part\tpart',
))
def test_invalid_or_unapproved_url_rejected_before_dns(url):
    boundary = Boundary()
    with pytest.raises(DENIED):
        validate_https_url(url, (HOST,))
    with pytest.raises(DENIED):
        boundary.transport(consent(purpose='download')).download(url, io.BytesIO())
    assert boundary.resolutions == boundary.opens == []


def test_valid_https_url_preserves_encoded_path_and_only_approved_origin():
    assert validate_https_url(URL, (HOST,)) == (HOST, '/works/10.1234%2Fsynthetic')
    with pytest.raises(DENIED):
        validate_https_url(URL, ('crossref.org',))
    with pytest.raises(DENIED):
        validate_https_url(URL, ())


@pytest.mark.parametrize('ip', (
    '127.0.0.1', '10.1.2.3', '172.16.0.1', '192.168.1.1',
    '169.254.169.254', '0.0.0.0', '224.0.0.1', '100.64.0.1',
    '198.18.0.1', '192.0.2.1', '::', '::1', 'fc00::1',
    'fe80::1', 'ff02::1', '::ffff:127.0.0.1',
))
def test_any_nonpublic_dns_answer_rejects_entire_destination(ip):
    # A public first answer must not hide a private fallback or rebind target.
    boundary = Boundary(ips=(PUBLIC, ip))
    with pytest.raises(DENIED):
        boundary.transport()(request())
    assert len(boundary.resolutions) == 1 and boundary.opens == []


def test_empty_dns_response_fails_without_opening_transport():
    boundary = Boundary(ips=())
    with pytest.raises((OSError, *DENIED)):
        boundary.transport()(request())
    assert boundary.opens == []


def test_public_addresses_are_resolved_once_and_passed_to_pinned_opener():
    payload = '{"title":"Джерело — 東京"}'.encode('utf-8')
    response = Response(payload, piece_size=3)
    boundary = Boundary(response, ips=(PUBLIC, '2606:4700:4700::1111'))
    result = boundary.transport()(request())
    assert result == payload
    assert boundary.resolutions == [(HOST, 443, socket.SOCK_STREAM)]
    assert len(boundary.opens) == 1
    opened_url, timeout, addresses = boundary.opens[0]
    assert opened_url == URL and 0 < timeout <= 30
    assert addresses == (PUBLIC, '2606:4700:4700::1111')
    assert response.closed and len(response.read_sizes) > 1


def test_redirect_to_unapproved_host_is_rejected_without_looking_it_up():
    response = Response(b'', status=302,
                        headers={'Location': 'https://unapproved.invalid/private'})
    boundary = Boundary(response)
    with pytest.raises(DENIED):
        boundary.transport()(request())
    assert len(boundary.opens) == len(boundary.resolutions) == 1
    assert response.closed


def test_approved_redirect_is_revalidated_and_provenance_records_final_url():
    final_url = 'https://repository.example/source.pdf'
    payload = b'%PDF-1.7\nSynthetic body only.\n'
    responses = (Response(b'', status=302, headers={'Location': final_url}),
                 Response(payload, headers={'Content-Type': 'application/pdf',
                                            'Content-Length': str(len(payload))}))
    boundary = Boundary(*responses)
    output = io.BytesIO()
    info = boundary.transport(consent(purpose='download',
        approved_hosts=(HOST, 'repository.example'))).download(URL, output)
    assert [row[0] for row in boundary.resolutions] == [HOST, 'repository.example']
    assert [row[0] for row in boundary.opens] == [URL, final_url]
    assert info.url == final_url and info.sha256 == sha256(payload).hexdigest()
    assert info.size == len(payload) and info.content_type == 'application/pdf'
    assert output.getvalue() == payload and all(row.closed for row in responses)


def test_redirects_cannot_create_an_unbounded_request_chain():
    responses = [Response(b'', status=302,
                          headers={'Location': f'https://{HOST}/step-{number}'})
                 for number in range(6)]
    boundary = Boundary(*responses)
    with pytest.raises((OSError, *DENIED)):
        boundary.transport()(request())
    assert len(boundary.opens) <= 4
    assert all(response.closed for response in responses[:len(boundary.opens)])


@pytest.mark.parametrize('headers', (
    {'Content-Type': 'text/html', 'Content-Length': '2'},
    {'Content-Type': 'application/json', 'Content-Encoding': 'gzip'},
    {'Content-Type': 'application/json', 'Content-Encoding': 'br'},
    {'Content-Type': 'application/json', 'Content-Length': '-1'},
    {'Content-Type': 'application/json', 'Content-Length': 'not-an-integer'},
    {'Content-Type': 'application/json', 'Content-Length': '1, 2'},
))
def test_metadata_rejects_unexpected_type_encoding_and_invalid_length(headers):
    response = Response(b'{}', headers=headers)
    with pytest.raises((OSError, *DENIED)):
        Boundary(response).transport()(request())
    assert response.closed


@pytest.mark.parametrize('declared', ('1', '5'))
def test_content_length_mismatch_is_not_reported_as_complete(declared):
    response = Response(b'{}', headers={'Content-Type': 'application/json',
                                       'Content-Length': declared})
    with pytest.raises((OSError, *DENIED)):
        Boundary(response).transport()(request())
    assert response.closed


def test_utf8_byte_limit_is_exact_and_unknown_length_cannot_bypass_it():
    payload = '{"title":"Є"}'.encode('utf-8')
    boundary = Boundary(Response(payload))
    assert boundary.transport(consent(max_bytes=len(payload)))(request()) == payload
    for headers in ({'Content-Type': 'application/json'},
                    {'Content-Type': 'application/json', 'Content-Length': str(len(payload))}):
        response = Response(payload, headers=headers, piece_size=2)
        with pytest.raises((OSError, *DENIED)):
            Boundary(response).transport(consent(max_bytes=len(payload) - 1))(request())
        assert response.closed


def test_download_measures_actual_bytes_and_has_no_citation_authority():
    payload = b'%PDF-1.7\nSynthetic source\n'
    response = Response(payload, headers={'Content-Type': 'application/pdf'}, piece_size=4)
    output = io.BytesIO()
    info = Boundary(response).transport(consent(purpose='download')).download(URL, output)
    assert info.size == len(payload) and info.sha256 == sha256(payload).hexdigest()
    assert info.url == URL and info.content_type == 'application/pdf'
    assert output.getvalue() == payload
    assert not getattr(info, 'verified_citation', False)
    assert not getattr(info, 'verified_source', False)


def test_cancellation_precedes_dns_and_stops_a_partly_read_response():
    boundary = Boundary()
    with pytest.raises(InterruptedError):
        boundary.transport()(request(), cancelled=lambda: True)
    assert boundary.resolutions == boundary.opens == []
    stopped = False

    def cancel_after_read():
        nonlocal stopped
        stopped = True

    response = Response(b'{"message":"partial"}', piece_size=2, on_read=cancel_after_read)
    with pytest.raises(InterruptedError):
        Boundary(response).transport()(request(), cancelled=lambda: stopped)
    assert response.closed and len(response.read_sizes) == 1


def test_deadline_covers_reading_and_is_not_reset_for_each_chunk():
    now = 0.0

    def advance():
        nonlocal now
        now += 2.0

    response = Response(b'{"message":"many chunks"}', piece_size=1, on_read=advance)
    boundary = Boundary(response)
    with pytest.raises(TimeoutError):
        boundary.transport(consent(timeout=1.0), clock=lambda: now)(request())
    assert response.closed and len(response.read_sizes) == 1
    assert 0 < boundary.opens[0][1] <= 1.0


@pytest.mark.parametrize('where', ('dns', 'opener', 'status'))
def test_errors_do_not_expose_credentials_contact_information_or_remote_body(where):
    secret = 'person@example.invalid secret-token-123 https://private.invalid/?key=secret'

    def fail(*args, **kwargs):
        raise OSError(secret)

    boundary = Boundary(Response(secret.encode(), status=403))
    options = dict(resolver=boundary.resolve, opener=boundary.open)
    options['resolver' if where == 'dns' else 'opener'] = fail if where != 'status' else boundary.open
    with pytest.raises((OSError, *DENIED)) as error:
        SafeTransport(consent(), **options)(request())
    assert 'person@' not in str(error.value)
    assert 'secret-token' not in str(error.value)
    assert 'private.invalid' not in str(error.value)
    assert '?key=' not in str(error.value)


def test_opted_in_acquisition_preserves_global_offline_and_telemetry_policy(monkeypatch):
    for key, value in OFFLINE_ENV.items():
        monkeypatch.setenv(key, value)
    before = {key: os.environ[key] for key in OFFLINE_ENV}
    Boundary(Response()).transport()(request())
    assert {key: os.environ[key] for key in OFFLINE_ENV} == before


def test_request_budget_is_shared_across_transfers_and_denies_before_next_dns():
    boundary = Boundary(*(Response() for _ in range(12)))
    transport = boundary.transport()
    for _ in range(12):
        assert transport(request()) == b'{"message": {}}'
    with pytest.raises(ValueError, match='request budget'):
        transport(request())
    assert len(boundary.resolutions) == len(boundary.opens) == 12


@pytest.fixture
def isolated_child(monkeypatch, tmp_path):
    """Substitute only the child command; real parent supervision is exercised."""
    from carr_library.acquisition import transport as module

    fixture = tmp_path / 'synthetic_https_worker.py'
    started = tmp_path / 'child-started'
    fixture.write_text('''
from hashlib import sha256
import json
from pathlib import Path
import signal
import sys
import time

job = json.loads(sys.stdin.buffer.read())
mode = sys.argv[1]
target = Path(job['output'])
payload = b'{"title":"Synthetic record"}' if job['metadata'] else b'%PDF-1.7\\nSynthetic bytes.\\n'
target.write_bytes(payload)
Path(sys.argv[2]).write_text('started')
if mode in ('hang', 'ignore_terminate'):
    if mode == 'ignore_terminate':
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    time.sleep(30)
info = dict(url=job['url'], size=len(payload), sha256=sha256(payload).hexdigest(),
            content_type='application/json' if job['metadata'] else 'application/pdf')
if mode == 'wrong_digest':
    info['sha256'] = '0' * 64
elif mode == 'wrong_size':
    info['size'] += 1
elif mode == 'foreign_url':
    info['url'] = 'https://unapproved.invalid/changed.pdf'
elif mode == 'stderr':
    print('private-email@example.invalid secret-token', file=sys.stderr)
print(json.dumps(dict(ok=True, info=info, requests=job.get('spent_requests', 0) + 1)))
''', encoding='utf-8')
    original_popen = module.subprocess.Popen
    original_temporary = module.TemporaryDirectory
    state = SimpleNamespace(mode='success', processes=[], commands=[], started=started,
                            temporary_root=tmp_path)

    def launch(command, **options):
        state.commands.append(command)
        assert command[:3] == [sys.executable, '-I', '-B']
        assert Path(command[3]).name == 'network_worker.py'
        assert options.get('shell', False) is False and options['close_fds'] is True
        process = original_popen([sys.executable, '-I', '-B', str(fixture),
                                  state.mode, str(started)], **options)
        state.processes.append(process)
        return process

    def temporary(*args, **kwargs):
        kwargs.setdefault('dir', tmp_path)
        return original_temporary(*args, **kwargs)

    monkeypatch.setattr(module.subprocess, 'Popen', launch)
    monkeypatch.setattr(module, 'TemporaryDirectory', temporary)
    yield state
    # A failing assertion must not leave a test child running in the workspace.
    for process in state.processes:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=3)


def assert_child_finished_and_clean(state):
    assert state.processes and all(process.poll() is not None for process in state.processes)
    assert list(state.temporary_root.glob('carr-public-acquisition-*')) == []


@pytest.mark.parametrize('mode', ('hang', 'ignore_terminate'))
def test_real_child_deadline_terminates_blocked_network_work_and_cleans_payload(isolated_child, mode):
    isolated_child.mode = mode
    started = monotonic()
    with pytest.raises(TimeoutError):
        SafeTransport(consent(timeout=1.0))(request())
    assert monotonic() - started < 5.0
    assert isolated_child.started.exists()
    assert_child_finished_and_clean(isolated_child)


def test_real_child_cancellation_stops_a_running_transfer_before_any_output(isolated_child):
    isolated_child.mode = 'hang'
    output = io.BytesIO()
    started = monotonic()
    with pytest.raises(InterruptedError):
        SafeTransport(consent(purpose='download')).download(
            URL, output, cancelled=isolated_child.started.exists)
    assert monotonic() - started < 5.0 and output.getvalue() == b''
    assert_child_finished_and_clean(isolated_child)


@pytest.mark.parametrize('mode', ('wrong_digest', 'wrong_size', 'foreign_url', 'stderr'))
def test_real_child_unverified_payload_is_not_copied_to_download_sink(isolated_child, mode):
    isolated_child.mode = mode
    output = io.BytesIO()
    with pytest.raises((OSError, *DENIED)) as error:
        SafeTransport(consent(purpose='download')).download(URL, output)
    assert output.getvalue() == b''
    assert 'private-email' not in str(error.value) and 'secret-token' not in str(error.value)
    assert_child_finished_and_clean(isolated_child)


def test_real_child_verified_payload_has_exact_digest_and_leaves_no_staging(isolated_child):
    output = io.BytesIO()
    info = SafeTransport(consent(purpose='download')).download(URL, output)
    payload = b'%PDF-1.7\nSynthetic bytes.\n'
    assert output.getvalue() == payload
    assert info.url == URL and info.size == len(payload)
    assert info.sha256 == sha256(payload).hexdigest() and info.content_type == 'application/pdf'
    assert_child_finished_and_clean(isolated_child)


def test_real_child_transfers_preserve_the_cumulative_request_budget(isolated_child):
    transport = SafeTransport(consent(purpose='download'))
    payload = b'%PDF-1.7\nSynthetic bytes.\n'
    for _ in range(12):
        output = io.BytesIO()
        assert transport.download(URL, output).size == len(payload)
        assert output.getvalue() == payload
    output = io.BytesIO()
    with pytest.raises(ValueError, match='request budget'):
        transport.download(URL, output)
    assert output.getvalue() == b''
    assert_child_finished_and_clean(isolated_child)


def test_completion_marker_cannot_publish_mutable_staging_aliases(tmp_path, monkeypatch):
    """Observe the actual completion boundary, then cancel before source import."""
    from carr_library.application import LibraryApp
    from carr_library.acquisition.service import AcquisitionService
    from carr_library.acquisition.transport import DownloadInfo

    doi = '10.5555/completion-order'
    source = 'https://open.example.org/manuscript.pdf'
    location = {'url': source, 'url_for_pdf': source, 'license': 'cc-by',
                'version': 'acceptedVersion', 'host_type': 'repository'}
    response = json.dumps({'doi': doi, 'title': 'Synthetic completion boundary',
        'is_oa': True, 'year': 2026, 'genre': 'journal-article',
        'best_oa_location': location, 'oa_locations': [location]}).encode()
    payload = b'%PDF-1.7\nSynthetic source; import is cancelled after publication.\n'

    class Transfer:
        def __call__(self, provider_request, *, cancelled):
            assert provider_request.provider == 'unpaywall' and not cancelled()
            return response

        def download(self, url, stream, *, cancelled):
            assert url == source and not cancelled()
            stream.write(payload)
            return DownloadInfo(url, sha256(payload).hexdigest(), len(payload),
                                'application/pdf')

    app = LibraryApp(tmp_path / 'private-library')
    workspace = app.create_workspace('Completion boundary')
    app._acquisition_service = AcquisitionService(app,
        transport_factory=lambda permission: Transfer())
    preview = app.lookup_doi(workspace, doi, online_allowed=True,
        providers=('unpaywall',), unpaywall_email='synthetic@example.org')
    target = tmp_path / 'published-document'
    marker = target / '.acquisition-incomplete'
    observed = []
    original_unlink = Path.unlink

    def unlink(path, *args, **kwargs):
        if path == marker:
            # A power loss immediately after this removal must not expose a
            # complete file that another staging filename can still modify.
            assert marker.exists()
            assert (target / 'document.pdf').read_bytes() == payload
            for name in ('document.pdf', 'provenance.json'):
                assert (target / name).stat().st_nlink == 1
                assert not any((stage / name).exists()
                               for stage in tmp_path.glob('.carr-acquire-*'))
            result = original_unlink(path, *args, **kwargs)
            observed.append('complete')
            return result
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'unlink', unlink)
    result = app.acquire_doi_source(workspace, preview, 0, target,
        online_allowed=True, rights_confirmed=True, cancelled=lambda: bool(observed))
    assert observed == ['complete'] and result.state == 'downloaded'
    assert result.edition_id is None and result.reference_id is None
    assert result.path.read_bytes() == payload and not marker.exists()
    receipt = json.loads(result.receipt_path.read_text(encoding='utf-8'))
    assert receipt['sha256'] == sha256(payload).hexdigest()
    assert receipt['bytes'] == len(payload) and receipt['source_text_verified'] is False
    assert receipt['source_url'] == source
    assert not app.browse(workspace)
    assert list(tmp_path.glob('.carr-acquire-*')) == []

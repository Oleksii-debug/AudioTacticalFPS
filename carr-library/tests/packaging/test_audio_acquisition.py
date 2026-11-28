"""Controlled HTTP transport at the real urllib pipeline; no external download."""
from email.message import Message
from hashlib import sha256
from io import BytesIO
import importlib.util
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import urllib.request
import urllib.response

import pytest

from carr_library.audio import jobs

PROJECT = Path(__file__).resolve().parents[2]


class Body(BytesIO):
    def __init__(self, raw, action=None, close_error=False):
        super().__init__(raw)
        self.requests = []
        self.action = action
        self.close_error = close_error

    def read(self, amount=-1):
        self.requests.append(amount)
        value = super().read(amount)
        if self.action is not None:
            self.action(self, value)
        return value

    def close(self):
        super().close()
        if self.close_error:
            self.close_error = False
            raise OSError('Synthetic response-close failure.')


def install_transport(monkeypatch, rows):
    """Use real request/error/redirect handlers, substituting only HTTPS I/O."""
    observed = SimpleNamespace(requests=[], bodies=[])
    def https(_handler, request):
        number = len(observed.requests)
        if number >= len(rows):
            raise AssertionError('An unreviewed additional connection was attempted.')
        row = rows[number]
        observed.requests.append((request.full_url, request.get_method(), dict(request.header_items())))
        headers = Message()
        for key, value in row.get('headers', []):
            headers.add_header(key, value)
        body = Body(row.get('body', b''), row.get('action'), row.get('close_error', False))
        observed.bodies.append(body)
        response = urllib.response.addinfourl(body, headers,
            row.get('url', request.full_url), row.get('status', 200))
        response.msg = 'Synthetic controlled transport'
        return response
    monkeypatch.setattr(urllib.request.HTTPSHandler, 'https_open', https)
    monkeypatch.setattr(urllib.request.HTTPHandler, 'http_open',
        lambda *args, **kwargs: pytest.fail('No insecure connection is authorized.'))
    return observed


@pytest.fixture
def preparer():
    path = PROJECT / 'packaging' / 'prepare_ffmpeg_runtime.py'
    spec = importlib.util.spec_from_file_location('carr_acquisition_test', path)
    module = importlib.util.module_from_spec(spec)
    previous = list(sys.path)
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.path[:] = previous


@pytest.fixture
def selected(tmp_path, monkeypatch, preparer):
    payload = b'Controlled byte identity; not an executable.\x00' * 19
    directory = tmp_path / 'download'
    directory.mkdir()
    monkeypatch.setattr(preparer, 'FFMPEG_ARCHIVE_BYTES', len(payload))
    monkeypatch.setattr(preparer, 'FFMPEG_ARCHIVE_SHA256', sha256(payload).hexdigest())
    monkeypatch.setattr(preparer, '_DOWNLOAD_CHUNK', 31)
    return SimpleNamespace(helper=preparer, payload=payload, directory=directory,
                           destination=directory / preparer.DOWNLOAD_FILENAME)


def test_exact_pinned_bytes_publish_through_real_no_replace_owner(selected, monkeypatch):
    e = selected
    observed = install_transport(monkeypatch, [{'body': e.payload}])
    result = e.helper.acquire_archive(e.directory)
    assert e.destination.read_bytes() == e.payload
    assert e.destination.stat().st_nlink == 1
    assert sorted(path.name for path in e.directory.iterdir()) == [e.helper.DOWNLOAD_FILENAME]
    assert result['archive_sha256'] == sha256(e.payload).hexdigest()
    assert result['archive_bytes'] == len(e.payload) and result['native_verification'] == 'NOT_RUN'
    assert result['archive_url'] == e.helper.FFMPEG_ARCHIVE_URL
    assert result['final_host'] == 'github.com'
    assert len(observed.requests) == 1
    url, method, headers = observed.requests[0]
    assert url == e.helper.FFMPEG_ARCHIVE_URL and method == 'GET'
    assert headers['Accept-encoding'] == 'identity'
    assert all(0 < amount <= 31 for amount in observed.bodies[0].requests)
    assert observed.bodies[0].closed


@pytest.mark.parametrize('status', [301, 302, 303, 307, 308])
def test_approved_https_redirect_is_checked_by_actual_urllib_before_streaming(selected, monkeypatch, status):
    e = selected
    asset = 'https://release-assets.githubusercontent.com/fixed/archive?signature=synthetic'
    observed = install_transport(monkeypatch, [
        {'status': status, 'headers': [('Location', asset)]},
        {'body': e.payload, 'headers': [('Content-Length', str(len(e.payload)))]},
    ])
    result = e.helper.acquire_archive(e.directory)
    assert [row[0] for row in observed.requests] == [e.helper.FFMPEG_ARCHIVE_URL, asset]
    assert all(row[1] == 'GET' for row in observed.requests)
    assert result['final_host'] == 'release-assets.githubusercontent.com'
    assert 'signature' not in str(result)
    assert e.destination.read_bytes() == e.payload
    assert all(body.closed for body in observed.bodies)


@pytest.mark.parametrize('location', [
    'http://github.com/not-https.zip',
    'https://github.com.evil.invalid/archive',
    'https://release-assets.githubusercontent.com.evil.invalid/archive',
    'https://localhost/archive',
    'https://user@github.com/archive',
    'https://github.com:444/archive',
    'https://github.com/archive#fragment',
    'file:///private/archive',
])
def test_denied_redirect_never_opens_second_connection_and_closes_response(selected, monkeypatch, location):
    e = selected
    observed = install_transport(monkeypatch, [{'status': 302, 'headers': [('Location', location)]}])
    with pytest.raises((ValueError, urllib.error.HTTPError)):
        e.helper.acquire_archive(e.directory)
    assert len(observed.requests) == 1 and observed.bodies[0].closed
    assert not tuple(e.directory.iterdir())


@pytest.mark.parametrize('url', [
    'https://github.com/archive\n', 'https://github.com/with space',
    'https://github.com/back\\slash', 'https://github.com/archive\x7f',
    'https://@github.com/archive', 'https://github.com:bad/archive',
])
def test_control_ambiguous_or_credential_urls_fail_before_request_construction(preparer, url):
    with pytest.raises(ValueError):
        preparer._download_url(url)


@pytest.mark.parametrize('kind', ['short', 'extra', 'same-size-wrong'])
def test_body_size_and_digest_are_checked_without_trusting_absent_length(selected, monkeypatch, kind):
    e = selected
    payload = {'short': e.payload[:-1], 'extra': e.payload + b'x',
               'same-size-wrong': bytes([e.payload[0] ^ 1]) + e.payload[1:]}[kind]
    observed = install_transport(monkeypatch, [{'body': payload}])
    with pytest.raises(ValueError):
        e.helper.acquire_archive(e.directory)
    assert observed.bodies[0].closed and not tuple(e.directory.iterdir())


@pytest.mark.parametrize('kind', [
    'partial-status', 'server-error', 'wrong-length', 'duplicate-length',
    'invalid-length', 'encoded-body', 'content-range', 'foreign-final-url',
])
def test_unexpected_response_metadata_is_refused_before_body_read(selected, monkeypatch, kind):
    e = selected
    row = {'body': e.payload}
    if kind == 'partial-status':
        row['status'] = 206
    elif kind == 'server-error':
        row['status'] = 500
    elif kind == 'wrong-length':
        row['headers'] = [('Content-Length', str(len(e.payload) + 1))]
    elif kind == 'duplicate-length':
        row['headers'] = [('Content-Length', str(len(e.payload)))] * 2
    elif kind == 'invalid-length':
        row['headers'] = [('Content-Length', 'NaN')]
    elif kind == 'encoded-body':
        row['headers'] = [('Content-Encoding', 'gzip')]
    elif kind == 'content-range':
        row['headers'] = [('Content-Range', 'bytes 0-10/11')]
    else:
        row['url'] = 'https://foreign.invalid/archive'
    observed = install_transport(monkeypatch, [row])
    with pytest.raises(ValueError):
        e.helper.acquire_archive(e.directory)
    assert not observed.bodies[0].requests and observed.bodies[0].closed
    assert not tuple(e.directory.iterdir())


def test_redirect_loop_stays_bounded_and_removes_owned_partial(selected, monkeypatch):
    e = selected
    location = e.helper.FFMPEG_ARCHIVE_URL
    rows = [{'status': 302, 'headers': [('Location', location)]} for _ in range(8)]
    observed = install_transport(monkeypatch, rows)
    with pytest.raises(ValueError):
        e.helper.acquire_archive(e.directory)
    assert 1 < len(observed.requests) <= 6
    assert all(body.closed for body in observed.bodies)
    assert not tuple(e.directory.iterdir())


def test_existing_even_matching_archive_is_not_adopted_or_downloaded(selected, monkeypatch):
    e = selected
    e.destination.write_bytes(e.payload)
    monkeypatch.setattr(e.helper, '_download_opener', lambda: pytest.fail('Existing destination must refuse first.'))
    with pytest.raises(FileExistsError):
        e.helper.acquire_archive(e.directory)
    assert e.destination.read_bytes() == e.payload
    assert tuple(e.directory.iterdir()) == (e.destination,)


@pytest.mark.parametrize('stop', ['before-open', 'during-read', 'elapsed', 'read-error', 'close-error'])
def test_cancel_timeout_and_transport_failures_settle_response_and_partial(selected, monkeypatch, stop):
    e = selected
    stopped, clock = [stop == 'before-open'], [0.0]
    def observed_read(body, value):
        if value and stop == 'during-read':
            stopped[0] = True
        if value and stop == 'elapsed':
            clock[0] = e.helper.DOWNLOAD_SECONDS + 1
        if value and stop == 'read-error':
            raise TimeoutError('Synthetic transport timeout.')
    monkeypatch.setattr(e.helper.time, 'monotonic', lambda: clock[0])
    observed = install_transport(monkeypatch, [{
        'body': e.payload, 'action': observed_read, 'close_error': stop == 'close-error'}])
    with pytest.raises((InterruptedError, TimeoutError, OSError)):
        e.helper.acquire_archive(e.directory, cancelled=lambda: stopped[0])
    assert len(observed.requests) == (0 if stop == 'before-open' else 1)
    assert all(body.closed for body in observed.bodies)
    assert not tuple(e.directory.iterdir())


def test_destination_created_during_download_is_preserved_by_atomic_publication(selected, monkeypatch):
    e = selected
    original = jobs.publish_new
    def racing(source, destination, **kwargs):
        Path(destination).write_bytes(b'Foreign destination created after review.')
        return original(source, destination, **kwargs)
    monkeypatch.setattr(jobs, 'publish_new', racing)
    install_transport(monkeypatch, [{'body': e.payload}])
    with pytest.raises(FileExistsError):
        e.helper.acquire_archive(e.directory)
    assert e.destination.read_bytes() == b'Foreign destination created after review.'
    assert tuple(e.directory.iterdir()) == (e.destination,)


def test_downloaded_source_is_rehashed_during_copy_before_publication(selected, monkeypatch):
    e = selected
    original = jobs.publish_new
    def changed(source, destination, **kwargs):
        assert kwargs['expected_sha256'] == sha256(e.payload).hexdigest()
        Path(source).write_bytes(bytes([e.payload[0] ^ 1]) + e.payload[1:])
        return original(source, destination, **kwargs)
    monkeypatch.setattr(jobs, 'publish_new', changed)
    install_transport(monkeypatch, [{'body': e.payload}])
    with pytest.raises(ValueError, match='publication digest'):
        e.helper.acquire_archive(e.directory)
    assert not tuple(e.directory.iterdir())


def test_changed_directory_owner_refuses_before_publication(selected, monkeypatch):
    e = selected
    original = e.helper._directory_owner
    calls = []
    def owner(directory):
        value = original(directory)
        calls.append(value)
        return value if len(calls) == 1 else (value[0], value[1] + 1)
    monkeypatch.setattr(e.helper, '_directory_owner', owner)
    monkeypatch.setattr(jobs, 'publish_new', lambda *a, **k: pytest.fail('Changed owner must refuse first.'))
    install_transport(monkeypatch, [{'body': e.payload}])
    with pytest.raises(ValueError, match='directory was replaced'):
        e.helper.acquire_archive(e.directory)
    assert len(calls) == 2 and not tuple(e.directory.iterdir())


def test_partial_cleanup_does_not_remove_a_replacement_foreign_file(selected, monkeypatch):
    e = selected
    held = e.directory / 'retained-original'
    foreign = []
    def replacement(source, destination, **kwargs):
        source = Path(source)
        source.rename(held)
        source.write_bytes(b'Foreign replacement at an old temporary name.')
        foreign.append(source)
        raise OSError('Synthetic ownership change before publication.')
    monkeypatch.setattr(jobs, 'publish_new', replacement)
    install_transport(monkeypatch, [{'body': e.payload}])
    with pytest.raises(OSError, match='ownership change'):
        e.helper.acquire_archive(e.directory)
    assert held.read_bytes() == e.payload
    assert foreign[0].read_bytes() == b'Foreign replacement at an old temporary name.'
    assert not e.destination.exists()


@pytest.mark.parametrize('kind', ['relative', 'parent-traversal', 'not-directory'])
def test_download_directory_admission_precedes_any_network(selected, monkeypatch, kind):
    e = selected
    values = {'relative': Path('relative'), 'parent-traversal': e.directory / '..' / 'download',
              'not-directory': e.directory / 'file'}
    values['not-directory'].write_bytes(b'Keep this file.')
    monkeypatch.setattr(e.helper, '_download_opener', lambda: pytest.fail('No path admission, no connection.'))
    with pytest.raises((ValueError, OSError)):
        e.helper.acquire_archive(values[kind])
    assert values['not-directory'].read_bytes() == b'Keep this file.'

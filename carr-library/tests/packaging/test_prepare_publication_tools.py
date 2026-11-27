"""Pinned CI-tool acquisition: actual cache writes, bounded bytes and no overwrite."""
from hashlib import sha256
import importlib.util
import io
from pathlib import Path
import urllib.request
from zipfile import ZipFile

import pytest


_spec = importlib.util.spec_from_file_location('prepare_publication_tools',
    Path(__file__).resolve().parents[2] / 'scripts' / 'prepare_publication_tools.py')
prep = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(prep)


def record(data):
    return {'url': 'https://www.w3.org/TR/xhtml1/DTD/xhtml-special.ent',
            'size': len(data), 'sha256': sha256(data).hexdigest()}


class Response(io.BytesIO):
    def __init__(self, content, url='https://www.w3.org/TR/xhtml1/DTD/xhtml-special.ent'):
        super().__init__(content)
        self.url = url


class Opener:
    def __init__(self, response):
        self.response, self.calls = response, 0
    def open(self, request, timeout):
        self.calls += 1
        assert timeout == 30 and request.full_url.startswith('https://')
        return self.response


def test_exact_cached_bytes_are_reused_offline_without_an_open(tmp_path):
    data = b'Exact selected conformance bytes'
    target = tmp_path / 'cached.dtd'; target.write_bytes(data)
    opener = Opener(Response(b'unrequested'))
    assert prep.cached(target, record(data), opener=opener) == target
    assert opener.calls == 0 and target.read_bytes() == data
    with pytest.raises(FileNotFoundError, match='Missing offline'):
        prep.cached(tmp_path / 'missing.dtd', record(data), opener=opener)
    assert opener.calls == 0


def test_actual_validated_download_publishes_once_and_closes_response(tmp_path):
    data = b'Exact downloaded schema'
    target = tmp_path / 'schema.dtd'
    response = Response(data); opener = Opener(response)
    assert prep.cached(target, record(data), download=True, opener=opener) == target
    assert target.read_bytes() == data and response.closed and opener.calls == 1
    assert not list(tmp_path.glob('.publication-download-*'))
    assert target.stat().st_nlink == 1


@pytest.mark.parametrize('data,expected', [(b'excess bytes', b'excess'), (b'edited', b'pinned'), (b'short', b'longer')])
def test_mismatched_or_excess_response_is_not_published(tmp_path, data, expected):
    response = Response(data)
    target = tmp_path / 'invalid.dtd'
    with pytest.raises(ValueError):
        prep.cached(target, record(expected), download=True, opener=Opener(response))
    assert response.closed and not target.exists()
    assert not list(tmp_path.glob('.publication-download-*'))


def test_concurrent_destination_keeps_its_bytes(tmp_path, monkeypatch):
    data = b'Validated incoming bytes'
    target = tmp_path / 'owned.dtd'
    original = prep.os.link
    def concurrent(source, destination):
        Path(destination).write_bytes(b'Concurrent owner bytes')
        return original(source, destination)
    monkeypatch.setattr(prep.os, 'link', concurrent)
    with pytest.raises(ValueError):
        prep.cached(target, record(data), download=True, opener=Opener(Response(data)))
    assert target.read_bytes() == b'Concurrent owner bytes'
    assert not list(tmp_path.glob('.publication-download-*'))


def test_corrupt_cached_file_never_triggers_overwrite_or_network(tmp_path):
    target = tmp_path / 'existing.dtd'; target.write_bytes(b'Existing unrelated bytes')
    opener = Opener(Response(b'pinned'))
    with pytest.raises(ValueError):
        prep.cached(target, record(b'pinned'), download=True, opener=opener)
    assert opener.calls == 0 and target.read_bytes() == b'Existing unrelated bytes'


@pytest.mark.parametrize('url', ['http://www.w3.org/untrusted', 'https://example.invalid/payload',
                                 'https://username:password@github.com/payload'])
def test_redirect_policy_rejects_new_unapproved_authority(url):
    request = urllib.request.Request('https://www.w3.org/TR/xhtml1/DTD/xhtml-special.ent')
    with pytest.raises(ValueError, match='approved HTTPS'):
        prep.ApprovedRedirects().redirect_request(request, io.BytesIO(), 302, 'Found', {}, url)


def test_reported_final_url_is_checked_before_any_payload_write(tmp_path):
    response = Response(b'pinned', url='https://example.invalid/unapproved')
    target = tmp_path / 'redirected.dtd'
    with pytest.raises(ValueError, match='approved HTTPS'):
        prep.cached(target, record(b'pinned'), download=True, opener=Opener(response))
    assert response.closed and not target.exists()


def test_license_notices_are_read_and_hashed_without_extracting(tmp_path):
    archive = tmp_path / 'validator.zip'
    notices = ['validator/LICENSE.txt', 'validator/THIRD-PARTY.txt']
    with ZipFile(archive, 'w') as output:
        for name in notices: output.writestr(name, b'Synthetic license notice')
    proof = prep.notice_receipts(archive, {'notices': notices})
    assert set(proof) == set(notices)
    assert all(value == {'size': 24, 'sha256': sha256(b'Synthetic license notice').hexdigest()}
               for value in proof.values())
    assert not (tmp_path / 'validator').exists()

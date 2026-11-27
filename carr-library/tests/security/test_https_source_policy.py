"""Pure policy checks; no network or publisher authority is activated."""
from hashlib import sha256

import pytest

from carr_library.security._files import TrustError
from carr_library.security.https import HTTPSRepository, HTTPSRepositoryClient


@pytest.mark.parametrize('url', [
    'http://example.invalid/repository/', 'https://user:secret@example.invalid/',
    'https://example.invalid/repository/?token=secret', 'https://example.invalid/#fragment',
    'https://example.invalid/repository', 'https://example.invalid/a/../',
    'https://example.invalid/%2e%2e/', 'https://example.invalid//repository/',
    'https://example.invalid/repository/\\other/', 'https://example.invalid:0/',
    'https://example.invalid:65536/', 'https://example.invalid./',
])
def test_source_requires_one_explicit_unambiguous_https_directory(url):
    with pytest.raises(TrustError):
        HTTPSRepository(url)


@pytest.mark.parametrize('suffix', [
    '../targets/file.zip', 'metadata/delegated.json', 'metadata/../targets/a.zip',
    'metadata/%74argets.json', 'targets/a%252fb.zip', 'targets/a.zip?secret=1',
    'targets/a.zip#fragment', 'targets//a.zip', 'targets/%2Felsewhere',
])
def test_fetcher_cannot_expand_reviewed_origin_or_roles(suffix):
    source = HTTPSRepository('https://example.invalid/approved/')
    with pytest.raises((TrustError, ValueError)):
        source.object_limit(source.url + suffix, 1024)


def test_role_limits_are_independent_of_signed_target_length():
    source = HTTPSRepository('https://example.invalid/approved/')
    assert source.object_limit(source.url + 'metadata/2.root.json', 16 * 1024**3) == 512000
    assert source.object_limit(source.url + 'metadata/timestamp.json', 16 * 1024**3) == 16384
    assert source.object_limit(source.url + 'metadata/3.snapshot.json', 16 * 1024**3) == 2_000_000
    assert source.object_limit(source.url + 'metadata/3.targets.json', 16 * 1024**3) == 5_000_000
    assert source.object_limit(source.url + 'targets/hash.release.zip', 1024) == 1024
    for url in ('https://elsewhere.invalid/approved/metadata/timestamp.json',
                'https://example.invalid/other/metadata/timestamp.json'):
        with pytest.raises(TrustError):
            source.object_limit(url, 1024)


@pytest.mark.parametrize('timeout', [0, -.1, .001, 1801, float('nan'), float('inf'), True, '60'])
def test_total_network_deadline_is_finite(timeout):
    with pytest.raises(TrustError):
        HTTPSRepository('https://example.invalid/', timeout_seconds=timeout)


def test_optional_tls_authority_needs_its_own_independent_pin():
    raw = b'explicit test CA bytes'
    with pytest.raises(TrustError):
        HTTPSRepository('https://example.invalid/', ca_pem=raw)
    with pytest.raises(TrustError):
        HTTPSRepository('https://example.invalid/', ca_pem=raw, ca_sha256='0' * 64)
    source = HTTPSRepository('https://example.invalid/', ca_pem=raw,
                             ca_sha256=sha256(raw).hexdigest())
    assert source.ca_pem == raw
    assert raw.decode() not in repr(source)


def test_constructing_client_never_contacts_a_repository(repository, tmp_path, monkeypatch):
    import socket
    def forbidden(*args, **kwargs):
        raise AssertionError('No background network connection is authorized.')
    monkeypatch.setattr(socket, 'getaddrinfo', forbidden)
    source = HTTPSRepository('https://example.invalid/approved/')
    client = HTTPSRepositoryClient(tmp_path / 'cache', repository.policy(), source)
    assert client.repository is source
    assert not (client.cache / 'https-source.json').exists()

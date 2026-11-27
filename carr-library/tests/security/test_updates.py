from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os

import pytest
from securesystemslib.signer import CryptoSigner
from tuf.api.exceptions import RepositoryError, DownloadError

from carr_library.security._files import TrustError
from carr_library.security.updates import LocalRepositoryClient, UpdateCancelled, _local_fetcher
from carr_library.storage.revisions import SCHEMAS
from .conftest import Repository, bundle


def test_real_tuf_metadata_and_download_are_bound_to_independent_root_and_exact_review(repository, tmp_path):
    package_bytes = bundle()
    target = repository.publish(package_bytes)
    client = LocalRepositoryClient(tmp_path / 'cache', repository.policy())
    review = client.check(repository.root, repository.target_path)
    assert review.sha256 == target.hashes['sha256']
    assert review.build == 2 and review.source_commit == '2' * 40
    assert review.data_schema == max(SCHEMAS)
    with pytest.raises(TrustError, match='changed after review'):
        client.download(repository.root, repository.target_path, reviewed_sha256='0' * 64)
    downloaded = client.download(repository.root, repository.target_path, reviewed_sha256=review.sha256)
    assert downloaded.path.read_bytes() == package_bytes
    assert downloaded.path.stat().st_nlink == 1
    assert client.download(repository.root, repository.target_path, reviewed_sha256=review.sha256) == downloaded


@pytest.mark.parametrize('custom', [
    {'product': 'other'}, {'channel': 'preview'}, {'build': 1},
    {'data_schema': max(SCHEMAS) + 1}, {'source_commit': 'unbound'}, {'schema': 2},
    {'extra_command': 'run-this'}, {'version': '../other'},
])
def test_signed_wrong_product_channel_schema_or_downgrade_is_refused(repository, tmp_path, custom):
    repository.publish(bundle(), custom=custom)
    client = LocalRepositoryClient(tmp_path / 'cache', repository.policy())
    with pytest.raises(TrustError):
        client.check(repository.root, repository.target_path)


def test_unknown_publisher_cannot_replace_bootstrap_even_with_matching_cache_name(repository, tmp_path):
    repository.publish(bundle())
    alien = Repository(tmp_path / 'alien')
    alien.publish(bundle())
    client = LocalRepositoryClient(tmp_path / 'cache', repository.policy())
    (client.metadata / 'root.json').write_bytes(alien.bootstrap)
    with pytest.raises((RepositoryError, DownloadError)):
        client.check(alien.root, alien.target_path)
    assert client.check(repository.root, repository.target_path).build == 2


def test_tuf_rejects_expired_and_rolled_back_timestamp(repository, tmp_path):
    repository.publish(bundle())
    client = LocalRepositoryClient(tmp_path / 'cache', repository.policy())
    original = (repository.root / 'metadata' / 'timestamp.json').read_bytes()
    client.check(repository.root, repository.target_path)
    repository.advance()
    repository.publish(bundle(), build=3)
    assert client.check(repository.root, repository.target_path).build == 3
    (repository.root / 'metadata' / 'timestamp.json').write_bytes(original)
    with pytest.raises(RepositoryError):
        client.check(repository.root, repository.target_path)
    repository.md['timestamp'].signed.expires = datetime.now(timezone.utc) - timedelta(seconds=1)
    repository.md['timestamp'].signed.version += 1
    repository.flush()
    with pytest.raises(RepositoryError):
        client.check(repository.root, repository.target_path)


def test_tuf_rejects_mixed_metadata_and_tampered_target(repository, tmp_path):
    target = repository.publish(bundle())
    client = LocalRepositoryClient(tmp_path / 'cache', repository.policy())
    review = client.check(repository.root, repository.target_path)
    path = repository.root / 'targets' / (target.hashes['sha256'] + '.' + repository.target_path)
    path.write_bytes(b'x' * target.length)
    with pytest.raises(RepositoryError):
        client.download(repository.root, repository.target_path, reviewed_sha256=review.sha256)
    assert list(client.downloads.iterdir()) == []
    repository.advance()
    repository.publish(bundle(), build=3)
    (repository.root / 'metadata' / '2.snapshot.json').write_bytes(b'{}')
    with pytest.raises(RepositoryError):
        client.check(repository.root, repository.target_path)


@pytest.mark.parametrize('dual_signed', [True, False])
def test_tuf_root_rotation_requires_previous_and_new_authority(repository, tmp_path, dual_signed):
    repository.publish(bundle())
    client = LocalRepositoryClient(tmp_path / 'cache', repository.policy())
    client.check(repository.root, repository.target_path)
    old = repository.signers['root']
    new = CryptoSigner.generate_ed25519()
    root = repository.md['root'].signed
    root.roles['root'].keyids.clear()
    root.add_key(new.public_key, 'root')
    root.version += 1
    repository.signers['root'] = new
    repository.publish_root(old if dual_signed else None)
    if dual_signed:
        assert client.check(repository.root, repository.target_path).build == 2
    else:
        with pytest.raises(RepositoryError):
            client.check(repository.root, repository.target_path)


@pytest.mark.parametrize('url', [
    'https://example.invalid/targets/release.zip', 'file://host/metadata/root.json',
    'file:///targets/../../outside', 'file:///targets/%2e%2e/outside',
    'file:///targets/a.zip?redirect=http://invalid', 'file:///targets/a.zip#fragment',
])
def test_local_fetcher_has_no_network_or_redirect_fallback(repository, url):
    fetcher = _local_fetcher(repository.root, lambda: False)
    with pytest.raises((TrustError, DownloadError)):
        list(fetcher.fetch(url))


def test_cancelled_download_leaves_no_published_package(repository, tmp_path):
    repository.publish(bundle())
    client = LocalRepositoryClient(tmp_path / 'cache', repository.policy())
    review = client.check(repository.root, repository.target_path)
    with pytest.raises((UpdateCancelled, DownloadError)):
        client.download(repository.root, repository.target_path, reviewed_sha256=review.sha256, cancelled=lambda: True)
    assert list(client.downloads.iterdir()) == []


def test_linked_repository_target_rejected(repository, tmp_path):
    target = repository.publish(bundle())
    path = repository.root / 'targets' / (target.hashes['sha256'] + '.' + repository.target_path)
    os.link(path, tmp_path / 'alias.zip')
    client = LocalRepositoryClient(tmp_path / 'cache', repository.policy())
    review = client.check(repository.root, repository.target_path)
    with pytest.raises((TrustError, DownloadError)):
        client.download(repository.root, repository.target_path, reviewed_sha256=review.sha256)


def test_fetcher_resource_cap_is_independent_of_signed_length(repository):
    (repository.root / 'targets' / 'large.zip').write_bytes(b'123')
    fetcher = _local_fetcher(repository.root, lambda: False, max_target_bytes=2)
    with pytest.raises((TrustError, DownloadError), match='resource budget'):
        list(fetcher.fetch('file:///targets/large.zip'))

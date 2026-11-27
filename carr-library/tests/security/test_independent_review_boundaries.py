"""Independent synthetic trust-boundary probes; no installed code is executed."""
from hashlib import sha256
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import os
import threading
import time

import pytest

from carr_library.security import installation
from carr_library.security._files import TrustError
from carr_library.security.entitlements import EntitlementStore
from carr_library.security.refresh import RefreshWorker
from carr_library.security.updates import LocalRepositoryClient, Release
from .conftest import MemoryAnchor, REFERENCE, RenewalProvider, accept, bundle, envelope, grant


def test_arbitrary_path_and_checksum_cannot_enter_unpack_without_tuf_proof(repository, tmp_path):
    raw = bundle()
    source = tmp_path / 'unverified.zip'
    source.write_bytes(raw)
    release = Release('carr-test', 'stable', '0.2.0', 2, '2' * 40, 4,
                      'release.zip', len(raw), sha256(raw).hexdigest(), '')
    forged = SimpleNamespace(release=release, path=source)
    with pytest.raises(TrustError):
        installation.unpack(forged, tmp_path / 'stage', repository.policy())
    assert not (tmp_path / 'stage').exists()


def test_replacement_after_package_verification_cannot_supply_extracted_bytes(repository, tmp_path, monkeypatch):
    repository.publish(bundle('B'))
    client = LocalRepositoryClient(tmp_path / 'cache', repository.policy())
    review = client.check(repository.root, repository.target_path)
    package = client.download(repository.root, repository.target_path, reviewed_sha256=review.sha256)
    original_zip = installation.zipfile.ZipFile
    replacement = tmp_path / 'replacement.zip'
    replacement.write_bytes(bundle('C'))
    swapped = []

    def replace_before_extraction(path_or_file, *args, **kwargs):
        os.replace(replacement, package.path)
        swapped.append(True)
        return original_zip(path_or_file, *args, **kwargs)

    monkeypatch.setattr(installation.zipfile, 'ZipFile', replace_before_extraction)
    stage = tmp_path / 'stage'
    try:
        installation.unpack(package, stage, repository.policy())
    except TrustError:
        return
    assert swapped == [True]
    assert (stage / 'version.txt').read_bytes() == b'B'


def test_verified_package_proof_cannot_be_copied_to_different_release_bytes(repository, tmp_path):
    repository.publish(bundle('B'))
    client = LocalRepositoryClient(tmp_path / 'cache', repository.policy())
    review = client.check(repository.root, repository.target_path)
    package = client.download(repository.root, repository.target_path, reviewed_sha256=review.sha256)
    raw = bundle('C')
    alternate = tmp_path / 'unsigned.zip'
    alternate.write_bytes(raw)
    with pytest.raises(TrustError):
        rebound = replace(package, path=alternate,
                          release=replace(package.release, length=len(raw), sha256=sha256(raw).hexdigest()))
        installation.unpack(rebound, tmp_path / 'stage', repository.policy())
    assert not (tmp_path / 'stage').exists()


@pytest.mark.parametrize('stop', ['close', 'deadline'])
def test_renewal_cannot_advance_anchor_after_stop_during_local_acceptance(
        tmp_path, signer, policy, clock, stop):
    entered, release = threading.Event(), threading.Event()

    class BarrierAnchor(MemoryAnchor):
        block = False

        def load(self):
            if self.block and threading.current_thread().name == 'carr-entitlement-refresh':
                entered.set()
                assert release.wait(2)
            return super().load()

    anchor = BarrierAnchor()
    store = EntitlementStore(tmp_path / 'client', policy, anchor, clock=clock)
    original = envelope(signer, grant(clock))
    accept(store, original)

    class Provider(RenewalProvider):
        def renew(self, request, cancelled):
            return envelope(signer, grant(clock, sequence=2, request_nonce=request.nonce))

    worker = RefreshWorker(store, Provider(), REFERENCE, profile_id='trust-test',
                           clock=clock, timeout_seconds=.05 if stop == 'deadline' else 20)
    anchor.block = True
    try:
        assert worker.tick(manual=True)
        assert entered.wait(1)
        if stop == 'close':
            worker.close(0)
        else:
            time.sleep(.07)
    finally:
        release.set()
        if worker._thread is not None:
            worker._thread.join(2)
        worker.close()
    assert not worker.running
    assert anchor.load().sequence == 1
    assert store.current_path.read_bytes() == original

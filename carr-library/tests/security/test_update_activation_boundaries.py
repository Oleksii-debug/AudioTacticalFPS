"""Reuse exact update-boundary probes from reviewed candidate 8c981c7e.

These tests complement the unchanged current entitlement/native/polling suites.
Source files: test_publication_integrity.py and test_independent_review_boundaries.py.
Synthetic signed packages do not establish a Windows installation or owner authority.
"""
from dataclasses import replace
from hashlib import sha256
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import threading
import zipfile

import pytest

from carr_library.security import installation
from carr_library.security._files import TrustError, json_bytes
from carr_library.security.updates import LocalRepositoryClient, Release
from .conftest import bundle, installer_verify


def downloaded(repository, tmp_path):
    repository.publish(bundle('B'))
    client = LocalRepositoryClient(tmp_path / 'cache', repository.policy())
    review = client.check(repository.root, repository.target_path)
    return client.download(repository.root, repository.target_path, reviewed_sha256=review.sha256)


def test_in_place_rewrite_after_snapshot_cannot_replace_extracted_bytes(repository, tmp_path, monkeypatch):
    package = downloaded(repository, tmp_path)
    original_zip = zipfile.ZipFile
    replacement = bundle('C')
    rewritten = []

    def rewrite_before_extraction(stream, *args, **kwargs):
        package.path.write_bytes(replacement)
        rewritten.append(True)
        return original_zip(stream, *args, **kwargs)

    monkeypatch.setattr(installation.zipfile, 'ZipFile', rewrite_before_extraction)
    stage = installation.unpack(package, tmp_path / 'stage', repository.policy())
    assert rewritten == [True]
    assert package.path.read_bytes() == replacement
    assert (stage / 'version.txt').read_bytes() == b'B'


def test_consuming_a_verified_package_still_applies_current_package_budget(repository, tmp_path):
    package = downloaded(repository, tmp_path)
    original = package.path.read_bytes()
    with pytest.raises(TrustError, match='download budget'):
        installation.unpack(package, tmp_path / 'stage',
                            repository.policy(max_package_bytes=len(original) - 1))
    assert not (tmp_path / 'stage').exists()
    assert package.path.read_bytes() == original


def release_fixture(tmp_path, *, receipt=False):
    root = tmp_path / 'program'
    with zipfile.ZipFile(io.BytesIO(bundle())) as archive:
        archive.extractall(root)
    manifest = json.loads((root / 'bundle-manifest.json').read_bytes())
    release = Release('carr-test', 'stable', '0.2.0', 2, '2' * 40, 4,
                      'release.zip', 1, 'a' * 64, '')
    manifest['source_commit'] = release.source_commit
    if receipt:
        wheel = 'wheelhouse/universal_research_library-0.2.0-py3-none-any.whl'
        (root / 'wheelhouse/synthetic.whl').rename(root / wheel)
        digest = manifest['files'].pop('wheelhouse/synthetic.whl')
        manifest['files'][wheel] = digest
        proof = {'source_commit': release.source_commit, 'source_tree': '3' * 40,
                 'project_name': 'universal-research-library', 'project_version': release.version,
                 'wheel_sha256': digest, 'payload_files': 1, 'required_runtime_files': 1}
        save_proof(root, manifest, proof)
    (root / 'bundle-manifest.json').write_bytes(json_bytes(manifest))
    return root, release, manifest


def save_proof(root, manifest, proof):
    raw = json_bytes(proof)
    (root / 'application-source.json').write_bytes(raw)
    manifest['files']['application-source.json'] = sha256(raw).hexdigest()


@pytest.mark.parametrize('receipt', [False, True])
def test_real_bundle_verifier_and_signed_release_share_exact_source_identity(tmp_path, receipt):
    root, release, _ = release_fixture(tmp_path, receipt=receipt)
    installation.WindowsBundleAdapter(installer_verify())._verify_release(root, release)


@pytest.mark.parametrize(('field', 'value'), [
    ('source_commit', '9' * 40), ('project_version', '0.1.0'),
    ('project_name', 'other-product'), ('wheel_sha256', 'f' * 64),
])
def test_inventoried_source_receipt_cannot_disagree_with_signed_release(tmp_path, field, value):
    root, release, manifest = release_fixture(tmp_path, receipt=True)
    proof = json.loads((root / 'application-source.json').read_bytes())
    proof[field] = value
    save_proof(root, manifest, proof)
    (root / 'bundle-manifest.json').write_bytes(json_bytes(manifest))
    # Inventory itself is internally consistent: the cross-binding must reject.
    installer_verify()(root)
    with pytest.raises(TrustError, match='source receipt differs'):
        installation.WindowsBundleAdapter(installer_verify())._verify_release(root, release)


def test_health_refuses_another_source_commit_before_starting_installed_python(tmp_path, monkeypatch):
    root, release, manifest = release_fixture(tmp_path)
    manifest['source_commit'] = '9' * 40
    (root / 'bundle-manifest.json').write_bytes(json_bytes(manifest))
    python = root / '.venv/Scripts/python.exe'
    python.parent.mkdir(parents=True)
    python.write_bytes(b'Synthetic sentinel: never executed.')
    calls = []
    monkeypatch.setattr(installation.WindowsBundleAdapter, '_run_owned', lambda *args, **kwargs: calls.append(args))
    with pytest.raises(TrustError, match='source commit differs'):
        installation.WindowsBundleAdapter(installer_verify()).health(root, release)
    assert calls == []


def test_source_receipt_cannot_be_added_outside_verified_inventory(tmp_path):
    root, release, manifest = release_fixture(tmp_path, receipt=True)
    del manifest['files']['application-source.json']
    (root / 'bundle-manifest.json').write_bytes(json_bytes(manifest))
    with pytest.raises(TrustError, match='outside the verified inventory'):
        installation.WindowsBundleAdapter(installer_verify())._verify_release(root, release)


def test_program_only_updater_refuses_managed_installer_ownership_before_verifying_or_launching(tmp_path):
    root = tmp_path / 'managed'
    program = root / 'app'
    program.mkdir(parents=True)
    marker = root / 'managed-install.ini'
    marker.write_bytes(b'Existing standard installation identity')
    calls = []
    adapter = installation.WindowsBundleAdapter(lambda *a, **k: calls.append(a))
    with pytest.raises(TrustError, match='ownership receipts'):
        adapter.verify(program)
    assert calls == [] and marker.read_bytes() == b'Existing standard installation identity'


def test_update_native_runner_uses_the_shared_owned_tree_and_fixed_diagnostic_budget(tmp_path, monkeypatch):
    from carr_library.audio import process as capture_process
    calls = []
    def run(arguments, **options):
        calls.append((arguments, options))
        return capture_process.ProcessResult(b'', b'', .01, 0)
    monkeypatch.setattr(capture_process, 'run_process', run)
    monkeypatch.setenv('PYTHONPATH', 'untrusted')
    monkeypatch.setenv('PIP_INDEX_URL', 'https://untrusted.invalid')
    adapter = installation.WindowsBundleAdapter(lambda root: None)
    assert adapter._run_owned([tmp_path / 'python.exe', '-I', '--self-test'], root=tmp_path, timeout=9) == 0
    arguments, options = calls[0]
    assert arguments[1:] == ['-I', '--self-test']
    assert options['owned_tree'] is True and options['allow_failure'] is True
    assert options['budget'].timeout == 9 and options['budget'].max_log_bytes == 4 * 1024**2
    assert 'PYTHONPATH' not in options['env'] and 'PIP_INDEX_URL' not in options['env']
    assert options['env']['PIP_NO_INDEX'] == '1'


def test_update_cannot_return_to_rollback_while_native_cleanup_is_unconfirmed(tmp_path, monkeypatch):
    from carr_library.audio import process as capture_process
    from carr_library.audio.models import AudioCleanupPending
    entered, quiescent = threading.Event(), threading.Event()
    class Owner:
        quiescent_event = quiescent
    def run(*args, **kwargs):
        entered.set()
        raise AudioCleanupPending('Private native details must not escape', cleanup_owner=Owner())
    monkeypatch.setattr(capture_process, 'run_process', run)
    adapter = installation.WindowsBundleAdapter(lambda root: None)
    outcomes = []
    def work():
        try:
            adapter._run_owned([tmp_path / 'python.exe'], root=tmp_path, timeout=1)
        except Exception as error:
            outcomes.append(error)
    thread = threading.Thread(target=work)
    thread.start()
    try:
        assert entered.wait(2)
        assert thread.is_alive() and outcomes == []
    finally:
        quiescent.set()
        thread.join(2)
    assert not thread.is_alive() and len(outcomes) == 1
    assert isinstance(outcomes[0], TrustError)
    assert 'Private native' not in str(outcomes[0])


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


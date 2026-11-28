"""Real current core/child boundaries over a labelled synthetic owner API."""
from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
import runpy

import pytest

from carr_library.audio import voice_packages, voice_transport
from carr_library.audio.models import AudioCancelled
from carr_library.audio.voice_manifest import LocalModelManifest
from carr_library.audio.voice_packages import managed_catalog, approved_profile
from carr_library.operations import ResourceBroker

_fixture = runpy.run_path(str(Path(__file__).with_name('voice_management_fixtures.py')))
installed = _fixture['installed']
lifecycle_ready = _fixture['lifecycle_ready']
package = _fixture['package']
configure = _fixture['configure']
write_json = _fixture['write_json']


def assert_released(library):
    assert ResourceBroker.for_app(library).snapshot()['leases'] == []


def test_registration_adds_only_owner_lock_and_exact_current_binding(installed):
    library, selected, manager, calls = installed
    manifest = LocalModelManifest.load(selected)
    proof = manifest.verify(require_complete=True)
    value = manager.store.read()
    binding = value['runtime_configuration']['management']
    assert value['revision'] == 1 and binding['sha256'] == sha256(selected.read_bytes()).hexdigest()
    assert binding['ownership']['runtime']['path'] == str(selected.parent)
    assert binding['ownership']['lock']['path'] == str(selected.parent / 'models/voices/packages/.voice-packages.lock')
    assert sorted(path.name for path in (selected.parent / 'models/voices/packages').iterdir()) == ['.voice-packages.lock']
    assert proof['files'] == len(manifest.files) and len(calls) == 1
    assert_released(library)


@pytest.mark.parametrize('permission', [False, None, 1], ids=['unchecked', 'null', 'integer'])
def test_install_requires_exact_rights_before_owner_mutation(installed, tmp_path, permission):
    library, selected, manager, calls = installed
    configure(manager)
    archive = tmp_path / 'reference.zip'
    package(archive)
    review = manager.inspect(archive)
    previous, count = manager.store.read(), len(calls)
    with pytest.raises(ValueError, match='permission'):
        manager.apply(review, rights_approved=permission, supported_languages=['uk'])
    assert manager.store.read() == previous and len(calls) == count
    assert not (selected.parent / 'models/voices/packages/voice-packages.json').exists()
    assert_released(library)


def test_actual_synthetic_owner_install_refreshes_complete_inventory_and_rights(installed, tmp_path):
    library, selected, manager, calls = installed
    configure(manager)
    archive = tmp_path / 'reference.zip'
    document = package(archive)
    review = manager.inspect(archive)
    previous, count = manager.store.read(), len(calls)
    result = manager.apply(review, rights_approved=True, supported_languages=['uk'])
    assert result == manager.store.read() and result['revision'] == previous['revision'] + 1
    manifest = LocalModelManifest.load(selected)
    manifest.verify(require_complete=True)
    assert approved_profile(manifest, document) is True
    assert manifest.settings['profile_languages']['fixture'] == {'supported': ['uk'], 'candidates': ['sk']}
    assert manifest.settings['profile_reviews']['fixture']['references'] == {
        row['role']: row['sha256'] for row in document['files'].values()}
    assert len(calls) == count + 1
    assert manager.versions('fixture') == (document,)
    assert_released(library)


def test_actual_version_activation_keeps_both_immutable_versions_and_rebinds_review(lifecycle_ready):
    library, selected, manager, lifecycle, calls = lifecycle_ready
    before = manager.store.read()
    review = manager.review_version('fixture', '1.0.0')
    old_paths = {path.relative_to(selected.parent).as_posix(): path.read_bytes()
                 for path in (selected.parent / 'models/voices/packages/fixture').rglob('*') if path.is_file()}
    result = manager.apply(review, rights_approved=True, supported_languages=['uk'])
    manifest = LocalModelManifest.load(selected)
    manifest.verify(require_complete=True)
    assert result['revision'] == before['revision'] + 1
    assert manifest.settings['profile_reviews']['fixture']['version'] == '1.0.0'
    assert {row['version'] for row in manager.versions('fixture')} == {'1.0.0', '2.0.0'}
    assert {name: (selected.parent / name).read_bytes() for name in old_paths} == old_paths
    assert json.loads((selected.parent / 'models/voices/packages/voice-packages.json').read_bytes())['active'] == {'fixture': '1.0.0'}
    assert_released(library)


@pytest.mark.parametrize('collision', ['catalog', 'reference-basename'])
def test_first_managed_install_cannot_retarget_a_legacy_identity(installed, tmp_path, collision):
    library, selected, manager, calls = installed
    identity = 'legacy'
    if collision == 'reference-basename':
        identity = 'separate-legacy'
        legacy = selected.parent / 'models/voices/separate-legacy.wav'
        legacy.write_bytes(b'untouched existing synthetic legacy reference')
        doc = json.loads(selected.read_bytes())
        doc['files'][legacy.relative_to(selected.parent).as_posix()] = sha256(legacy.read_bytes()).hexdigest()
        write_json(selected, doc)
        manager.register_manifest(selected, expected_revision=manager.store.read()['revision'])
    configure(manager)
    archive = tmp_path / 'collision.zip'
    package(archive, identity=identity)
    review = manager.inspect(archive)
    old = manager.store.path.read_bytes()
    with pytest.raises(RuntimeError, match='owner package API failed'):
        manager.apply(review, rights_approved=True, supported_languages=['uk'])
    assert manager.store.path.read_bytes() == old
    assert not (selected.parent / 'models/voices/packages' / identity).exists()
    assert not (selected.parent / 'models/voices/packages/voice-packages.json').exists()
    if collision == 'reference-basename':
        assert legacy.read_bytes() == b'untouched existing synthetic legacy reference'
    assert_released(library)


@pytest.mark.parametrize('language', ['en', 'xx', 'uk-UA'])
def test_review_never_promotes_an_undeclared_production_language(installed, tmp_path, language):
    library, selected, manager, calls = installed
    configure(manager)
    archive = tmp_path / 'input.zip'
    package(archive)
    review = manager.inspect(archive)
    count = len(calls)
    with pytest.raises(ValueError, match='subset'):
        manager.apply(review, rights_approved=True, supported_languages=[language])
    assert len(calls) == count
    assert_released(library)


def test_changed_archive_is_refused_before_install_and_preserves_settings(installed, tmp_path):
    library, selected, manager, calls = installed
    configure(manager)
    archive = tmp_path / 'input.zip'
    package(archive)
    review = manager.inspect(archive)
    previous, count = manager.store.read(), len(calls)
    archive.write_bytes(archive.read_bytes() + b'changed after explicit review')
    with pytest.raises(ValueError, match='changed'):
        manager.apply(review, rights_approved=True, supported_languages=['uk'])
    assert manager.store.read() == previous and len(calls) == count
    assert_released(library)


@pytest.mark.parametrize('mode', ['socket', 'child'])
def test_actual_owner_child_cannot_connect_or_start_an_unowned_child(installed, tmp_path, mode):
    library, selected, manager, calls = installed
    configure(manager)
    archive = tmp_path / 'input.zip'
    package(archive, source='fixture:' + mode)
    review = manager.inspect(archive)
    previous = manager.store.read()
    with pytest.raises(RuntimeError, match='owner package API failed'):
        manager.apply(review, rights_approved=True, supported_languages=['uk'])
    assert manager.store.read() == previous
    assert not (selected.parent / 'models/voices/packages/fixture').exists()
    assert_released(library)


def test_actual_slow_owner_is_reaped_before_cancellation_releases_admission(installed, tmp_path):
    library, selected, manager, calls = installed
    configure(manager)
    archive = tmp_path / 'slow.zip'
    package(archive, source='fixture:slow')
    review = manager.inspect(archive)
    marker = tmp_path / 'voice-install-entered.txt'
    previous = manager.store.read()
    with pytest.raises((AudioCancelled, InterruptedError)):
        manager.apply(review, rights_approved=True, supported_languages=['uk'],
                      cancelled=marker.exists)
    assert marker.read_text() == 'actual owner entered'
    assert manager.store.read() == previous
    assert not (selected.parent / 'models/voices/packages/fixture').exists()
    assert_released(library)


def test_owner_helpers_are_captured_and_checked_again_inside_actual_child(installed, tmp_path, monkeypatch):
    library, selected, manager, calls = installed
    configure(manager)
    archive = tmp_path / 'input.zip'
    package(archive)
    real = voice_transport.run_metadata
    helper = selected.parent / 'voice_assets.py'
    original = helper.read_bytes()
    def changed(command, **kwargs):
        helper.write_bytes(original + b'\n# changed after parent verification\n')
        return real(command, **kwargs)
    monkeypatch.setattr(voice_transport, 'run_metadata', changed)
    with pytest.raises(RuntimeError, match='owner package API failed'):
        manager.inspect(archive)
    assert helper.read_bytes() != original
    assert not (selected.parent / 'models/voices/packages/fixture').exists()
    assert_released(library)


def test_owner_install_cannot_rebind_an_unrelated_runtime_change(installed, tmp_path, monkeypatch):
    library, selected, manager, calls = installed
    configure(manager)
    archive = tmp_path / 'input.zip'
    package(archive)
    review = manager.inspect(archive)
    before = manager.store.read()
    real = voice_packages._query
    def changed(manifest, action, **kwargs):
        result = real(manifest, action, **kwargs)
        if action == 'install':
            (selected.parent / 'models/Holos/model.bin').write_bytes(b'foreign post-child change')
        return result
    monkeypatch.setattr(voice_packages, '_query', changed)
    with pytest.raises(ValueError, match='changed'):
        manager.apply(review, rights_approved=True, supported_languages=['uk'])
    assert manager.store.read() == before
    assert (selected.parent / 'models/voices/packages/fixture/1.0.0').is_dir()
    with pytest.raises(ValueError, match='Checksum'):
        LocalModelManifest.load(selected).verify(require_complete=False)
    assert_released(library)


def test_replaced_library_is_refused_before_any_owner_child_or_recreation(installed, tmp_path):
    library, selected, manager, calls = installed
    saved = library.with_name('original-library')
    count = len(calls)
    library.rename(saved)
    library.mkdir()
    canary = library / 'canary.txt'
    canary.write_bytes(b'replacement must remain untouched')
    try:
        with pytest.raises((ValueError, FileNotFoundError)):
            manager.configuration()
        assert list(library.iterdir()) == [canary] and len(calls) == count
    finally:
        library.rename(library.with_name('untouched-replacement'))
        saved.rename(library)
    assert_released(library)


def test_actual_export_contains_exact_reviewed_version_and_preserves_originals(lifecycle_ready, tmp_path):
    library, selected, manager, lifecycle, calls = lifecycle_ready
    review = manager.review_version('fixture', '1.0.0')
    original = manager.store.read()
    output = tmp_path / 'export.dordiai-voice'
    result = lifecycle.export(review, output, export_rights_approved=True)
    assert result['sha256'] == sha256(output.read_bytes()).hexdigest()
    assert result['model_inference'] == 'NOT_RUN'
    import zipfile
    with zipfile.ZipFile(output) as archive:
        assert set(archive.namelist()) == {'voice-package.json', *review.document['files']}
        assert json.loads(archive.read('voice-package.json')) == review.document
    assert manager.store.read() == original
    assert_released(library)


@pytest.mark.parametrize('permission', [False, 1])
def test_export_requires_separate_exact_permission_before_owner_read(lifecycle_ready, tmp_path, permission):
    library, selected, manager, lifecycle, calls = lifecycle_ready
    review = manager.review_version('fixture', '1.0.0')
    count = len(calls)
    with pytest.raises(ValueError, match='permission'):
        lifecycle.export(review, tmp_path / 'no.zip', export_rights_approved=permission)
    assert len(calls) == count and not (tmp_path / 'no.zip').exists()
    assert_released(library)


@pytest.mark.parametrize('delete', [False, True], ids=['revoke', 'delete'])
def test_actual_revoke_or_delete_retains_local_revocation_without_legacy_fallback(lifecycle_ready, delete):
    library, selected, manager, lifecycle, calls = lifecycle_ready
    directory = selected.parent / 'models/voices/packages/fixture'
    before = {str(path): path.read_bytes() for path in directory.rglob('*') if path.is_file()}
    review = lifecycle.review_removal('fixture', delete_files=delete)
    result = lifecycle.remove(review, confirmation=('DELETE ' if delete else 'REVOKE ') + 'fixture')
    manifest = LocalModelManifest.load(selected)
    manifest.verify(require_complete=True)
    assert result == manager.store.read()
    assert 'fixture' in manifest.settings['revoked_profiles']
    legacy, profiles, owned = managed_catalog(manifest, {'fixture': 'unsafe fallback', 'legacy': 'legacy'})
    assert 'fixture' not in legacy and 'fixture' not in profiles and 'fixture' in owned
    if delete:
        assert not directory.exists()
    else:
        assert {str(path): path.read_bytes() for path in directory.rglob('*') if path.is_file()} == before
    assert_released(library)


@pytest.mark.parametrize('confirmation', ['DELETE wrong', 'yes', 'REVOKE fixture'])
def test_delete_needs_exact_reviewed_action_before_child(lifecycle_ready, confirmation):
    library, selected, manager, lifecycle, calls = lifecycle_ready
    review = lifecycle.review_removal('fixture', delete_files=True)
    count, previous = len(calls), manager.store.read()
    with pytest.raises(ValueError, match='confirmation'):
        lifecycle.remove(review, confirmation=confirmation)
    assert len(calls) == count and manager.store.read() == previous
    assert (selected.parent / 'models/voices/packages/fixture').is_dir()
    assert_released(library)


def test_removal_rechecks_review_after_the_actual_owner_lock_is_acquired(lifecycle_ready, tmp_path):
    library, selected, manager, lifecycle, calls = lifecycle_ready
    review = lifecycle.review_removal('fixture', delete_files=True)
    previous = manager.store.read()
    (tmp_path / 'voice-maintenance-case.txt').write_text('change-index-before-yield')
    with pytest.raises(RuntimeError, match='owner package API failed'):
        lifecycle.remove(review, confirmation='DELETE fixture')
    assert manager.store.read() == previous
    assert not (tmp_path / 'voice-removal-entered.txt').exists()
    assert (selected.parent / 'models/voices/packages/fixture').is_dir()
    assert_released(library)


def test_changed_ancestor_alias_is_refused_with_same_reference_and_lock_leaf_identities(installed, tmp_path, monkeypatch):
    import subprocess
    library, selected, manager, calls = installed
    configure(manager)
    archive = tmp_path / 'input.zip'
    package(archive)
    review = manager.inspect(archive)
    models = selected.parent / 'models'
    relocated = selected.parent / 'moved-models'
    leaves = [models / 'voices', models / 'voices/packages', models / 'voices/packages/.voice-packages.lock']
    identities = [(path.stat().st_dev, path.stat().st_ino) for path in leaves]
    before = manager.store.read()
    real = voice_transport.run_metadata
    observed = []
    def swapped(command, **kwargs):
        models.rename(relocated)
        try:
            if os.name == 'nt':
                completed = subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J',
                                            str(models), str(relocated)],
                                           capture_output=True, timeout=15)
                assert completed.returncode == 0, completed.stderr[-2048:]
            else:
                models.symlink_to(relocated, target_is_directory=True)
            assert [(path.stat().st_dev, path.stat().st_ino) for path in leaves] == identities
            observed.append(True)
            return real(command, **kwargs)
        finally:
            if os.path.lexists(models):
                if os.name == 'nt':
                    models.rmdir()
                else:
                    models.unlink()
            relocated.rename(models)
    monkeypatch.setattr(voice_transport, 'run_metadata', swapped)
    with pytest.raises(RuntimeError, match='owner package API failed'):
        manager.apply(review, rights_approved=True, supported_languages=['uk'])
    assert observed == [True] and manager.store.read() == before
    assert not (models / 'voices/packages/fixture').exists()
    assert not (models / 'voices/packages/voice-packages.json').exists()
    assert_released(library)


@pytest.mark.parametrize('phase', ['before-owner-child', 'after-owner-child'])
def test_real_concurrent_stream_lock_waits_before_hashing_administrative_byte(installed, tmp_path, monkeypatch, phase):
    import subprocess
    import sys
    import time
    from carr_library.audio import voice_ownership
    library, selected, manager, calls = installed
    configure(manager)
    archive = tmp_path / 'input.zip'
    package(archive)
    review = manager.inspect(archive)
    lock = selected.parent / 'models/voices/packages/.voice-packages.lock'
    ready, release = tmp_path / 'actor-ready', tmp_path / 'actor-release'
    actor = r"""
import os,sys,time
from pathlib import Path
lock,ready,release=map(Path,sys.argv[1:])
with lock.open('r+b') as stream:
    if os.name=='nt':
        import msvcrt
        msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
    else:
        import fcntl
        fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
    ready.write_bytes(b'held')
    deadline=time.monotonic()+10
    while not release.exists():
        if time.monotonic()>deadline: raise TimeoutError('synthetic stream was not released')
        time.sleep(.01)
"""
    children, waits, snapshots = [], [], []
    def start_actor():
        child = subprocess.Popen([sys.executable, '-I', '-B', '-c', actor,
                                  str(lock), str(ready), str(release)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        children.append(child)
        deadline = time.monotonic() + 5
        while not ready.exists():
            assert child.poll() is None, child.communicate(timeout=2)
            assert time.monotonic() < deadline
            time.sleep(.01)
    original_try = voice_ownership._try_lock
    def actual_try(descriptor):
        result = original_try(descriptor)
        if not result and ready.exists():
            waits.append(True)
            snapshots.append(manager.broker.snapshot())
            release.write_bytes(b'actual holder may close')
        return result
    monkeypatch.setattr(voice_ownership, '_try_lock', actual_try)
    original_query = voice_packages._query
    def query(manifest, action, **kwargs):
        if action == 'install' and phase == 'before-owner-child':
            start_actor()
        result = original_query(manifest, action, **kwargs)
        if action == 'install' and phase == 'after-owner-child':
            start_actor()
        return result
    monkeypatch.setattr(voice_packages, '_query', query)
    try:
        result = manager.apply(review, rights_approved=True, supported_languages=['uk'])
        assert result == manager.store.read()
        assert len(children) == 1 and waits and all(snapshot['leases'] for snapshot in snapshots)
        assert children[0].wait(timeout=5) == 0
        LocalModelManifest.load(selected).verify(require_complete=True)
    finally:
        release.write_bytes(b'cleanup')
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)
            child.stdout.close()
            child.stderr.close()
    assert_released(library)

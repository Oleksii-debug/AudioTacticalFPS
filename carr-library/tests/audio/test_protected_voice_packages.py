"""Real authenticated envelopes and the existing isolated synthetic owner API."""
from contextlib import contextmanager
from dataclasses import asdict, replace
from hashlib import sha256
import json
from pathlib import Path
import runpy
import threading
import zipfile

import pytest

from carr_library.audio import cleanup, process, voice_lifecycle
from carr_library.audio.models import AudioCancelled
from carr_library.audio.voice_lifecycle import ProtectedVoicePackageReview, VoiceProfileLifecycle
from carr_library.operations import ResourceBroker, ResourceLimits
from carr_library.sync import crypto

# Fixture source is explicit; product imports remain the source/installed package
# selected by the enclosing gate, including under --import-mode=importlib.
_fixture = runpy.run_path(str(Path(__file__).with_name('test_voice_packages.py')))
installed = _fixture['installed']
lifecycle_ready = _fixture['lifecycle_ready']
package = _fixture['package']
configure = _fixture['configure']
PASSWORD = 'Exact protected fixture password 42! '


@pytest.fixture
def protected(installed, tmp_path):
    library, manifest, manager, calls = installed
    configure(manager)
    original = tmp_path / 'input.dordiai-voice'
    document = package(original)
    encrypted = tmp_path / 'input.carr-voice.enc'
    receipt = crypto.encrypt_file(original, encrypted, PASSWORD)
    assert receipt.sha256 == sha256(encrypted.read_bytes()).hexdigest()
    broker = ResourceBroker.for_app(library, limits=ResourceLimits(
        cpu_slots=2, memory_bytes=512 * 1024**2, disk_bytes=64 * 1024**2,
        min_free_disk_bytes=0))
    lifecycle = VoiceProfileLifecycle(library, manager=manager, broker=broker)
    return library, manifest, manager, lifecycle, calls, original, encrypted, document


def clean(lifecycle, parent):
    assert lifecycle.broker.snapshot()['leases'] == []
    assert not list(parent.glob('.voice-protected-import-*'))


def test_actual_export_uses_same_envelope_and_exact_owner_data(lifecycle_ready, tmp_path):
    _, _, manager, lifecycle, _ = lifecycle_ready
    review = manager.review_version('fixture', '1.0.0')
    target = tmp_path / 'selected.carr-voice.enc'
    result = lifecycle.export_protected(review, target, passphrase=PASSWORD,
                                       export_rights_approved=True)
    assert result['encrypted'] is True
    assert result['sha256'] == sha256(target.read_bytes()).hexdigest()
    assert result['size_bytes'] == target.stat().st_size
    assert result['profile'] == 'fixture' and result['version'] == '1.0.0'
    assert not zipfile.is_zipfile(target)
    plain = tmp_path / 'explicit-test-decryption.dordiai-voice'
    crypto.decrypt_file(target, plain, PASSWORD)
    with zipfile.ZipFile(plain) as archive:
        assert set(archive.namelist()) == {'voice-package.json', *review.document['files']}
        assert json.loads(archive.read('voice-package.json')) == review.document
        for name, item in review.document['files'].items():
            assert sha256(archive.read(name)).hexdigest() == item['sha256']
    assert PASSWORD not in json.dumps(result)
    assert not list(tmp_path.glob('.voice-export-*'))
    clean(lifecycle, tmp_path)


def test_inspection_deletes_plaintext_before_return_and_apply_freshly_rechecks(protected, tmp_path):
    _, manifest, manager, lifecycle, calls, original, encrypted, document = protected
    before, ciphertext = manager.store.read(), encrypted.read_bytes()
    count = len(calls)
    review = lifecycle.inspect_protected(encrypted, passphrase=PASSWORD)
    assert type(review) is ProtectedVoicePackageReview and review.document == document
    assert review.plaintext_sha256 == sha256(original.read_bytes()).hexdigest()
    assert review.source_sha256 == sha256(ciphertext).hexdigest()
    assert len(calls) > count and manager.store.read() == before
    assert not (manifest.parent / 'models/voices/packages').exists()
    assert PASSWORD not in json.dumps(asdict(review))
    assert 'No profile has been installed' in review.summary()
    clean(lifecycle, tmp_path)
    applied = lifecycle.apply_protected(review, passphrase=PASSWORD,
        rights_approved=True, supported_languages=['uk'])
    assert applied['revision'] == before['revision'] + 1
    assert manager.store.read() == applied
    root = manifest.parent / 'models/voices/packages/fixture/1.0.0'
    with zipfile.ZipFile(original) as archive:
        for name in document['files']:
            assert (root / name).read_bytes() == archive.read(name)
    assert encrypted.read_bytes() == ciphertext
    assert PASSWORD not in manager.store.path.read_text()
    assert all(PASSWORD not in repr(command) for command in calls)
    clean(lifecycle, tmp_path)


@pytest.mark.parametrize('permission', [False, None, 1, 'yes'])
def test_protected_apply_requires_fresh_exact_permission_before_decryption(protected, monkeypatch, permission):
    _, _, _, lifecycle, _, _, encrypted, _ = protected
    review = lifecycle.inspect_protected(encrypted, passphrase=PASSWORD)
    monkeypatch.setattr(crypto, 'decrypt_file', lambda *a, **k: pytest.fail('No apply authority.'))
    with pytest.raises(ValueError, match='permission'):
        lifecycle.apply_protected(review, passphrase=PASSWORD,
            rights_approved=permission, supported_languages=['uk'])


@pytest.mark.parametrize('phase', ['inspect', 'apply'])
def test_wrong_password_never_enters_owner_inspection_or_install(protected, tmp_path, monkeypatch, phase):
    _, _, manager, lifecycle, calls, _, encrypted, _ = protected
    review = lifecycle.inspect_protected(encrypted, passphrase=PASSWORD) if phase == 'apply' else None
    before, count = manager.store.read(), len(calls)
    monkeypatch.setattr(manager, 'inspect', lambda *a, **k: pytest.fail('Unauthenticated package reached owner.'))
    with pytest.raises(crypto.CryptoError, match='incorrect'):
        if phase == 'inspect':
            lifecycle.inspect_protected(encrypted, passphrase=PASSWORD.rstrip())
        else:
            lifecycle.apply_protected(review, passphrase=PASSWORD.rstrip(),
                rights_approved=True, supported_languages=['uk'])
    assert manager.store.read() == before and len(calls) == count
    clean(lifecycle, tmp_path)


@pytest.mark.parametrize('kind', ['tag', 'truncated', 'trailing'])
def test_corrupted_envelope_never_produces_an_owner_package(protected, tmp_path, monkeypatch, kind):
    _, _, manager, lifecycle, _, _, encrypted, _ = protected
    raw = bytearray(encrypted.read_bytes())
    if kind == 'tag':
        raw[-1] ^= 1
    elif kind == 'truncated':
        del raw[-1]
    else:
        raw += b'unapproved trailing byte'
    encrypted.write_bytes(raw)
    before = manager.store.read()
    monkeypatch.setattr(manager, 'inspect', lambda *a, **k: pytest.fail('Invalid envelope reached owner.'))
    with pytest.raises(crypto.CryptoError):
        lifecycle.inspect_protected(encrypted, passphrase=PASSWORD)
    assert manager.store.read() == before
    clean(lifecycle, tmp_path)


def test_plaintext_with_protected_name_is_not_a_fallback(protected, tmp_path, monkeypatch):
    _, _, manager, lifecycle, _, original, encrypted, _ = protected
    encrypted.write_bytes(original.read_bytes())
    monkeypatch.setattr(manager, 'inspect', lambda *a, **k: pytest.fail('No plaintext fallback.'))
    with pytest.raises(crypto.CryptoError):
        lifecycle.inspect_protected(encrypted, passphrase=PASSWORD)
    clean(lifecycle, tmp_path)


@pytest.mark.parametrize('kind', ['ciphertext', 'registration', 'library'])
def test_apply_rejects_stale_identity_before_new_decryption(protected, monkeypatch, kind):
    _, _, manager, lifecycle, _, _, encrypted, _ = protected
    review = lifecycle.inspect_protected(encrypted, passphrase=PASSWORD)
    if kind == 'ciphertext':
        changed = bytearray(encrypted.read_bytes())
        changed[-1] ^= 1
        encrypted.write_bytes(changed)
    elif kind == 'registration':
        configure(manager)
    else:
        review = replace(review, library_identity=(review.library_identity[0], review.library_identity[1] + 1))
    monkeypatch.setattr(crypto, 'decrypt_file', lambda *a, **k: pytest.fail('Stale review must not decrypt.'))
    with pytest.raises(ValueError, match='changed|another library'):
        lifecycle.apply_protected(review, passphrase=PASSWORD,
            rights_approved=True, supported_languages=['uk'])


def test_owner_inspection_mutation_cannot_become_a_protected_review(protected, tmp_path, monkeypatch):
    _, _, manager, lifecycle, _, _, encrypted, _ = protected
    inspect = manager.inspect
    def change(source, **kwargs):
        result = inspect(source, **kwargs)
        Path(source).write_bytes(b'changed after actual owner inspection')
        return result
    monkeypatch.setattr(manager, 'inspect', change)
    with pytest.raises(ValueError, match='changed'):
        lifecycle.inspect_protected(encrypted, passphrase=PASSWORD)
    clean(lifecycle, tmp_path)


def test_source_change_after_authentication_precedes_owner_admission(protected, tmp_path, monkeypatch):
    _, _, manager, lifecycle, _, _, encrypted, _ = protected
    decrypt = crypto.decrypt_file
    def change(*args, **kwargs):
        result = decrypt(*args, **kwargs)
        raw = bytearray(encrypted.read_bytes())
        raw[-1] ^= 1
        encrypted.write_bytes(raw)
        return result
    monkeypatch.setattr(crypto, 'decrypt_file', change)
    monkeypatch.setattr(manager, 'inspect', lambda *a, **k: pytest.fail('Changed envelope reached owner.'))
    with pytest.raises(ValueError, match='changed during authentication'):
        lifecycle.inspect_protected(encrypted, passphrase=PASSWORD)
    clean(lifecycle, tmp_path)


def test_admission_is_measured_and_retained_during_actual_crypto_and_owner_work(protected, tmp_path, monkeypatch):
    _, _, manager, lifecycle, _, _, encrypted, _ = protected
    reserve, decrypt, inspect = lifecycle.broker.reserve, crypto.decrypt_file, manager.inspect
    requests, phases = [], []
    @contextmanager
    def admission(request, **kwargs):
        requests.append(request)
        with reserve(request, **kwargs) as lease:
            yield lease
    def guarded_decrypt(*args, **kwargs):
        assert len(lifecycle.broker.snapshot()['leases']) == 1
        phases.append('decrypt')
        return decrypt(*args, **kwargs)
    def guarded_inspect(*args, **kwargs):
        assert len(lifecycle.broker.snapshot()['leases']) == 1
        phases.append('owner')
        return inspect(*args, **kwargs)
    monkeypatch.setattr(lifecycle.broker, 'reserve', admission)
    monkeypatch.setattr(crypto, 'decrypt_file', guarded_decrypt)
    monkeypatch.setattr(manager, 'inspect', guarded_inspect)
    lifecycle.inspect_protected(encrypted, passphrase=PASSWORD)
    assert phases == ['decrypt', 'owner'] and len(requests) == 1
    request, = requests
    assert request.disk_bytes == 2 * encrypted.stat().st_size + 4 * 1024**2
    assert request.memory_bytes == 256 * 1024**2 and request.disk_path == tmp_path
    clean(lifecycle, tmp_path)


def test_cancel_after_actual_decryption_removes_private_plaintext(protected, tmp_path, monkeypatch):
    _, _, manager, lifecycle, _, _, encrypted, _ = protected
    stopped, decrypt = threading.Event(), crypto.decrypt_file
    def cancelled(*args, **kwargs):
        result = decrypt(*args, **kwargs)
        stopped.set()
        return result
    monkeypatch.setattr(crypto, 'decrypt_file', cancelled)
    monkeypatch.setattr(manager, 'inspect', lambda *a, **k: pytest.fail('Cancelled plaintext reached owner.'))
    with pytest.raises((InterruptedError, AudioCancelled)) as caught:
        lifecycle.inspect_protected(encrypted, passphrase=PASSWORD, cancelled=stopped.is_set)
    assert 'cancel' in str(caught.value).lower()
    clean(lifecycle, tmp_path)


@pytest.mark.parametrize('masked', [False, True])
def test_actual_owner_pending_cleanup_retains_decrypted_stage_and_lease(protected, tmp_path, monkeypatch, masked):
    _, _, manager, lifecycle, _, _, encrypted, _ = protected
    original_stop = process._stop_owned
    allowed, owners = threading.Event(), []
    def stop(child):
        assert original_stop(child) is True
        if not allowed.is_set():
            raise TimeoutError('Synthetic uncertainty after the actual inspection child exited.')
        return True
    def pending(attempt, *, message, callbacks=()):
        owner = cleanup.CleanupOwner(attempt)
        for callback in callbacks:
            owner.add_quiescent_callback(callback)
        owners.append(owner)
        return cleanup.AudioCleanupPending(message, cleanup_owner=owner)
    inspect = manager.inspect
    def wrapped(*args, **kwargs):
        try:
            return inspect(*args, **kwargs)
        except cleanup.AudioCleanupPending:
            if masked:
                raise OSError('Synthetic status failure hiding pending owner.')
            raise
    monkeypatch.setattr(process, '_stop_owned', stop)
    monkeypatch.setattr(cleanup, 'pending_cleanup', pending)
    monkeypatch.setattr(manager, 'inspect', wrapped)
    try:
        with pytest.raises((cleanup.AudioCleanupPending, OSError)):
            lifecycle.inspect_protected(encrypted, passphrase=PASSWORD)
        stage, = tmp_path.glob('.voice-protected-import-*')
        assert (stage / 'profile.dordiai-voice').is_file()
        assert len(lifecycle.broker.snapshot()['leases']) == 1
        assert owners and all(not owner.quiescent for owner in owners)
        assert not list(tmp_path.glob('**/packages/fixture'))
        allowed.set()
        assert all(owner.close() for owner in owners)
        assert not stage.exists()
        clean(lifecycle, tmp_path)
    finally:
        allowed.set()
        for owner in owners:
            owner.close()


@pytest.mark.parametrize('kind', ['rights', 'occupied', 'suffix', 'capability'])
def test_export_refusals_do_not_enter_owner_or_publish_plaintext(lifecycle_ready, tmp_path, monkeypatch, kind):
    _, _, manager, lifecycle, _ = lifecycle_ready
    review = manager.review_version('fixture', '1.0.0')
    target = tmp_path / ('unsafe.zip' if kind == 'suffix' else 'selected.carr-voice.enc')
    if kind == 'occupied':
        target.write_bytes(b'COMPETITOR')
    monkeypatch.setattr(voice_lifecycle, '_query', lambda *a, **k: pytest.fail('Unapproved export entered owner.'))
    if kind == 'capability':
        monkeypatch.setattr(crypto, '_load_crypto', lambda: (_ for _ in ()).throw(
            crypto.CryptoUnavailable('Pinned optional capability unavailable.')))
    with pytest.raises((ValueError, FileExistsError, crypto.CryptoUnavailable)):
        lifecycle.export_protected(review, target, passphrase=PASSWORD,
                                   export_rights_approved=kind != 'rights')
    if kind == 'occupied':
        assert target.read_bytes() == b'COMPETITOR'
    else:
        assert not target.exists()
    assert not list(tmp_path.glob('.voice-export-*'))
    clean(lifecycle, tmp_path)


def test_ciphertext_receipt_is_checked_before_external_publication(lifecycle_ready, tmp_path, monkeypatch):
    _, _, manager, lifecycle, _ = lifecycle_ready
    review = manager.review_version('fixture', '1.0.0')
    encrypt = crypto.encrypt_file
    def corrupt(*args, **kwargs):
        receipt = encrypt(*args, **kwargs)
        raw = bytearray(receipt.path.read_bytes())
        raw[-1] ^= 1
        receipt.path.write_bytes(raw)
        return receipt
    monkeypatch.setattr(crypto, 'encrypt_file', corrupt)
    target = tmp_path / 'not-published.carr-voice.enc'
    with pytest.raises(ValueError, match='encryption receipt'):
        lifecycle.export_protected(review, target, passphrase=PASSWORD, export_rights_approved=True)
    assert not target.exists()
    clean(lifecycle, tmp_path)


def test_verified_protected_export_survives_late_cancel(lifecycle_ready, tmp_path, monkeypatch):
    _, _, manager, lifecycle, _ = lifecycle_ready
    review = manager.review_version('fixture', '1.0.0')
    stopped, publish = threading.Event(), voice_lifecycle.publish_package
    def committed(*args, **kwargs):
        receipt = publish(*args, **kwargs)
        stopped.set()
        return receipt
    monkeypatch.setattr(voice_lifecycle, 'publish_package', committed)
    target = tmp_path / 'committed.carr-voice.enc'
    result = lifecycle.export_protected(review, target, passphrase=PASSWORD,
        export_rights_approved=True, cancelled=stopped.is_set)
    assert stopped.is_set() and result['encrypted'] is True
    assert result['sha256'] == sha256(target.read_bytes()).hexdigest()
    clean(lifecycle, tmp_path)


def test_applied_registration_survives_later_private_cleanup_notice(protected, tmp_path, monkeypatch):
    _, _, manager, lifecycle, _, _, encrypted, _ = protected
    review = lifecycle.inspect_protected(encrypted, passphrase=PASSWORD)
    temporary = voice_lifecycle.retained_temporary_directory
    before = manager.store.read()['revision']
    @contextmanager
    def notice(**kwargs):
        with temporary(**kwargs) as path:
            yield path
        raise OSError('Synthetic cleanup failure after actual stage removal.')
    monkeypatch.setattr(voice_lifecycle, 'retained_temporary_directory', notice)
    result = lifecycle.apply_protected(review, passphrase=PASSWORD,
        rights_approved=True, supported_languages=['uk'])
    assert result['revision'] == manager.store.read()['revision'] == before + 1
    assert 'do not repeat' in result['cleanup_warning']
    clean(lifecycle, tmp_path)


def test_complete_backup_never_retains_secret_plaintext_or_fresh_apply_authority(protected, tmp_path, monkeypatch):
    from carr_library.application import LibraryApp
    from carr_library.audio.voice_packages import VoicePackageManager
    from carr_library.library_backup import create_backup, restore_backup, verify_backup
    library, _, _, lifecycle, _, _, encrypted, _ = protected
    lifecycle.inspect_protected(encrypted, passphrase=PASSWORD)
    app = LibraryApp(library)
    workspace = app.create_workspace('Protected package review remains passive')
    app.add_text(workspace, 'Original', 'An immutable original for complete backup.')
    backup, restored = tmp_path / 'backup', tmp_path / 'restored'
    checksum = create_backup(app, backup)
    verify_backup(backup, checksum)
    assert not list(backup.rglob('*carr-voice.enc'))
    for file in backup.rglob('*.json'):
        assert PASSWORD.encode() not in file.read_bytes()
    restore_backup(backup, restored, checksum)
    restored_manager = VoicePackageManager(restored)
    assert restored_manager.store.read()['restored'] is True
    monkeypatch.setattr(crypto, 'decrypt_file', lambda *a, **k: pytest.fail('Restored authority must reject first.'))
    with pytest.raises(ValueError, match='Restored'):
        VoiceProfileLifecycle(restored, manager=restored_manager).inspect_protected(
            encrypted, passphrase=PASSWORD)
    clean(lifecycle, tmp_path)


@pytest.mark.parametrize('field,value', [
    ('plaintext_sha256', 'e' * 64), ('plaintext_size', 1),
    ('document_json', '{"different":"unreviewed"}')])
def test_changed_passive_review_never_installs_a_different_package(protected, tmp_path, field, value):
    _, manifest, manager, lifecycle, _, _, encrypted, _ = protected
    review = lifecycle.inspect_protected(encrypted, passphrase=PASSWORD)
    before = manager.store.read()
    with pytest.raises(ValueError, match='exact reviewed contents'):
        lifecycle.apply_protected(replace(review, **{field: value}), passphrase=PASSWORD,
            rights_approved=True, supported_languages=['uk'])
    assert manager.store.read() == before
    assert not (manifest.parent / 'models/voices/packages').exists()
    clean(lifecycle, tmp_path)


def test_profile_size_bound_is_enforced_before_crypto_reads(protected, tmp_path, monkeypatch):
    _, _, _, lifecycle, _, _, encrypted, _ = protected
    monkeypatch.setattr(voice_lifecycle, 'MAX_PACKAGE_BYTES', encrypted.stat().st_size - 1)
    monkeypatch.setattr(crypto, 'decrypt_file', lambda *a, **k: pytest.fail('Oversized profile must not decrypt.'))
    with pytest.raises(ValueError, match='bounded regular'):
        lifecycle.inspect_protected(encrypted, passphrase=PASSWORD)
    clean(lifecycle, tmp_path)


def test_other_authenticated_archive_type_never_becomes_a_voice_package(protected, tmp_path):
    _, manifest, manager, lifecycle, _, _, encrypted, _ = protected
    foreign = tmp_path / 'other-kind.zip'
    with zipfile.ZipFile(foreign, 'w') as archive:
        archive.writestr('backup.json', '{"kind":"different authenticated archive"}')
    encrypted.unlink()
    crypto.encrypt_file(foreign, encrypted, PASSWORD)
    before = manager.store.read()
    with pytest.raises((ValueError, RuntimeError)):
        lifecycle.inspect_protected(encrypted, passphrase=PASSWORD)
    assert manager.store.read() == before
    assert not (manifest.parent / 'models/voices/packages').exists()
    clean(lifecycle, tmp_path)

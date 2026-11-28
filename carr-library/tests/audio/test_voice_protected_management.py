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
_fixture = runpy.run_path(str(Path(__file__).with_name('voice_management_fixtures.py')))
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
    broker = manager.broker
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
    assert not (manifest.parent / 'models/voices/packages/voice-packages.json').exists()
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


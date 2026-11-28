"""Receipt-bound copies must refuse changed bytes before exposing a destination."""
from hashlib import sha256

import pytest

from carr_library.audio import jobs


def test_source_change_before_identity_capture_cannot_publish_stale_receipt_bytes(tmp_path):
    source, target = tmp_path / 'source.wav', tmp_path / 'destination.wav'
    accepted = b'ACCEPTED PCM SOURCE'
    source.write_bytes(accepted)
    receipt = sha256(accepted).hexdigest()
    # This is the export's actual gap: the source was verified, then changes
    # before the later file identity is captured. The new identity is honest.
    source.write_bytes(b'REPLACED PCM SOURCE')
    current = jobs.file_identity(source.stat())
    with pytest.raises(ValueError, match='publication digest'):
        jobs.publish_new(source, target, expected_identity=current, expected_sha256=receipt)
    assert not target.exists()
    assert not list(tmp_path.glob('.publish-*'))
    assert source.read_bytes() == b'REPLACED PCM SOURCE'


def test_receipt_bound_independent_copy_and_existing_destination_remain_exact(tmp_path):
    source, target = tmp_path / 'source.wav', tmp_path / 'destination.wav'
    payload = b'exact accepted bytes' * 100
    source.write_bytes(payload)
    digest = sha256(payload).hexdigest()
    jobs.publish_new(source, target, expected_sha256=digest)
    assert target.read_bytes() == payload and target.stat().st_nlink == source.stat().st_nlink == 1
    source.write_bytes(b'new unreviewed source')
    with pytest.raises(FileExistsError):
        jobs.publish_new(source, target, expected_sha256=digest)
    assert target.read_bytes() == payload and not list(tmp_path.glob('.publish-*'))


@pytest.mark.parametrize('digest', ['', 'A' * 64, 'z' * 64, 123, True])
def test_invalid_expected_digest_fails_before_staging_or_reading(tmp_path, monkeypatch, digest):
    monkeypatch.setattr(jobs, 'verified_reader', lambda *a, **k: pytest.fail('Invalid digest opened a source.'))
    monkeypatch.setattr(jobs.tempfile, 'mkstemp', lambda *a, **k: pytest.fail('Invalid digest created scratch.'))
    with pytest.raises(ValueError, match='digest is invalid'):
        jobs.publish_new(tmp_path / 'absent-source', tmp_path / 'output', expected_sha256=digest)
    assert list(tmp_path.iterdir()) == []


def test_unbound_legacy_copy_api_preserves_its_existing_behavior(tmp_path):
    source, target = tmp_path / 'legacy-source', tmp_path / 'legacy-output'
    source.write_bytes(b'legacy independent copy')
    jobs.publish_new(source, target, expected_identity=jobs.file_identity(source.stat()))
    assert target.read_bytes() == source.read_bytes() and target.stat().st_nlink == 1

"""Portable checks for native probe admission; these do not execute Inno."""
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from carr_library.security.updates import LocalRepositoryClient


path = Path(__file__).resolve().parents[2] / 'scripts/windows_managed_update_smoke.py'
spec = importlib.util.spec_from_file_location('managed_update_native_policy', path)
native = importlib.util.module_from_spec(spec)
spec.loader.exec_module(native)


def test_native_gate_refuses_non_windows_before_loading_or_mutating_inputs(tmp_path, monkeypatch):
    monkeypatch.setattr(native, 'os', SimpleNamespace(name='posix'))
    monkeypatch.setattr(native, '_load', lambda *args: pytest.fail('Native setup was entered on a substitute platform.'))
    with pytest.raises(RuntimeError, match='real Windows'):
        native.probe(None, None, None, None, None, tmp_path / 'work', tmp_path / 'report')
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('field', ['setup_sha256', 'setup_bytes', 'source_commit', 'status', 'release_status'])
def test_native_gate_refuses_changed_compiled_receipt_before_execution(tmp_path, field):
    setup = tmp_path / 'synthetic-unexecuted.exe'
    setup.write_bytes(b'Public synthetic non-executable fixture')
    value = dict(status='BUILT_NOT_INSTALLED', source_commit='a' * 40,
        setup_sha256=sha256(setup.read_bytes()).hexdigest(), setup_bytes=setup.stat().st_size,
        release_status='unsigned-private-candidate')
    evidence = tmp_path / 'setup-build.json'
    evidence.write_text(json.dumps(value), encoding='utf-8')
    assert native._evidence(setup, evidence, 'a' * 40) == value
    value[field] = 1 if field == 'setup_bytes' else 'changed'
    evidence.write_text(json.dumps(value), encoding='utf-8')
    with pytest.raises(RuntimeError, match='exact source receipt'):
        native._evidence(setup, evidence, 'a' * 40)


def test_native_probe_synthetic_repository_uses_actual_tuf_verified_target(tmp_path):
    package = tmp_path / 'original-synthetic-target.zip'
    package.write_bytes(b'Synthetic bytes: this test verifies TUF only, never setup execution.')
    build = dict(application_version='0.2.0.dev1', source_commit='b' * 40)
    repository = tmp_path / 'synthetic local publisher'
    policy = native._repository(repository, package, build)
    client = LocalRepositoryClient(tmp_path / 'client', policy)
    review = client.check(repository, 'managed-update.zip')
    assert (review.product, review.channel, review.build) == ('carr-managed-native-probe', 'synthetic', 2)
    verified = client.download(repository, 'managed-update.zip', reviewed_sha256=review.sha256)
    assert verified.path.read_bytes() == package.read_bytes()
    assert {p.relative_to(repository).as_posix() for p in repository.rglob('*') if p.is_file()} == {
        'metadata/1.root.json', 'metadata/1.targets.json', 'metadata/1.snapshot.json',
        'metadata/timestamp.json', 'targets/' + sha256(package.read_bytes()).hexdigest() + '.managed-update.zip'}
    # There is no private signing-key or arbitrary helper file in the fixture.

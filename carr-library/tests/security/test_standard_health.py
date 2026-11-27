"""Real managed payload checks; process results here are explicitly synthetic.

The unchanged native standard gate executes all three health commands from the
actual installed interpreter. These regressions use real files and the current
bundle/transfer validators, never execute the inert fixture interpreter.
"""
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from carr_library.security import installation, standard_windows
from carr_library.security._files import TrustError, json_bytes
from carr_library.security.standard_windows import WindowsStandardHost
from carr_library.security.updates import Release
from .test_installation import _managed_slot
from .test_standard_update import B, C, SyntheticStandardHost, setup_image


@pytest.fixture
def managed(tmp_path, monkeypatch):
    root = tmp_path / 'standard-health'
    (root / 'versions').mkdir(parents=True)
    (root / 'install-layout-v1.txt').write_bytes(b'Resonance.StandardInstall/1\r\n')
    host = SyntheticStandardHost(root)
    generation = host.install(setup_image(B, 'B')[0])
    release = Release('carr-test', 'stable', '0.2.0', 2, B, 4,
                      'release.zip', 1, 'd' * 64, '')
    value = SimpleNamespace(root=root, host=host, generation=generation,
        release=release, snapshot=host.snapshot(root), calls=[], failed_at=None,
        after_command=None, stopped=False)

    def process(command, **options):
        # The real process owner is exercised by the independent native gate.
        value.calls.append((tuple(command), options))
        if value.after_command is not None:
            value.after_command()
        return (7 if len(value.calls) - 1 == value.failed_at else 0), b''

    monkeypatch.setattr(standard_windows, 'run_owned', process)
    return value


def inventory(root):
    return {path.relative_to(root).as_posix(): sha256(path.read_bytes()).hexdigest()
            for path in root.rglob('*') if path.is_file()}


def check(value, *, snapshot=None, release=None, cancelled=lambda: False):
    return WindowsStandardHost.health(value.host, value.root,
        value.snapshot if snapshot is None else snapshot,
        value.release if release is None else release, cancelled=cancelled)


def source_receipt(value, damage=None):
    generation = value.generation
    manifest_path = generation / 'bundle-manifest.json'
    manifest = json.loads(manifest_path.read_bytes())
    name = 'wheelhouse/universal_research_library-0.2.0-py3-none-any.whl'
    wheel = b'INERT EXACT WHEEL RECEIPT FIXTURE; NEVER EXECUTE'
    (generation / name).write_bytes(wheel)
    manifest['files'][name] = sha256(wheel).hexdigest()
    proof = {'source_commit': B, 'project_name': 'universal-research-library',
             'project_version': '0.2.0', 'wheel_sha256': sha256(wheel).hexdigest()}
    if damage == 'source_commit':
        proof[damage] = C
    elif damage == 'project_name':
        proof[damage] = 'another-project'
    elif damage == 'project_version':
        proof[damage] = '0.1.0'
    elif damage == 'wheel_sha256':
        proof[damage] = 'e' * 64
    raw = json_bytes(proof)
    (generation / 'application-source.json').write_bytes(raw)
    if damage != 'unsealed':
        manifest['files']['application-source.json'] = sha256(raw).hexdigest()
    manifest_path.write_bytes(json_bytes(manifest))
    value.snapshot = value.host.snapshot(value.root)


@pytest.mark.parametrize('with_receipt', [False, True])
def test_standard_health_verifies_managed_payload_and_runs_fixed_commands(managed, with_receipt):
    if with_receipt:
        source_receipt(managed)
    before = inventory(managed.root)
    assert check(managed) is True
    assert len(managed.calls) == 3
    python = managed.generation / '.venv' / 'Scripts' / 'python.exe'
    first, second, third = [row[0] for row in managed.calls]
    assert all(command[:5] == (python, '-I', '-B', '-X', 'utf8')
               for command, _ in managed.calls)
    assert first[5:] == ('-m', 'carr_library.native_checks', '--bundle-root',
                         str(managed.generation))
    assert second[5:] == ('-m', 'carr_library', '--self-test')
    assert third[5] == '-c' and third[-1] == managed.release.version
    for _, options in managed.calls:
        assert options['cwd'] == managed.generation
        assert options['timeout'] == 30
        assert options['environment'] == managed.host.environment()
    assert inventory(managed.root) == before
    assert managed.host.snapshot(managed.root) == managed.snapshot


@pytest.mark.parametrize('layout', ['app', 'versions'])
def test_portable_release_verifier_still_refuses_managed_ownership(tmp_path, layout):
    slot, marker, original = _managed_slot(tmp_path, layout)
    def forbidden(*args, **kwargs):
        pytest.fail('Portable verification must reject before sharing any payload checks.')
    adapter = installation.WindowsBundleAdapter(forbidden)
    with pytest.raises(TrustError, match='installer ownership receipts'):
        adapter._verify_release(slot, None)
    assert marker.read_bytes() == original
    assert not list(slot.iterdir())


def test_standard_health_rejects_signed_source_mismatch_before_commands(managed):
    before = inventory(managed.root)
    with pytest.raises(TrustError, match='source commit'):
        check(managed, release=replace(managed.release, source_commit=C))
    assert not managed.calls and inventory(managed.root) == before


@pytest.mark.parametrize('changed', ['manifest', 'python', 'setup', 'shortcut', 'registration'])
def test_standard_health_rejects_changed_activation_before_commands(managed, changed):
    if changed == 'manifest':
        path = managed.generation / 'bundle-manifest.json'
        value = json.loads(path.read_bytes())
        value['source_commit'] = C
        path.write_bytes(json_bytes(value))
    elif changed == 'python':
        (managed.generation / '.venv' / 'Scripts' / 'python.exe').write_bytes(b'changed interpreter')
    elif changed == 'setup':
        (managed.generation / 'maintenance-setup.exe').write_bytes(b'changed setup')
    elif changed == 'shortcut':
        managed.host.shortcut().write_bytes(str(managed.root / 'foreign.exe').encode())
    else:
        path = managed.root / 'synthetic-registration.json'
        value = json.loads(path.read_bytes())
        value['DisplayVersion']['value'] = 'foreign registration'
        path.write_bytes(json_bytes(value))
    before = inventory(managed.root)
    with pytest.raises((TrustError, ValueError)):
        check(managed)
    assert not managed.calls and inventory(managed.root) == before


@pytest.mark.parametrize('field', ['program', 'generation', 'source_commit', 'manifest_sha256'])
def test_standard_health_rejects_foreign_snapshot_before_commands(managed, field, tmp_path):
    snapshot = json.loads(json_bytes(managed.snapshot))
    if field == 'program':
        foreign = tmp_path / 'foreign'
        foreign.mkdir()
        snapshot[field] = str(foreign)
    elif field == 'generation':
        snapshot[field] = '../foreign'
    elif field == 'source_commit':
        snapshot[field] = C
    else:
        snapshot[field] = 'e' * 64
    before = inventory(managed.root)
    with pytest.raises(TrustError):
        check(managed, snapshot=snapshot)
    assert not managed.calls and inventory(managed.root) == before


@pytest.mark.parametrize('changed', ['program', 'wheel', 'completion'])
def test_standard_health_keeps_real_payload_and_installation_checks(managed, changed):
    if changed == 'program':
        (managed.generation / 'program.py').write_bytes(b'changed program')
    elif changed == 'wheel':
        (managed.generation / 'wheelhouse' / 'unannounced.whl').write_bytes(b'foreign wheel')
    else:
        (managed.generation / '.venv' / '.installation-complete').unlink()
    before = inventory(managed.root)
    with pytest.raises((TrustError, ValueError, FileNotFoundError)):
        check(managed)
    assert not managed.calls and inventory(managed.root) == before


@pytest.mark.parametrize('changed', ['registration', 'python'])
def test_standard_health_rechecks_activation_after_child_completion(managed, changed):
    def change_once():
        managed.after_command = None
        if changed == 'python':
            (managed.generation / '.venv' / 'Scripts' / 'python.exe').write_bytes(b'changed interpreter')
        else:
            path = managed.root / 'synthetic-registration.json'
            value = json.loads(path.read_bytes())
            value['DisplayVersion']['value'] = 'changed during health'
            path.write_bytes(json_bytes(value))
    managed.after_command = change_once
    with pytest.raises(TrustError, match='changed during its health check'):
        check(managed)
    assert len(managed.calls) == 3


@pytest.mark.parametrize('damage', ['source_commit', 'project_name', 'project_version', 'wheel_sha256', 'unsealed'])
def test_standard_health_preserves_optional_exact_source_receipt_checks(managed, damage):
    source_receipt(managed, damage)
    before = inventory(managed.root)
    with pytest.raises(TrustError, match='application source receipt'):
        check(managed)
    assert not managed.calls and inventory(managed.root) == before


@pytest.mark.parametrize('failed_at', [0, 1, 2])
def test_standard_health_stops_at_each_failed_health_command(managed, failed_at):
    managed.failed_at = failed_at
    before = inventory(managed.root)
    assert check(managed) is False
    assert len(managed.calls) == failed_at + 1
    assert inventory(managed.root) == before


def test_standard_health_keeps_rejected_generation_unlaunchable(managed):
    (managed.generation / 'standard-update-rejected.json').write_bytes(b'{}')
    before = inventory(managed.root)
    assert check(managed) is False
    assert not managed.calls and inventory(managed.root) == before


@pytest.mark.parametrize('phase', ['before', 'during'])
def test_standard_health_cancellation_never_reports_success(managed, phase):
    managed.stopped = phase == 'before'
    def stop():
        managed.stopped = True
    managed.after_command = stop
    with pytest.raises(TrustError, match='cancel'):
        check(managed, cancelled=lambda: managed.stopped)
    assert len(managed.calls) == (0 if phase == 'before' else 1)

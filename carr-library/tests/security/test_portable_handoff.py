"""Real signed packages, shared leases, state backups and recovery boundaries.

Only the native install/health operations use a labelled synthetic adapter.
Actual Windows guardian and launch qualification live in their separate suite.
"""
from dataclasses import asdict
from hashlib import sha256
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest

from carr_library import installation_lease as leases
from carr_library import portable_entry
from carr_library.security import handoff, installation
from carr_library.security._files import TrustError, json_bytes
from carr_library.security.anchors import Anchor
from carr_library.security.updates import LocalRepositoryClient
from .conftest import bundle, installer_verify
from .test_installation import setup_update, assert_preserved


def portable_bundle(version, *, marker=portable_entry.PORTABLE_CONTRACT,
                    seal_marker=True, source=None):
    with zipfile.ZipFile(io.BytesIO(bundle(version))) as source_zip:
        files = {name: source_zip.read(name) for name in source_zip.namelist()
                 if name != 'bundle-manifest.json'}
    files.pop('wheelhouse/synthetic.whl')
    wheel = io.BytesIO()
    package_root = Path(portable_entry.__file__).resolve().parent
    with zipfile.ZipFile(wheel, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for relative in portable_entry.OWNED_SOURCES:
            archive.writestr('carr_library/' + relative, (package_root / relative).read_bytes())
    files['wheelhouse/universal_research_library-0.2.0-py3-none-any.whl'] = wheel.getvalue()
    files.update({
        portable_entry.PORTABLE_MARKER: marker,
        'runtime/python.exe': b'Synthetic interpreter; never executed.',
        'ResearchLibrary.exe': b'Synthetic PE; never executed.',
        'native-launcher-build.json': b'Synthetic source receipt; not native evidence.',
    })
    manifest = {'schema': 1, 'bundled_python': True, 'bundled_launcher': True,
                'source_commit': source or ('1' if version == 'A' else '2') * 40,
                'files': {name: sha256(raw).hexdigest() for name, raw in files.items()}}
    if not seal_marker:
        del manifest['files'][portable_entry.PORTABLE_MARKER]
    raw = json_bytes(manifest)
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
        archive.writestr('bundle-manifest.json', raw)
    return output.getvalue(), sha256(raw).hexdigest()


def download(repository, cache, raw):
    repository.advance()
    repository.publish(raw)
    client = LocalRepositoryClient(cache, repository.policy())
    reviewed = client.check(repository.root, repository.target_path)
    return client.download(repository.root, repository.target_path,
                           reviewed_sha256=reviewed.sha256)


def install_owned_fixture(root):
    """Populate only the exact entry bytes that the real identity check reads."""
    wheel, = (root / 'wheelhouse').glob('universal_research_library-*.whl')
    package = root / '.venv/Lib/site-packages/carr_library'
    with zipfile.ZipFile(wheel) as archive:
        for relative in portable_entry.OWNED_SOURCES:
            path = package / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(archive.read('carr_library/' + relative))
    python = root / '.venv/Scripts/python.exe'
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_bytes(b'Synthetic executable: native health is tested separately.')
    (root / '.venv/.installation-complete').touch(exist_ok=True)


@pytest.fixture
def ready(setup_update, repository, tmp_path, monkeypatch):
    app, *_rest, old_package, old_tx, anchor, _adapter = setup_update
    raw_a, manifest_a = portable_bundle('A')
    (old_tx.installed / 'wheelhouse/synthetic.whl').unlink()
    with zipfile.ZipFile(io.BytesIO(raw_a)) as source:
        source.extractall(old_tx.installed)
    install_owned_fixture(old_tx.installed)
    anchor.value = Anchor(1, sha256(raw_a).hexdigest(), old_tx.clock())
    raw_b, manifest_b = portable_bundle('B')
    package = download(repository, tmp_path / 'portable-cache', raw_b)
    calls = []

    def prepare(adapter, root, release):
        adapter._verify_release(root, release)
        calls.append(('prepare', root))

    def activate(adapter, root, release):
        adapter._verify_release(root, release)
        install_owned_fixture(root)
        calls.append(('activate', root))

    def health_run(command, *, cwd, env, timeout):
        assert installation._HEALTH_PROGRAM in command
        answer = subprocess.check_output(
            [sys.executable, '-I', '-B', str(cwd / 'program.py')], timeout=10)
        calls.append(('health', cwd))
        return subprocess.CompletedProcess(command, 0 if answer.strip() == b'B' else 1)

    monkeypatch.setattr(handoff._PinnedAdapter, 'prepare', prepare)
    monkeypatch.setattr(handoff._PinnedAdapter, 'activate', activate)
    monkeypatch.setattr(installation, '_run_owned', health_run)
    helper = handoff.PortableUpdateHandoff(
        app, old_tx.installed, old_tx.work, repository.policy(), anchor, installer_verify(),
        installed_manifest_sha256=manifest_a, clock=old_tx.clock)
    return helper, package, manifest_b, calls, setup_update


def assert_original(ready):
    helper, _package, _manifest, calls, original = ready
    assert (helper.program / 'version.txt').read_bytes() == b'A'
    assert helper.anchor.load().sequence == 1
    assert not [call for call in calls if call[0] == 'activate']
    assert_preserved(original)


def test_verified_portable_activation_and_recovery_preserve_real_reader_drafts(ready):
    helper, package, manifest_b, calls, original = ready
    result = helper.apply(package)
    assert result['status'] == 'complete'
    assert result['installed_manifest_sha256'] == manifest_b
    assert result['restart'] == 'not-requested'
    assert Path(result['backup']).is_dir()
    assert (Path(result['previous']) / 'version.txt').read_bytes() == b'A'
    assert (helper.program / 'version.txt').read_bytes() == b'B'
    assert helper.anchor.load().sequence == 2
    assert [kind for kind, _root in calls] == ['prepare', 'activate', 'health']
    assert_preserved(original)
    assert helper.recover(package) == 'complete'
    assert [kind for kind, _root in calls][-1] == 'health'
    assert_preserved(original)


@pytest.mark.parametrize('phase', ['old-moved', 'new-active'])
def test_external_recovery_keeps_authority_when_current_program_is_absent(ready, monkeypatch, phase):
    helper, package, _manifest, _calls, original = ready
    original_rename = os.rename

    class PowerLoss(BaseException):
        pass

    def interrupted(source, destination):
        original_rename(source, destination)
        if ((phase == 'old-moved' and Path(source) == helper.program)
                or (phase == 'new-active' and Path(destination) == helper.program)):
            raise PowerLoss()

    with monkeypatch.context() as patch:
        patch.setattr(os, 'rename', interrupted)
        with pytest.raises(PowerLoss):
            helper.apply(package)
    if phase == 'old-moved':
        assert not helper.program.exists()
    restarted = handoff.PortableUpdateHandoff(
        helper.app, helper.program, helper.work, helper.policy, helper.anchor, helper.verify_bundle,
        installed_manifest_sha256=helper.before, clock=helper.clock)
    assert restarted.recover(package) == 'rolled-back'
    assert (helper.program / 'version.txt').read_bytes() == b'A'
    assert helper.anchor.load().sequence == 1
    assert_preserved(original)


@pytest.mark.parametrize('field', ['source_commit', 'version', 'notes'])
def test_mutable_recovery_release_cannot_override_authenticated_package(ready, monkeypatch, field):
    helper, package, _manifest, _calls, original = ready
    original_write = handoff.UpdateTransaction._write

    class PowerLoss(BaseException):
        pass

    def interrupted(transaction, record, phase):
        original_write(transaction, record, phase)
        if phase == 'new-active':
            raise PowerLoss()

    with monkeypatch.context() as patch:
        patch.setattr(handoff.UpdateTransaction, '_write', interrupted)
        with pytest.raises(PowerLoss):
            helper.apply(package)
    journal = helper.work / 'activation.json'
    raw = journal.read_bytes()
    record = json.loads(raw)
    record['release'][field] = '9' * 40 if field == 'source_commit' else 'unapproved'
    journal.write_bytes(json_bytes(record))
    with pytest.raises(TrustError, match='authenticated package'):
        helper.recover(package)
    assert helper.anchor.load().sequence == 1
    assert (helper.program / 'version.txt').read_bytes() == b'B'
    assert_preserved(original)
    journal.write_bytes(raw)
    assert helper.recover(package) == 'rolled-back'
    assert (helper.program / 'version.txt').read_bytes() == b'A'
    assert_preserved(original)


def test_completed_journal_is_not_proof_without_protected_commit(ready, monkeypatch):
    helper, package, _manifest, _calls, original = ready
    original_write = handoff.UpdateTransaction._write

    class PowerLoss(BaseException):
        pass

    def interrupted(transaction, record, phase):
        original_write(transaction, record, phase)
        if phase == 'new-active':
            raise PowerLoss()

    with monkeypatch.context() as patch:
        patch.setattr(handoff.UpdateTransaction, '_write', interrupted)
        with pytest.raises(PowerLoss):
            helper.apply(package)
    journal = helper.work / 'activation.json'
    raw = journal.read_bytes()
    record = json.loads(raw)
    record['phase'] = 'complete'
    journal.write_bytes(json_bytes(record))
    with pytest.raises(TrustError, match='protected state'):
        helper.recover(package)
    assert helper._completed is None
    assert_preserved(original)
    journal.write_bytes(raw)
    assert helper.recover(package) == 'rolled-back'


def test_source_manifest_change_before_exclusive_admission_refuses_activation(ready):
    helper, package, _manifest, _calls, _original = ready
    path = helper.program / 'bundle-manifest.json'
    raw = path.read_bytes()
    record = json.loads(raw)
    record['source_commit'] = 'f' * 40
    path.write_bytes(json_bytes(record))
    with pytest.raises(TrustError, match='configured manifest identity'):
        helper.apply(package)
    assert_original(ready)
    path.write_bytes(raw)


@pytest.mark.parametrize('mutation', ['marker', 'unsealed', 'source'])
def test_even_signed_candidate_must_preserve_owned_entry_capability(ready, repository, tmp_path, mutation):
    helper, _package, _manifest, _calls, _original = ready
    options = {'marker': b'unsupported'} if mutation == 'marker' else (
        {'seal_marker': False} if mutation == 'unsealed' else {'source': 'f' * 40})
    raw, _ = portable_bundle('B', **options)
    package = download(repository, tmp_path / ('changed-' + mutation), raw)
    with pytest.raises(TrustError, match='child-ownership contract'):
        helper.apply(package)
    assert_original(ready)


def test_package_changed_after_manifest_snapshot_cannot_supply_activation_bytes(ready, monkeypatch):
    helper, package, _manifest, _calls, _original = ready
    original = handoff.candidate_manifest
    raw = package.path.read_bytes()

    def change_after_snapshot(*args, **kwargs):
        answer = original(*args, **kwargs)
        package.path.write_bytes(b'x' * len(raw))
        return answer

    monkeypatch.setattr(handoff, 'candidate_manifest', change_after_snapshot)
    with pytest.raises(TrustError, match='checksum'):
        helper.apply(package)
    assert_original(ready)


def test_cancellation_after_protected_commit_does_not_relabel_success(ready, monkeypatch):
    helper, package, _manifest, _calls, original = ready
    cancelled = []
    compare = helper.anchor.compare_and_set

    def publish(expected, replacement, **kwargs):
        compare(expected, replacement, **kwargs)
        cancelled.append(True)

    monkeypatch.setattr(helper.anchor, 'compare_and_set', publish)
    result = helper.apply(package, cancelled=lambda: bool(cancelled))
    assert result['status'] == 'complete'
    assert helper.anchor.load().sequence == 2
    assert (helper.program / 'version.txt').read_bytes() == b'B'
    assert_preserved(original)


def test_actual_shared_child_lease_refuses_update_without_staging_or_cancellation(ready, tmp_path):
    helper, package, _manifest, _calls, _original = ready
    root = portable_entry.control_root(helper.program, create=True)
    release = tmp_path / 'release-child'
    program = r'''
import pathlib, sys, time
sys.path.insert(0, sys.argv[3])
from carr_library.installation_lease import installation_lease
with installation_lease(pathlib.Path(sys.argv[1])):
    print('leased', flush=True)
    deadline = time.monotonic() + 15
    while not pathlib.Path(sys.argv[2]).exists():
        if time.monotonic() >= deadline:
            raise TimeoutError('Fixture release did not arrive')
        time.sleep(.02)
'''
    process = subprocess.Popen(
        [sys.executable, '-I', '-B', '-X', 'utf8', '-c', program, str(root), str(release),
         str(Path(leases.__file__).resolve().parents[1])],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == 'leased'
        with pytest.raises(RuntimeError, match='running jobs'):
            helper.apply(package)
        assert not list(helper.work.glob('stage-*'))
        assert not list(helper.work.glob('backup-*'))
        assert process.poll() is None
        assert_original(ready)
    finally:
        release.touch()
        try:
            _output, errors = process.communicate(timeout=10)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
    assert process.returncode == 0, errors


def test_managed_installer_metadata_cannot_be_left_behind_by_generic_activation(ready):
    helper, package, _manifest, _calls, _original = ready
    marker = helper.program.parent / leases.MARKER
    owner = helper.program.parent / 'owned-files.json'
    marker.write_bytes(b'Exact setup-A marker')
    owner.write_bytes(b'Exact setup-A ownership')
    with pytest.raises(TrustError, match='authenticated setup handoff'):
        helper.apply(package)
    assert marker.read_bytes() == b'Exact setup-A marker'
    assert owner.read_bytes() == b'Exact setup-A ownership'
    assert_original(ready)


def test_external_runtime_must_survive_the_program_directory_move(ready, monkeypatch):
    helper, package, _manifest, _calls, _original = ready
    executable = helper.program / 'runtime/python.exe'
    monkeypatch.setattr(handoff.sys, 'executable', str(executable))
    with pytest.raises(TrustError, match='outside program'):
        helper.apply(package)
    assert_original(ready)


def test_activation_status_never_hashes_or_authenticates_on_ui_thread(tmp_path, monkeypatch):
    prefix = tmp_path / 'program' / '.venv'
    prefix.mkdir(parents=True)
    marker = prefix.parent / portable_entry.PORTABLE_MARKER
    marker.write_bytes(portable_entry.PORTABLE_CONTRACT)
    monkeypatch.setattr(portable_entry, 'portable_contract',
                        lambda *_args: pytest.fail('UI status read the full bundle'))
    assert 'separately configured trusted helper' in portable_entry.activation_status(prefix)


def test_changed_installed_guard_refuses_source_ownership_claim(ready):
    helper, package, _manifest, _calls, _original = ready
    guard = helper.program / '.venv/Lib/site-packages/carr_library/portable_bootstrap.py'
    original = guard.read_bytes()
    guard.write_bytes(b'print("unowned legacy entry")\n')
    with pytest.raises(ValueError, match='ownership code differs'):
        helper.apply(package)
    assert_original(ready)
    guard.write_bytes(original)


def test_changed_candidate_guard_rolls_back_before_protected_commit(ready, monkeypatch):
    helper, package, _manifest, calls, original = ready
    activate = handoff._PinnedAdapter.activate

    def changed_after_install(adapter, root, release):
        activate(adapter, root, release)
        (root / '.venv/Lib/site-packages/carr_library/portable_bootstrap.py').write_bytes(b'changed')

    monkeypatch.setattr(handoff._PinnedAdapter, 'activate', changed_after_install)
    with pytest.raises(ValueError, match='ownership code differs'):
        helper.apply(package)
    assert helper.anchor.load().sequence == 1
    assert (helper.program / 'version.txt').read_bytes() == b'A'
    assert [kind for kind, _root in calls] == ['prepare', 'activate']
    assert_preserved(original)

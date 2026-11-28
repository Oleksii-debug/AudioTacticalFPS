"""Portable capability admission and real fixed health/public CLI boundaries."""
from hashlib import sha256
import importlib
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import sysconfig
import venv

import pytest

import carr_library
import bootstrap
from carr_library import installation_lease as leases, portable_entry
from carr_library.security import installation


def program(tmp_path):
    root = tmp_path / 'portable'
    root.mkdir()
    (root / '.venv').mkdir()
    (root / '.venv' / '.installation-complete').touch()
    marker = root / portable_entry.PORTABLE_MARKER
    marker.write_bytes(portable_entry.PORTABLE_CONTRACT)
    manifest = {'schema': 1, 'bundled_python': True, 'bundled_launcher': True,
                'source_commit': 'a' * 40,
                'files': {marker.name: sha256(marker.read_bytes()).hexdigest()}}
    (root / 'bundle-manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    return root


@pytest.mark.parametrize('change', ['unsealed', 'different-marker', 'source', 'unowned-launcher'])
def test_local_marker_cannot_admit_an_unsealed_or_unowned_bundle(tmp_path, change):
    root = program(tmp_path)
    path = root / 'bundle-manifest.json'
    value = json.loads(path.read_bytes())
    if change == 'unsealed':
        value['files'].clear()
    elif change == 'different-marker':
        (root / portable_entry.PORTABLE_MARKER).write_bytes(b'unsupported owner')
    elif change == 'source':
        value['source_commit'] = 'HEAD'
    else:
        value['bundled_launcher'] = False
    path.write_text(json.dumps(value), encoding='utf-8')
    with pytest.raises(ValueError):
        portable_entry.portable_contract(root)


def test_owned_capability_is_explicit_and_control_survives_program_move(tmp_path):
    root = program(tmp_path)
    raw = portable_entry.portable_contract(root)
    control = portable_entry.control_root(root, create=True)
    assert control.parent == root.parent
    assert not control.is_relative_to(root)
    with leases.installation_lease(control):
        with pytest.raises(RuntimeError, match='running jobs'):
            with leases.installation_lease(control, exclusive=True):
                pytest.fail('Shared app lease was ignored')
    previous = root.with_name('retained-A')
    root.rename(previous)
    with leases.installation_lease(control, exclusive=True):
        assert portable_entry.portable_contract(previous) == raw
    assert (control / leases.LEASE).is_file()


def test_application_context_cannot_bypass_portable_lifetime_owner(tmp_path):
    root = program(tmp_path)
    with pytest.raises(RuntimeError, match='lifetime owner'):
        with leases.application_lease(prefix=root / '.venv'):
            pytest.fail('Application work was admitted without an owner')


def test_exact_bootstrap_self_test_does_not_open_a_completed_public_bypass(tmp_path):
    root = program(tmp_path)
    ready = root / '.venv' / '.installation-complete'
    ready.unlink()
    with leases.application_lease(['--self-test'], prefix=root / '.venv',
                                  allow_bootstrap_self_test=True):
        pass
    for arguments in ([], ['--self-test', '--data-dir', str(tmp_path)], ['--backup', str(tmp_path)]):
        with pytest.raises(RuntimeError, match='incomplete'):
            with leases.application_lease(arguments, prefix=root / '.venv',
                                          allow_bootstrap_self_test=True):
                pytest.fail('A non-bootstrap operation bypassed readiness')
    ready.touch()
    with pytest.raises(RuntimeError, match='lifetime owner'):
        with leases.application_lease(['--self-test'], prefix=root / '.venv',
                                      allow_bootstrap_self_test=True):
            pytest.fail('The completed CLI bypassed lifetime ownership')


@pytest.mark.parametrize('name', ['installation_lease.py', 'portable_entry.py',
                                 'durable_state.py', 'windows_state.py',
                                 'audio/windows_process.py', 'native_checks.py', 'local_audio_setup.py', 'unchanged'])
def test_portable_builder_rejects_old_or_changed_ownership_sources(tmp_path, monkeypatch, name):
    import importlib.util
    import zipfile

    path = Path(__file__).resolve().parents[2] / 'packaging' / 'build_windows_bundle.py'
    spec = importlib.util.spec_from_file_location('portable_fixture_builder', path)
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    fixture_packaging = tmp_path / 'builder/packaging'
    fixture_packaging.mkdir(parents=True)
    package = fixture_packaging.parent / 'src/carr_library'
    loaded = Path(portable_entry.__file__).resolve().parent
    relatives = ('installation_lease.py', 'portable_entry.py', 'portable_bootstrap.py',
                 'durable_state.py', 'windows_state.py', 'audio/windows_process.py',
                 '__main__.py', 'native_checks.py', 'transcription_cli.py', 'security/__main__.py',
                 'studio/cli.py', 'extract/ocr_model_catalog.py', 'assistant/translation_cli.py',
                 'operations/cli.py', 'local_audio_setup.py')
    for relative in relatives:
        target = package / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(loaded / relative, target)
    (fixture_packaging / 'native-launcher').mkdir()
    shutil.copyfile(path.parent / 'native-launcher/launcher.c',
                    fixture_packaging / 'native-launcher/launcher.c')
    monkeypatch.setattr(builder, 'HERE', fixture_packaging)
    wheel = tmp_path / 'synthetic.whl'
    with zipfile.ZipFile(wheel, 'w') as archive:
        for relative in relatives:
            data = (package / relative).read_bytes()
            archive.writestr('carr_library/' + relative, b'# old entry' if relative == name else data)
    receipt = json.dumps({'source_sha256': {
        'launcher.c': builder.sha256(path.parent / 'native-launcher/launcher.c')}}).encode()
    if name == 'unchanged':
        builder.verify_portable_entry_sources(wheel, (b'not-executed', receipt))
    else:
        with pytest.raises(ValueError, match='ownership code differs'):
            builder.verify_portable_entry_sources(wheel, (b'not-executed', receipt))


def _copy_installed_fixture(root):
    """Actual local venv/package imports; no fake health result or CLI callback."""
    environment = root / '.venv'
    venv.EnvBuilder(with_pip=False).create(environment)
    executable = environment / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    site = Path(subprocess.check_output(
        [str(executable), '-I', '-B', '-c', 'import sysconfig; print(sysconfig.get_path("purelib"))'],
        text=True, timeout=20).strip())
    shutil.copytree(Path(carr_library.__file__).resolve().parent, site / 'carr_library',
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    shutil.copytree(Path(bootstrap.__file__).resolve().parent, site / 'bootstrap',
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    # Reuse the already provisioned dependency environment, with the new
    # installed package first. No resolver, network or shared-env mutation.
    dependencies = sorted({str(Path(item).resolve()) for item in sys.path
                           if item and Path(item).is_dir()
                           and Path(item).name in ('site-packages', 'dist-packages')})
    (site / 'fixture-dependencies.pth').write_text('\n'.join(dependencies) + '\n', encoding='utf-8')
    version = '0.2.0.dev1'
    metadata = site / ('universal_research_library-' + version + '.dist-info')
    metadata.mkdir()
    (metadata / 'METADATA').write_text(
        'Metadata-Version: 2.1\nName: universal-research-library\nVersion: ' + version + '\n',
        encoding='utf-8')
    (environment / '.installation-complete').touch()
    return executable, version


def test_private_health_runs_exact_synthetic_self_test_while_public_cli_remains_leased(tmp_path):
    managed = tmp_path / 'managed'
    root = managed / 'app'
    root.mkdir(parents=True)
    (managed / leases.MARKER).write_bytes(b'Fixture standard installation identity')
    executable, version = _copy_installed_fixture(root)
    with leases.installation_lease(managed, exclusive=True):
        public = subprocess.run([str(executable), '-I', '-B', '-X', 'utf8',
                                 '-m', 'carr_library', '--self-test'],
                                cwd=tmp_path, capture_output=True, text=True, timeout=20)
        assert public.returncode == 1
        assert 'running jobs' in public.stderr
        private = subprocess.run([str(executable), '-I', '-B', '-X', 'utf8',
                                  '-c', installation._HEALTH_PROGRAM, str(root), version],
                                 cwd=tmp_path, capture_output=True, text=True, timeout=40)
        assert private.returncode == 0, private.stderr
        mismatched = subprocess.run([str(executable), '-I', '-B', '-X', 'utf8',
                                     '-c', installation._HEALTH_PROGRAM, str(root), '9.9.9'],
                                    cwd=tmp_path, capture_output=True, text=True, timeout=20)
        assert mismatched.returncode != 0
        assert 'differs from the signed release' in mismatched.stderr
    assert not list(root.glob('library*'))


def test_public_native_check_uses_same_installed_lease(tmp_path, monkeypatch, capsys):
    from carr_library import native_checks
    managed = tmp_path / 'managed'
    root = managed / 'app'
    prefix = root / '.venv'
    prefix.mkdir(parents=True)
    (prefix / '.installation-complete').touch()
    (managed / leases.MARKER).write_bytes(b'fixture managed identity')
    monkeypatch.setattr(sys, 'prefix', str(prefix))
    monkeypatch.setattr(native_checks, 'check_native_dependencies',
                        lambda *_args: pytest.fail('Diagnostic bypassed the exclusive lease'))
    with leases.installation_lease(managed, exclusive=True):
        assert native_checks.main(['--bundle-root', str(root)]) == 1
    assert 'running jobs' in capsys.readouterr().err


def test_old_marker_only_profile_cannot_claim_receipt_based_ownership(tmp_path):
    root = program(tmp_path)
    marker = root / portable_entry.PORTABLE_MARKER
    marker.write_bytes(b'[Resonance portable updates]\nProtocol=1\nOwner=windows-job-v1\n')
    manifest = root / 'bundle-manifest.json'
    value = json.loads(manifest.read_bytes())
    value['files'][marker.name] = sha256(marker.read_bytes()).hexdigest()
    manifest.write_text(json.dumps(value), encoding='utf-8')
    with pytest.raises(ValueError, match='no supported child-ownership contract'):
        portable_entry.portable_contract(root)


def test_confirmed_owner_cleanup_removes_only_its_exact_receipt(tmp_path):
    root = program(tmp_path)
    control = portable_entry.control_root(root, create=True)
    with leases.installation_lease(control):
        first = portable_entry._record_owner(root, control)
        second = portable_entry._record_owner(root, control)
        portable_entry._clear_owner(first)
        assert not first[0].exists()
        assert second[0].read_bytes() == second[1]
    with leases.installation_lease(control, exclusive=True):
        with pytest.raises(RuntimeError, match='did not confirm complete cleanup'):
            portable_entry.require_clean_owners(root)
    with leases.installation_lease(control):
        portable_entry._clear_owner(second)
    with leases.installation_lease(control, exclusive=True):
        portable_entry.require_clean_owners(root)
    assert not list(control.glob('owner-*'))


def test_changed_cleanup_receipt_is_retained_instead_of_certifying_idle(tmp_path):
    root = program(tmp_path)
    control = portable_entry.control_root(root, create=True)
    with leases.installation_lease(control):
        receipt = portable_entry._record_owner(root, control)
        original = json.loads(receipt[1])
        assert original['owner_pid'] == os.getpid()
        changed = dict(original, owner_pid=os.getpid() + 1)
        receipt[0].write_text(json.dumps(changed), encoding='utf-8')
        with pytest.raises(RuntimeError, match='receipt changed'):
            portable_entry._clear_owner(receipt)
        assert json.loads(receipt[0].read_bytes()) == changed
    with leases.installation_lease(control, exclusive=True):
        with pytest.raises(RuntimeError, match='did not confirm complete cleanup'):
            portable_entry.require_clean_owners(root)

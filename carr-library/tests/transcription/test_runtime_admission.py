"""Read-only Windows-layout admission checks; no fake executable is launched."""
import shutil

import pytest

from carr_library.transcription import TranscriptionService, inspect_runtime
from carr_library.transcription import service as service_module


def windows_layout(runtime, *, system_site=False):
    environment = runtime.python.parent.parent
    scripts = environment / 'Scripts'
    scripts.mkdir(exist_ok=True)
    launcher = scripts / 'python.exe'
    launcher.write_bytes(b'Synthetic Windows venv launcher; never executable.')
    base = runtime.root / 'base-python'
    base.mkdir()
    for name in ('python.exe', 'python311.dll', 'vcruntime140.dll', 'python311._pth'):
        (base / name).write_bytes(b'Synthetic base runtime identity: ' + name.encode())
    (base / 'DLLs').mkdir()
    (base / 'DLLs' / '_ssl.pyd').write_bytes(b'Synthetic native runtime support.')
    (environment / 'pyvenv.cfg').write_text(
        f'home = {base}\ninclude-system-site-packages = {str(system_site).lower()}\n'
        'version = 3.11.18\nexecutable = unused-creation-hint\n', encoding='utf-8')
    return launcher, base


@pytest.mark.parametrize('relative', ['python.exe', 'python311.dll', 'vcruntime140.dll',
                                     'python311._pth', 'DLLs/_ssl.pyd'])
def test_changed_windows_base_bytes_revoke_review_before_execution(synthetic_asr_runtime, tmp_path,
                                                                  monkeypatch, relative):
    runtime = synthetic_asr_runtime
    launcher, base = windows_layout(runtime)
    service = TranscriptionService(tmp_path / 'library/state/transcription')
    inspected = inspect_runtime(launcher, runtime.worker, runtime.model)
    profile = service.register_runtime('Read-only synthetic Windows layout', launcher,
        runtime.worker, runtime.model, owner_trusted=True,
        expected_fingerprint=inspected['fingerprint'])
    source = tmp_path / 'source.wav'
    source.write_bytes(b'Synthetic source identity only.')
    job = service.create_job(profile['id'], [source])
    target = base / relative
    target.write_bytes(target.read_bytes() + b' changed')
    changed = inspect_runtime(launcher, runtime.worker, runtime.model)
    assert changed['python_identity'] == inspected['python_identity']
    assert changed['fingerprint'] != inspected['fingerprint']
    assert str(target) in changed['python_support']
    with pytest.raises(ValueError, match='exact current'):
        service.register_runtime('Stale review', launcher, runtime.worker, runtime.model,
            owner_trusted=True, expected_fingerprint=inspected['fingerprint'])
    launched = []
    def must_not_launch(*args, **kwargs):
        launched.append(True)
        raise AssertionError('The synthetic Windows launcher must never execute.')
    monkeypatch.setattr(service_module, 'run_process', must_not_launch)
    result = service.run(job['id'])
    assert result['status'] == 'failed' and 'changed' in result['error']
    assert launched == []


def test_windows_base_site_inventory_uses_operational_home(synthetic_asr_runtime):
    runtime = synthetic_asr_runtime
    launcher, base = windows_layout(runtime, system_site=True)
    base_site = base / 'Lib/site-packages'
    base_site.mkdir(parents=True)
    for path in runtime.site.iterdir():
        shutil.move(str(path), base_site / path.name)
    inspected = inspect_runtime(launcher, runtime.worker, runtime.model)
    assert len(inspected['runtime_distributions']) == 7
    assert str(base / 'python.exe') in inspected['python_support']


def test_missing_windows_base_executable_cannot_be_reviewed(synthetic_asr_runtime):
    runtime = synthetic_asr_runtime
    launcher, base = windows_layout(runtime)
    (base / 'python.exe').unlink()
    with pytest.raises(ValueError, match='base Python executable'):
        inspect_runtime(launcher, runtime.worker, runtime.model)


def symlink(path, target):
    try:
        path.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip('This host does not permit the final-executable symlink fixture.')


def test_unused_windows_executable_alias_is_not_an_execution_input(synthetic_asr_runtime):
    runtime = synthetic_asr_runtime
    launcher, base = windows_layout(runtime)
    before = inspect_runtime(launcher, runtime.worker, runtime.model)
    alias = base / 'python3.exe'
    symlink(alias, base / 'python.exe')
    linked = inspect_runtime(launcher, runtime.worker, runtime.model)
    assert linked['fingerprint'] == before['fingerprint']
    assert str(alias) not in linked['python_support']
    unused = runtime.root / 'unused-interpreter.exe'
    unused.write_bytes(b'Never selected or launched.')
    alias.unlink()
    symlink(alias, unused)
    unused.write_bytes(b'Unused executable bytes can change independently.')
    assert inspect_runtime(launcher, runtime.worker, runtime.model)['fingerprint'] == before['fingerprint']


@pytest.mark.parametrize('change', ['retarget', 'mutate'])
def test_selected_windows_executable_alias_is_pinned(synthetic_asr_runtime, tmp_path, monkeypatch, change):
    runtime = synthetic_asr_runtime
    launcher, base = windows_layout(runtime)
    alias = launcher.with_name('python3.exe')
    symlink(alias, launcher)
    service = TranscriptionService(tmp_path / 'library/state/transcription')
    inspected = inspect_runtime(alias, runtime.worker, runtime.model)
    profile = service.register_runtime('Reviewed selected executable alias', alias,
        runtime.worker, runtime.model, owner_trusted=True, expected_fingerprint=inspected['fingerprint'])
    audio = tmp_path / 'source.wav'
    audio.write_bytes(b'Synthetic source identity.')
    job = service.create_job(profile['id'], [audio])
    if change == 'retarget':
        replacement = launcher.with_name('different-interpreter.exe')
        replacement.write_bytes(launcher.read_bytes())
        alias.unlink()
        symlink(alias, replacement)
    else:
        launcher.write_bytes(launcher.read_bytes() + b' changed')
    current = inspect_runtime(alias, runtime.worker, runtime.model)
    assert current['fingerprint'] != inspected['fingerprint']
    if change == 'retarget':
        assert current['python_identity'] == inspected['python_identity']
        assert current['python_resolved'] != inspected['python_resolved']
    else:
        assert current['python_identity'] != inspected['python_identity']
    launched = []
    def must_not_launch(*args, **kwargs):
        launched.append(True)
        raise AssertionError('An altered selected executable must not execute.')
    monkeypatch.setattr(service_module, 'run_process', must_not_launch)
    result = service.run(job['id'])
    assert result['status'] == 'failed' and 'changed' in result['error']
    assert launched == []


def test_selected_alias_retargeted_during_inventory_is_rejected(synthetic_asr_runtime):
    runtime = synthetic_asr_runtime
    launcher, base = windows_layout(runtime)
    alias = launcher.with_name('python3.exe')
    symlink(alias, launcher)
    replacement = launcher.with_name('different-interpreter.exe')
    replacement.write_bytes(launcher.read_bytes())
    changed = []
    def progress(event):
        if not changed and event['phase'] == 'Verifying existing model files':
            alias.unlink()
            symlink(alias, replacement)
            changed.append(True)
    with pytest.raises(ValueError, match='executable link changed during inspection'):
        inspect_runtime(alias, runtime.worker, runtime.model, progress=progress)
    assert changed


@pytest.mark.parametrize('relative', ['python311.dll', 'DLLs/_ssl.pyd'])
def test_native_support_links_remain_rejected(synthetic_asr_runtime, relative):
    runtime = synthetic_asr_runtime
    launcher, base = windows_layout(runtime)
    support = base / relative
    replacement = runtime.root / 'outside-native-support'
    replacement.write_bytes(support.read_bytes())
    support.unlink()
    symlink(support, replacement)
    with pytest.raises(ValueError, match='regular local files'):
        inspect_runtime(launcher, runtime.worker, runtime.model)

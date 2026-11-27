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


def test_final_python_alias_is_pinned_to_its_same_directory_target(synthetic_asr_runtime):
    runtime = synthetic_asr_runtime
    launcher, base = windows_layout(runtime)
    alias = base / 'python3.exe'
    alias.symlink_to(base / 'python.exe')
    inspected = inspect_runtime(launcher, runtime.worker, runtime.model)
    record = inspected['python_support'][str(alias)]
    assert record['resolved'] == str(base / 'python.exe')
    assert record['sha256'] == inspected['python_support'][str(base / 'python.exe')]['sha256']
    (base / 'python.exe').write_bytes(b'Changed base interpreter identity.')
    changed = inspect_runtime(launcher, runtime.worker, runtime.model)
    assert changed['fingerprint'] != inspected['fingerprint']


def test_python_alias_cannot_escape_ordinary_base_directory(synthetic_asr_runtime):
    runtime = synthetic_asr_runtime
    launcher, base = windows_layout(runtime)
    outside = runtime.root / 'outside-python.exe'
    outside.write_bytes(b'Unreviewed external alias target.')
    (base / 'python3.exe').symlink_to(outside)
    with pytest.raises(ValueError, match='outside|base directory'):
        inspect_runtime(launcher, runtime.worker, runtime.model)


def test_executable_alias_exception_does_not_allow_native_dll_links(synthetic_asr_runtime):
    runtime = synthetic_asr_runtime
    launcher, base = windows_layout(runtime)
    alias = base / 'python312.dll'
    alias.symlink_to(base / 'python311.dll')
    with pytest.raises(ValueError, match='Python executable aliases'):
        inspect_runtime(launcher, runtime.worker, runtime.model)

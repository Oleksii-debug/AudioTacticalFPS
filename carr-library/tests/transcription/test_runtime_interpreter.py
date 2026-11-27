"""Real venv selection through the production isolated transcription worker."""
import ast
from hashlib import sha256
import os
from pathlib import Path
import subprocess
import venv

import pytest

from carr_library.application import LibraryApp


@pytest.mark.parametrize(('symlinks', 'directory_alias'),
                         [(False, False), (True, False), (False, True)] if os.name != 'nt' else [(False, False)])
def test_selected_venv_packages_and_executable_identity_survive_worker_isolation(tmp_path, symlinks, directory_alias):
    runtime = tmp_path / 'selected-runtime'
    venv.EnvBuilder(with_pip=False, symlinks=symlinks).create(runtime)
    python = runtime / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    package_path = Path(subprocess.check_output([str(python), '-I', '-B', '-c',
        "import sysconfig; print(sysconfig.get_path('purelib'))"], text=True).strip())
    assert package_path.is_relative_to(runtime)
    assert python.is_symlink() is symlinks
    selected = python
    if directory_alias:
        alias = tmp_path / 'runtime-alias'
        alias.symlink_to(runtime, target_is_directory=True)
        selected = alias / python.relative_to(runtime)
    (package_path / 'carr_venv_sentinel.py').write_text("VALUE = 'selected venv only'\n", encoding='utf-8')
    hook_output = tmp_path / 'unexpected-site-hook'
    hook = f"import pathlib; pathlib.Path({str(hook_output)!r}).write_text('site hook ran')\n"
    (package_path / 'sitecustomize.py').write_text(hook, encoding='utf-8')
    (package_path / 'unexpected.pth').write_text(hook, encoding='utf-8')

    # Reuse the existing complete receipt fixture, without importing test code
    # or adding an alternative protocol implementation.
    fixture = ast.parse(Path(__file__).with_name('test_supplied_worker_bridge.py').read_text(encoding='utf-8'))
    source = next(ast.literal_eval(node.value) for node in fixture.body
                  if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name)
                      and target.id == 'SYNTHETIC_WORKER' for target in node.targets))
    worker = tmp_path / 'synthetic-worker.py'
    worker.write_text("from carr_venv_sentinel import VALUE\nassert VALUE == 'selected venv only'\n"
                      + source.replace('__MODE__', repr('complete')), encoding='utf-8')
    models = tmp_path / 'models'
    models.mkdir()
    original = tmp_path / 'recording.wav'
    original.write_bytes(b'Synthetic receipt fixture; no acoustic inference.')
    app = LibraryApp(tmp_path / 'library')
    service = app.transcription(app.create_workspace('Selected venv'))
    configured = service.configure(selected, worker, models)
    identity = service.submit([original])

    completed = service.run(identity, timeout=20)

    assert completed['status'] == 'completed', completed.get('last_error')
    assert completed['done'] == 1 and len(completed['artifacts']) == 6
    assert Path(configured['python']) == python
    assert service.configuration(verify=True)['python'] == str(python)
    assert configured['python_sha256'] == sha256(python.read_bytes()).hexdigest()
    assert not hook_output.exists()
    assert not tuple(package_path.rglob('*.pyc'))
    assert original.read_bytes() == b'Synthetic receipt fixture; no acoustic inference.'

    # Replacing the selected binary must still fail before launching a worker.
    replacement = tmp_path / 'changed-python'
    replacement.write_bytes(python.read_bytes() + b'\nchanged after selection\n')
    if symlinks:
        python.unlink()
        python.symlink_to(replacement)
    else:
        python.write_bytes(replacement.read_bytes())
    waiting = service.submit([original])
    with pytest.raises(ValueError, match='runtime changed'):
        service.run(waiting, timeout=20)
    assert service.get(waiting)['status'] == 'ready'
    assert not (service._path(waiting) / 'job_config.json').exists()

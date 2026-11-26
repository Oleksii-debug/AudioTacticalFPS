"""Canonical launcher/installer integration without executing Windows DLLs."""
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from carr_library import __main__ as launcher
from carr_library import native_runtime


ROOT = Path(__file__).resolve().parents[2]


def installer():
    spec = importlib.util.spec_from_file_location('native_installer_test', ROOT / 'packaging/install_windows.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('fails', [False, True])
def test_launcher_retains_native_handle_until_work_finishes(monkeypatch, fails):
    events = []

    class Handle:
        def close(self):
            events.append('close')

    def activate():
        events.append('activate')
        return Handle()

    def operation():
        assert events == ['activate']
        events.append('operation')
        if fails:
            raise RuntimeError('Synthetic application failure')
        return {'status': 'PASS'}

    monkeypatch.setattr(native_runtime, 'activate_bundled_runtime', activate)
    monkeypatch.setattr(launcher, 'self_test', operation)
    assert launcher.main(['--self-test']) == int(fails)
    assert events == ['activate', 'operation', 'close']


def test_bad_native_runtime_prevents_application_start(monkeypatch):
    def rejected():
        raise ValueError('Native runtime checksum failed')

    monkeypatch.setattr(native_runtime, 'activate_bundled_runtime', rejected)
    monkeypatch.setattr(launcher, 'self_test', lambda: pytest.fail('Application must not start'))
    assert launcher.main(['--self-test']) == 1


@pytest.fixture
def bundle(tmp_path):
    for name in ('install.lock', 'install_windows.py', 'Install.cmd', 'ResearchLibrary.cmd',
                 'CheckInstallation.cmd', 'README.txt', 'wheelhouse/fixture.whl',
                 'runtime/python.exe', 'native-runtime/native-manifest.json',
                 'native-runtime/bin/msvcp140.dll', 'native-runtime/bin/msvcp140_1.dll'):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'Synthetic inventory only, never executed')
    manifest = {'schema': 1, 'bundled_python': True, 'bundled_native_runtime': True,
                'files': {p.relative_to(tmp_path).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                          for p in tmp_path.rglob('*') if p.is_file()}}
    (tmp_path / 'bundle-manifest.json').write_text(json.dumps(manifest))
    return tmp_path, manifest


def test_installer_preserves_canonical_native_inventory_before_loading_anything(bundle):
    root, _ = bundle
    installer().verify(root)
    (root / 'native-runtime/bin/unlisted.dll').write_bytes(b'Unlisted dependency')
    with pytest.raises(ValueError, match='unexpected or missing native'):
        installer().verify(root)


@pytest.mark.parametrize('flag', [False, 'true'])
def test_installer_rejects_unannounced_or_invalid_native_flag(bundle, flag):
    root, manifest = bundle
    manifest['bundled_native_runtime'] = flag
    (root / 'bundle-manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='native runtime'):
        installer().verify(root)


def test_installer_requires_both_reviewed_dll_names_even_if_outer_manifest_is_rewritten(bundle):
    root, manifest = bundle
    name = 'native-runtime/bin/msvcp140_1.dll'
    (root / name).unlink()
    del manifest['files'][name]
    (root / 'bundle-manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='unexpected or missing native'):
        installer().verify(root)

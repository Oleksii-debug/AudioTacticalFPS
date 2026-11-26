"""The installed optional-runtime check executes actual PyCA encryption."""
import importlib
import json
from types import SimpleNamespace

from carr_library import native_checks


def test_native_profile_diagnostic_runs_actual_authenticated_encryption(tmp_path, monkeypatch):
    manifest = {'schema': 1, 'target': native_checks.TARGET, 'bundled_python': True,
        'bundled_model': False, 'bundled_ocr': False, 'bundled_encryption': True,
        'files': {'README.txt': 'a' * 64}}
    (tmp_path / 'bundle-manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    events = []

    def load(name):
        if name == 'carr_library.native_runtime':
            return SimpleNamespace(activate_bundled_runtime=lambda **kwargs: None)
        if name in {module for _, module in native_checks.NATIVE_MODULES}:
            return SimpleNamespace()  # This test does not claim Windows DLL execution.
        events.append(name)
        return importlib.import_module(name)

    monkeypatch.setattr(native_checks, 'import_module', load)
    report = native_checks.check_native_dependencies(tmp_path)
    assert report['status'] == 'PASS'
    assert report['encryption'] == {'status': 'PASS', 'aes256_gcm_roundtrip': 'PASS',
                                    'changed_ciphertext_rejected': True}
    assert events == ['cryptography.hazmat.primitives.ciphers.aead', 'cryptography.exceptions']

"""Pinned runtime inventory checks; no Windows execution is implied."""
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path

import pytest


SOURCE = Path(__file__).resolve().parents[2] / 'packaging' / 'windows_ocr.py'
spec = importlib.util.spec_from_file_location('windows_ocr_verifier', SOURCE)
ocr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ocr)


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    root = tmp_path / 'runtime'
    executable = root / 'bin' / 'tesseract.exe'
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b'MZ synthetic fixture, never executed')
    expected = {'schema': 1, 'engine': {'version': 'synthetic-test'}, 'files': {
        'bin/tesseract.exe': {'size': executable.stat().st_size,
                              'sha256': sha256(executable.read_bytes()).hexdigest()}}}
    manifest = tmp_path / 'runtime.json'
    manifest.write_text(json.dumps(expected), encoding='utf-8')
    approved = tmp_path / 'approved.json'
    approved.write_text(json.dumps(expected), encoding='utf-8')
    monkeypatch.setattr(ocr, 'APPROVED_MANIFEST', approved)
    return root, manifest, executable


def test_accepts_exact_reviewed_bytes_without_executing_them(runtime):
    root, manifest, _ = runtime
    result = ocr.verify_runtime(root, manifest)
    assert result['files'] == 1 and result['windows_execution'] == 'NOT_RUN'


@pytest.mark.parametrize('change', ['extra', 'missing', 'same_size_tamper', 'invented_provenance'])
def test_changes_cannot_redefine_the_approved_runtime(runtime, change):
    root, manifest, executable = runtime
    if change == 'extra':
        (root / 'injected.dll').write_bytes(b'unapproved')
    elif change == 'missing':
        executable.unlink()
    elif change == 'same_size_tamper':
        executable.write_bytes(b'x' * executable.stat().st_size)
    else:
        value = json.loads(manifest.read_text())
        value['engine']['version'] = 'unreviewed'
        manifest.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        ocr.verify_runtime(root, manifest)


@pytest.mark.parametrize('kind', ['file_symlink', 'root_symlink', 'hardlink'])
def test_linked_runtime_paths_are_rejected(runtime, kind):
    root, manifest, executable = runtime
    outside = root.parent / 'outside.exe'
    outside.write_bytes(executable.read_bytes())
    if kind == 'root_symlink':
        alias = root.parent / 'alias'
        alias.symlink_to(root, target_is_directory=True)
        root = alias
    else:
        executable.unlink()
        if kind == 'file_symlink':
            executable.symlink_to(outside)
        else:
            os.link(outside, executable)
    with pytest.raises(ValueError):
        ocr.verify_runtime(root, manifest)


@pytest.mark.skipif(not os.environ.get('CARR_TEST_OCR_DIR'), reason='staged Windows OCR runtime not supplied')
def test_actual_windows_runtime_all_files_match_reviewed_manifest():
    result = ocr.verify_runtime(os.environ['CARR_TEST_OCR_DIR'], SOURCE.with_name('windows-ocr.json'))
    assert result['engine']['version'] == '5.5.4'
    assert result['files'] > 40



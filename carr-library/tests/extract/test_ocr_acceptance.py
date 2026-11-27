"""The real source acceptance engine is explicit and independent of PATH."""
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil

import pytest

from bootstrap.ocr_acceptance import prepared_ocr
from carr_library.extract.ocr_runtime import OcrRuntimeError, _native_target


def test_prepared_verified_bundle_is_used_with_empty_path(tmp_path, monkeypatch):
    original, source = prepared_ocr(monkeypatch)
    supplied = os.environ.get('CARR_CI_OCR_DIR')
    if supplied:
        bundle = Path(supplied)
    else:
        bundle = tmp_path / 'prepared bundle Україна'
        (bundle / 'tessdata').mkdir(parents=True)
        executable = Path(original.executable)
        shutil.copy2(executable, bundle / executable.name)
        if os.name == 'nt':
            for dependency in executable.parent.glob('*.dll'):
                shutil.copy2(dependency, bundle / dependency.name)
        for language in ('eng', 'osd'):
            shutil.copyfile(source / (language + '.traineddata'), bundle / 'tessdata' / (language + '.traineddata'))
        files = {path.relative_to(bundle).as_posix(): {'size': path.stat().st_size,
                 'sha256': sha256(path.read_bytes()).hexdigest()}
                 for path in bundle.rglob('*') if path.is_file()}
        (bundle / 'ocr-manifest.json').write_text(json.dumps({
            'schema': 1, 'target': _native_target(), 'executable': executable.name,
            'tessdata': 'tessdata', 'files': files}), encoding='utf-8')
    monkeypatch.setenv('CARR_CI_OCR_DIR', str(bundle))
    monkeypatch.delenv('CARR_OCR_DIR', raising=False)
    monkeypatch.setenv('PATH', str(tmp_path / 'no-global-ocr'))
    checked, models = prepared_ocr(monkeypatch)
    assert checked.portable and checked.root == bundle.resolve()
    assert models == checked.tessdata
    assert os.environ['CARR_OCR_DIR'] == str(bundle)


def test_bad_explicit_ci_bundle_fails_without_falling_back_to_path(tmp_path, monkeypatch):
    missing = tmp_path / 'missing-prepared-bundle'
    monkeypatch.setenv('CARR_CI_OCR_DIR', str(missing))
    with pytest.raises(OcrRuntimeError):
        prepared_ocr(monkeypatch)
    assert not missing.exists()

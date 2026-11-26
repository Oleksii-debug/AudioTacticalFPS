"""OCR delivery boundaries; synthetic executable bytes are never executed here."""
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

HERE = Path(__file__).parent
spec = importlib.util.spec_from_file_location('base_windows_bundle_tests', HERE / 'test_windows_bundle.py')
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)
builder, installer = base.builder, base.installer


@pytest.fixture
def inputs(tmp_path):
    wheels = tmp_path / 'wheels'
    wheels.mkdir()
    dep = base.wheel(wheels, 'fixture')
    lock = tmp_path / 'windows.lock'
    lock.write_text(f'fixture==1.0 --hash=sha256:{builder.sha256(dep)}\n')
    app = base.wheel(tmp_path, 'universal_research_library', requires=['fixture==1.0'], entrypoint=True)
    ocr = tmp_path / 'OCR Ω'
    (ocr / 'tessdata').mkdir(parents=True)
    for name, data in {'tesseract.exe': b'MZ synthetic test fixture',
                       'helper.dll': b'MZ synthetic dependency',
                       'tessdata/eng.traineddata': b'synthetic model',
                       'LICENSE.txt': b'Synthetic test fixture only'}.items():
        (ocr / name).write_bytes(data)
    files = {p.relative_to(ocr).as_posix(): {'size': p.stat().st_size, 'sha256': builder.sha256(p)}
             for p in ocr.rglob('*') if p.is_file()}
    manifest = {'schema': 1, 'target': 'win_amd64', 'executable': 'tesseract.exe',
                'tessdata': 'tessdata', 'files': files}
    (ocr / 'ocr-manifest.json').write_text(json.dumps(manifest))
    return dict(wheel=app, wheelhouse=wheels, lock=lock, output=tmp_path / 'offline', ocr_dir=ocr)


def test_verified_ocr_runtime_and_models_are_delivered(inputs):
    builder.build_bundle(**inputs)
    output = inputs['output']
    manifest = json.loads((output / 'bundle-manifest.json').read_text())
    assert manifest['bundled_ocr'] is True
    assert (output / 'ocr/tesseract.exe').read_bytes() == (inputs['ocr_dir'] / 'tesseract.exe').read_bytes()
    assert 'CARR_OCR_DIR' in (output / 'ResearchLibrary.cmd').read_text()
    # The combined diagnostic loads native dependencies and delegates bundled
    # recognition to the same canonical OCR checker; it does not run it twice.
    assert 'carr_library.native_checks --bundle-root' in (output / 'CheckInstallation.cmd').read_text()
    installer.verify(output)


def test_runtime_tamper_rejected_before_output(inputs):
    (inputs['ocr_dir'] / 'helper.dll').write_bytes(b'changed')
    with pytest.raises(ValueError, match='checksum|size'):
        builder.build_bundle(**inputs)
    assert not inputs['output'].exists()


def test_extra_delivered_dll_is_not_silently_admitted(inputs):
    builder.build_bundle(**inputs)
    (inputs['output'] / 'ocr/unexpected.dll').write_bytes(b'extra')
    with pytest.raises(ValueError, match='OCR runtime'):
        installer.verify(inputs['output'])


def test_unlisted_linked_ocr_directory_rejected_during_install_preflight(inputs):
    builder.build_bundle(**inputs)
    outside = inputs['output'].parent / 'external-runtime-files'
    outside.mkdir()
    (outside / 'unapproved.dll').write_bytes(b'synthetic unapproved payload')
    try:
        (inputs['output'] / 'ocr/unlisted-directory').symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip('This environment does not permit creating symbolic links')
    with pytest.raises(ValueError, match='linked|reparse|OCR runtime'):
        installer.verify(inputs['output'])


def test_ocr_requires_windows_target(inputs):
    path = inputs['ocr_dir'] / 'ocr-manifest.json'
    manifest = json.loads(path.read_text())
    manifest['target'] = 'linux_x86_64'
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='Windows x64'):
        builder.build_bundle(**inputs)
    assert not inputs['output'].exists()


def test_corresponding_sources_are_required_checked_and_delivered(inputs):
    source = inputs['output'].parent / 'source.tar'
    source.write_bytes(b'synthetic corresponding-source archive')
    path = inputs['ocr_dir'] / 'ocr-manifest.json'
    manifest = json.loads(path.read_text())
    manifest['provenance'] = {'corresponding_source': {
        'filename': 'ocr-corresponding-source.tar', 'size': source.stat().st_size,
        'sha256': hashlib.sha256(source.read_bytes()).hexdigest()}}
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='requires its corresponding-source'):
        builder.build_bundle(**inputs)
    original = source.read_bytes()
    source.write_bytes(b'tampered')
    with pytest.raises(ValueError, match='checksum mismatch'):
        builder.build_bundle(**inputs, ocr_source_archive=source)
    source.write_bytes(original)
    builder.build_bundle(**inputs, ocr_source_archive=source)
    assert (inputs['output'] / 'ocr-corresponding-source.tar').read_bytes() == original
    installer.verify(inputs['output'])

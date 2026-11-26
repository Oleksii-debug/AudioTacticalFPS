"""Managed OCR command/provenance boundaries and actual installed English OCR.

Synthetic runtime inventories below test binding only. The separately named
real-engine cases use installed official model bytes and actual Tesseract.
"""
from hashlib import sha1, sha256
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest

from carr_library.extract.managed_ocr import ManagedOcrRuntime, bind_model_profile
from carr_library.extract.model_catalog import REVISIONS, models
from carr_library.extract.ocr_model_store import ModelSelection, OcrModelStore
from carr_library.extract.ocr_runtime import OcrRuntime, OcrRuntimeError, verify_ocr_bundle


@pytest.fixture
def selected(tmp_path):
    base = OcrRuntime('local-tesseract', tessdata=tmp_path / 'base models',
                      target='linux_x86_64', root=tmp_path / 'base engine')
    selection = ModelSelection(tmp_path / 'managed models', tmp_path / 'managed models/tessdata',
                               'fast', REVISIONS['fast'], (('eng', 'a' * 64), ('osd', 'b' * 64)))
    return ManagedOcrRuntime(base, selection)


def test_installed_profile_is_exact_passthrough_without_store_access(monkeypatch):
    monkeypatch.setenv('CARR_OCR_MODELS_DIR', 'invalid relative folder')
    base = OcrRuntime('local-tesseract')
    assert bind_model_profile(base, 'installed', 'eng') is base
    assert bind_model_profile(None, 'installed', 'eng') is None


def test_managed_wrapper_retains_base_runtime_identity(selected):
    assert selected.executable == selected.runtime.executable
    assert selected.root == selected.runtime.root
    assert selected.target == selected.runtime.target
    assert selected.managed is True and selected.portable is False
    assert selected.tessdata == selected.selection.directory
    assert selected.languages == ('eng', 'osd')
    fingerprint = selected.fingerprint()
    assert fingerprint['managed_models'] == selected.selection.fingerprint()
    for key, value in selected.runtime.fingerprint().items():
        assert fingerprint[key] == value
    assert str(selected.selection.root) not in json.dumps(fingerprint)
    assert str(selected.root) not in json.dumps(fingerprint)


def test_command_binds_managed_directory_and_lstm_without_mutating_input(selected):
    original = [selected.executable, 'image.png', 'stdout', '-l', 'eng']
    result = selected.command(original)
    assert result[:5] == [selected.executable, '--oem', '1', '--tessdata-dir', str(selected.tessdata)]
    assert result[5:] == original[1:]
    assert original == [selected.executable, 'image.png', 'stdout', '-l', 'eng']
    assert str(selected.runtime.tessdata) not in result


@pytest.mark.parametrize('mode', ['0', '1', '3'])
def test_explicit_engine_mode_and_staging_are_preserved(selected, tmp_path, mode):
    staging = str(tmp_path / 'separately verified staging')
    original = [selected.executable, 'image.png', 'stdout', '--oem', mode,
                '--tessdata-dir', staging, '-l', 'osd' if mode == '0' else 'eng']
    assert selected.command(original) == original


@pytest.mark.parametrize('flag', ['--version', '--list-langs', '--help', '--help-extra',
                                 '--help-psm', '--help-oem', '--print-parameters'])
def test_informational_commands_do_not_receive_recognition_engine_mode(selected, flag):
    result = selected.command([selected.executable, flag])
    assert '--oem' not in result
    if flag == '--version':
        assert result == [selected.executable, flag]
    else:
        assert result == [selected.executable, '--tessdata-dir', str(selected.tessdata), flag]


def test_require_languages_checks_managed_selection_even_for_nonportable_engine(selected):
    selected.require_languages('eng', orientation=True)
    selected.require_languages('osd+eng')
    for language in ('ukr', 'script/Latin', 'eng+../osd'):
        with pytest.raises(OcrRuntimeError):
            selected.require_languages(language)
    no_orientation = ManagedOcrRuntime(selected.runtime,
        ModelSelection(selected.selection.root, selected.tessdata, 'fast', REVISIONS['fast'],
                       (('eng', 'a' * 64),)))
    with pytest.raises(OcrRuntimeError, match='orientation'):
        no_orientation.require_languages('eng', orientation=True)


def test_missing_or_invalid_profile_is_an_ocr_boundary_error(tmp_path, monkeypatch):
    monkeypatch.setenv('CARR_OCR_MODELS_DIR', str(tmp_path / 'no models'))
    for profile in ('fast', 'best', 'unknown'):
        with pytest.raises(OcrRuntimeError):
            bind_model_profile(OcrRuntime('tesseract'), profile, 'eng')
    with pytest.raises(OcrRuntimeError, match='No local OCR engine'):
        bind_model_profile(None, 'fast', 'eng')


@pytest.fixture
def real_models(tmp_path, monkeypatch):
    executable = shutil.which('tesseract')
    if executable is None:
        pytest.skip('Actual Tesseract is unavailable; no inference result is claimed.')
    report = subprocess.run([executable, '--list-langs'], check=True, capture_output=True,
                            timeout=10).stdout.decode('utf-8')
    match = re.search(r'^List of available languages in "([^"\r\n]+)"', report, re.MULTILINE)
    if match is None:
        pytest.skip('Actual installed Tesseract model directory is unavailable.')
    source = Path(match[1])
    table = {model.identifier: model for model in models('fast')}
    for language in ('eng', 'osd'):
        path = source / (language + '.traineddata')
        if not path.is_file():
            pytest.skip(f'Actual local {language} model is unavailable.')
        raw = path.read_bytes()
        blob = sha1(b'blob ' + str(len(raw)).encode('ascii') + b'\0' + raw).hexdigest()
        if len(raw) != table[language].size or blob != table[language].blob_sha1:
            pytest.skip(f'Installed {language} model is not the pinned official fast revision.')
    root = tmp_path / 'managed models Україна'
    monkeypatch.setenv('CARR_OCR_MODELS_DIR', str(root))
    store = OcrModelStore()
    report = store.install('fast', source, ('eng', 'osd'))
    assert report.installed == ('eng', 'osd') and report.bytes > 0
    base = OcrRuntime(executable, tessdata=tmp_path / 'wrong base models', root=tmp_path)
    return bind_model_profile(base, 'fast', 'eng', orientation=True), store


def test_real_selected_bytes_execute_even_with_wrong_base_directory_and_environment(real_models, tmp_path, monkeypatch):
    from PIL import Image, ImageDraw, ImageFont
    runtime, store = real_models
    monkeypatch.setenv('TESSDATA_PREFIX', str(tmp_path / 'wrong environment models'))
    image = Image.new('RGB', (1600, 320), 'white')
    ImageDraw.Draw(image).text((40, 80), 'Managed research library', fill='black',
                              font=ImageFont.load_default(size=65))
    path = tmp_path / 'synthetic scan.png'
    image.save(path, dpi=(300, 300))
    before = sha256(path.read_bytes()).hexdigest()
    runtime.assert_unchanged()
    result = subprocess.run(runtime.command([runtime.executable, str(path), 'stdout',
        '-l', 'eng', '--psm', '6']), check=True, capture_output=True, timeout=20, cwd=runtime.root)
    assert result.stdout.decode('utf-8').strip() == 'Managed research library'
    runtime.assert_unchanged()
    assert sha256(path.read_bytes()).hexdigest() == before
    assert runtime.selection == store.select('fast', 'eng', orientation=True)


def test_real_list_languages_uses_managed_model_directory(real_models, monkeypatch, tmp_path):
    runtime, _ = real_models
    monkeypatch.setenv('TESSDATA_PREFIX', str(tmp_path / 'missing'))
    result = subprocess.run(runtime.command([runtime.executable, '--list-langs']),
                            check=True, capture_output=True, timeout=10)
    report = result.stdout.decode('utf-8')
    assert str(runtime.tessdata) in report and '\neng\n' in report and '\nosd\n' in report


def test_real_pdf_managed_profile_preserves_review_boundary_and_exact_provenance(real_models, monkeypatch, tmp_path):
    from PIL import Image, ImageDraw, ImageFont
    from carr_library.extract.page_ocr import Config, extract_page
    from carr_library.extract.runtime_identity import runtime_identity
    runtime, _ = real_models
    monkeypatch.delenv('CARR_OCR_DIR', raising=False)
    monkeypatch.setenv('TESSDATA_PREFIX', str(tmp_path / 'wrong environment models'))
    image = Image.new('RGB', (1600, 400), 'white')
    ImageDraw.Draw(image).text((40, 100), 'Managed source evidence', fill='black',
                              font=ImageFont.load_default(size=65))
    source = tmp_path / 'managed source.pdf'
    image.save(source, resolution=300)
    original_hash = sha256(source.read_bytes()).hexdigest()
    config = Config(model_profile='fast')
    identity = runtime_identity(config)
    result = extract_page(source, 1, config)
    assert result['raw_text'].strip() == 'Managed source evidence'
    assert result['status'] == 'review_required' and result['blocks']
    assert result['physical_index'] == 1 and result['printed_label'] is None
    assert result['printed_label_verified'] is False
    assert 'ocr_not_verified' in result['warnings']
    actual = result['provenance']['ocr_runtime']['managed_models']
    assert actual == identity['ocr']['managed_models']
    assert actual['model_sha256'] == {'eng': dict(runtime.selection.model_sha256)['eng']}
    assert result['provenance']['asset_sha256'] == original_hash == sha256(source.read_bytes()).hexdigest()
    assert str(tmp_path) not in json.dumps(identity)


@pytest.mark.parametrize('change', ['same_size_bytes', 'missing_model', 'changed_license',
                                   'directory_symlink'])
def test_real_model_tampering_is_translated_and_never_falls_back(real_models, tmp_path, change):
    runtime, _ = real_models
    path = runtime.tessdata / 'eng.traineddata'
    if change == 'same_size_bytes':
        raw = bytearray(path.read_bytes()); raw[-1] ^= 1; path.write_bytes(raw)
    elif change == 'missing_model':
        path.unlink()
    elif change == 'changed_license':
        (runtime.tessdata.parent / 'LICENSE').write_text('changed notice', encoding='utf-8')
    else:
        moved = tmp_path / 'moved verified models'
        runtime.tessdata.rename(moved)
        if os.name == 'nt':
            # A real directory reparse point needs no optional symlink mode.
            import _winapi
            _winapi.CreateJunction(str(moved), str(runtime.tessdata))
        else:
            runtime.tessdata.symlink_to(moved, target_is_directory=True)
    for operation in (runtime.assert_unchanged,
                      lambda: bind_model_profile(runtime.runtime, 'fast', 'eng', orientation=True)):
        with pytest.raises(OcrRuntimeError) as error:
            operation()
        assert str(tmp_path) not in str(error.value)


def test_verified_base_manifest_remains_authoritative(real_models, tmp_path):
    managed, _ = real_models
    root = tmp_path / 'base inventory'
    (root / 'tessdata').mkdir(parents=True)
    (root / 'tesseract.exe').write_bytes(b'synthetic engine inventory; never executed')
    (root / 'tessdata/eng.traineddata').write_bytes(b'synthetic base model; never executed')
    (root / 'dependency.dll').write_bytes(b'initial component')
    files = {p.relative_to(root).as_posix(): {'size': p.stat().st_size,
              'sha256': sha256(p.read_bytes()).hexdigest()} for p in root.rglob('*') if p.is_file()}
    (root / 'ocr-manifest.json').write_text(json.dumps({'schema': 1, 'target': 'win_amd64',
        'executable': 'tesseract.exe', 'tessdata': 'tessdata', 'files': files}), encoding='utf-8')
    runtime = bind_model_profile(verify_ocr_bundle(root), 'fast', 'eng')
    assert runtime.portable is True
    assert runtime.manifest_sha256 == runtime.runtime.manifest_sha256
    assert runtime.files_sha256 == runtime.runtime.files_sha256
    runtime.assert_unchanged()
    (root / 'dependency.dll').write_bytes(b'changed component')
    with pytest.raises(OcrRuntimeError, match='checksum'):
        runtime.assert_unchanged()


def test_remaining_budget_is_forwarded_and_timeout_is_not_relabelled(real_models):
    runtime, _ = real_models
    def expired():
        raise TimeoutError('OCR deadline reached')
    with pytest.raises(TimeoutError, match='deadline'):
        bind_model_profile(runtime.runtime, 'fast', 'eng', remaining=expired)
    with pytest.raises(TimeoutError, match='deadline'):
        runtime.assert_unchanged(expired)

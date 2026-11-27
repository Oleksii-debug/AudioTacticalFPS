"""Portable runtime integrity and real local inference without PATH installation."""
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest

from carr_library.extract import ocr_runtime as runtime


def write_manifest(root, *, target='win_amd64', executable='tesseract.exe'):
    files = {path.relative_to(root).as_posix(): {
        'size': path.stat().st_size, 'sha256': sha256(path.read_bytes()).hexdigest()}
        for path in root.rglob('*') if path.is_file() and path.name != runtime.MANIFEST}
    manifest = {'schema': 1, 'target': target, 'executable': executable,
                'tessdata': 'tessdata', 'files': files,
                'version': 'synthetic inventory', 'provenance': {'fixture': True}}
    (root / runtime.MANIFEST).write_text(json.dumps(manifest, sort_keys=True), encoding='utf-8')
    return manifest


@pytest.fixture(autouse=True)
def isolated_discovery(monkeypatch, tmp_path):
    monkeypatch.delenv('CARR_OCR_DIR', raising=False)
    monkeypatch.setattr(runtime.sys, 'prefix', str(tmp_path / 'interpreter'))
    monkeypatch.setattr(runtime.sys, 'base_prefix', str(tmp_path / 'base'))


@pytest.fixture
def bundle(tmp_path):
    root = tmp_path / 'Research library ü' / 'ocr'
    (root / 'tessdata').mkdir(parents=True)
    (root / 'tesseract.exe').write_bytes(b'structural fixture, never executed')
    (root / 'dependency.dll').write_bytes(b'checked dependency')
    (root / 'LICENSE.txt').write_text('Synthetic license fixture', encoding='utf-8')
    (root / 'tessdata' / 'eng.traineddata').write_bytes(b'checked model fixture')
    write_manifest(root)
    return root


def test_complete_inventory_and_path_free_fingerprint(bundle):
    checked = runtime.verify_ocr_bundle(bundle)
    assert checked.executable == str(bundle / 'tesseract.exe')
    assert checked.tessdata == bundle / 'tessdata'
    assert checked.languages == ('eng',)
    assert checked.portable and checked.target == 'win_amd64'
    assert str(bundle) not in json.dumps(checked.fingerprint())
    checked.assert_unchanged()


def test_script_inventory_preserves_relative_names_and_hashes_every_pack(bundle):
    for name in ('Latin', 'script/Latin', 'script/Cyrillic', 'nested/Unsupported'):
        path = bundle / 'tessdata' / (name + '.traineddata')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(('synthetic inventory: ' + name).encode())
    write_manifest(bundle)
    checked = runtime.verify_ocr_bundle(bundle)
    assert checked.languages == ('Latin', 'eng', 'script/Cyrillic', 'script/Latin')
    checked.require_languages('eng+script/Latin+script/Cyrillic')
    hashes = checked.fingerprint()['files_sha256']
    for name in ('Latin', 'script/Latin', 'script/Cyrillic', 'nested/Unsupported'):
        relative = 'tessdata/' + name + '.traineddata'
        assert hashes[relative] == sha256((bundle / relative).read_bytes()).hexdigest()
    model = bundle / 'tessdata/script/Latin.traineddata'
    data = bytearray(model.read_bytes())
    data[-1] ^= 1
    model.write_bytes(data)
    with pytest.raises(runtime.OcrRuntimeError, match='checksum'):
        runtime.verify_ocr_bundle(bundle)


def test_missing_script_model_never_falls_back_to_another_runtime(bundle, monkeypatch):
    monkeypatch.setenv('CARR_OCR_DIR', str(bundle))
    monkeypatch.setattr(runtime.shutil, 'which', lambda _: pytest.fail('No PATH fallback permitted'))
    with pytest.raises(runtime.OcrRuntimeError, match='selected language'):
        runtime.discover_ocr_runtime('script/Latin')


@pytest.mark.parametrize('selection', ['../eng', 'script/../eng', 'script/Latin/eng',
                                      'script\\Latin', '/eng', 'eng+'])
def test_runtime_discovery_rejects_unsafe_model_identifiers(selection, monkeypatch):
    monkeypatch.setattr(runtime.shutil, 'which', lambda _: pytest.fail('No executable lookup permitted'))
    with pytest.raises(runtime.OcrRuntimeError, match='language selection'):
        runtime.discover_ocr_runtime(selection)


@pytest.mark.parametrize('name', ['tesseract.exe', 'dependency.dll', 'tessdata/eng.traineddata'])
def test_byte_tamper_even_at_same_size_is_rejected(bundle, name):
    path = bundle / name
    changed = bytearray(path.read_bytes())
    changed[-1] ^= 1
    path.write_bytes(changed)
    with pytest.raises(runtime.OcrRuntimeError, match='checksum'):
        runtime.verify_ocr_bundle(bundle)


@pytest.mark.parametrize('change', ['missing', 'extra', 'symlink', 'hardlink', 'linked-directory'])
def test_missing_extra_or_linked_payload_is_rejected(bundle, tmp_path, change):
    if change == 'missing':
        (bundle / 'dependency.dll').unlink()
    elif change == 'extra':
        (bundle / 'unlisted.dll').write_bytes(b'unknown executable dependency')
    elif change == 'symlink':
        original = bundle / 'dependency.dll'
        original.rename(tmp_path / 'outside.dll')
        original.symlink_to(tmp_path / 'outside.dll')
    elif change == 'hardlink':
        os.link(bundle / 'dependency.dll', tmp_path / 'other-name.dll')
    else:
        original = bundle / 'tessdata'
        original.rename(tmp_path / 'outside-models')
        original.symlink_to(tmp_path / 'outside-models', target_is_directory=True)
    with pytest.raises(runtime.OcrRuntimeError):
        runtime.verify_ocr_bundle(bundle)


@pytest.mark.parametrize('name', ['../outside.dll', '/absolute.dll', 'C:/outside.dll',
                                 'dir\\other.dll', './other.dll', 'NUL.dll', 'file.',
                                 'double//path.dll'])
def test_unsafe_manifest_paths_are_rejected(bundle, name):
    data = json.loads((bundle / runtime.MANIFEST).read_text())
    data['files'][name] = {'size': 1, 'sha256': 'a' * 64}
    (bundle / runtime.MANIFEST).write_text(json.dumps(data))
    with pytest.raises(runtime.OcrRuntimeError):
        runtime.verify_ocr_bundle(bundle)


def test_duplicate_keys_and_case_collisions_are_rejected(bundle):
    original = (bundle / runtime.MANIFEST).read_text()
    (bundle / runtime.MANIFEST).write_text(original.replace('"schema": 1', '"schema": 1, "schema": 1'))
    with pytest.raises(runtime.OcrRuntimeError, match='Duplicate'):
        runtime.verify_ocr_bundle(bundle)
    data = json.loads(original)
    data['files']['DEPENDENCY.DLL'] = data['files']['dependency.dll']
    (bundle / runtime.MANIFEST).write_text(json.dumps(data))
    with pytest.raises(runtime.OcrRuntimeError, match='Ambiguous'):
        runtime.verify_ocr_bundle(bundle)


def test_resource_budget_is_checked_before_payload_read(bundle):
    data = json.loads((bundle / runtime.MANIFEST).read_text())
    data['files']['tesseract.exe']['size'] = runtime.MAX_FILE + 1
    (bundle / runtime.MANIFEST).write_text(json.dumps(data))
    with pytest.raises(runtime.OcrRuntimeError, match='identity'):
        runtime.verify_ocr_bundle(bundle)
    def expired():
        raise TimeoutError('remaining work budget exhausted')
    with pytest.raises(runtime.OcrRuntimeError):
        runtime.verify_ocr_bundle(bundle, remaining=expired)


def test_explicit_selection_and_missing_language_do_not_fall_back(bundle, monkeypatch):
    monkeypatch.setenv('CARR_OCR_DIR', str(bundle))
    monkeypatch.setattr(runtime.shutil, 'which', lambda _: pytest.fail('No PATH fallback permitted'))
    assert runtime.discover_ocr_runtime().portable
    with pytest.raises(runtime.OcrRuntimeError, match='language'):
        runtime.discover_ocr_runtime('ukr')
    with pytest.raises(runtime.OcrRuntimeError, match='orientation'):
        runtime.discover_ocr_runtime(orientation=True)
    (bundle / 'dependency.dll').write_bytes(b'broken')
    with pytest.raises(runtime.OcrRuntimeError):
        runtime.discover_ocr_runtime()
    monkeypatch.setenv('CARR_OCR_DIR', str(bundle / 'missing'))
    with pytest.raises(runtime.OcrRuntimeError):
        runtime.discover_ocr_runtime()


@pytest.mark.parametrize('value', ['', 'ocr', '../ocr'])
def test_relative_or_empty_explicit_selection_is_rejected(value, monkeypatch):
    monkeypatch.setenv('CARR_OCR_DIR', value)
    with pytest.raises(runtime.OcrRuntimeError, match='absolute'):
        runtime.discover_ocr_runtime()


def test_prefix_discovery_and_incompatible_platform(bundle, monkeypatch):
    monkeypatch.setattr(runtime.sys, 'prefix', str(bundle.parent / '.venv'))
    monkeypatch.setattr(runtime, '_native_target', lambda: 'win_amd64')
    assert runtime.discover_ocr_runtime().executable == str(bundle / 'tesseract.exe')
    monkeypatch.setattr(runtime, '_native_target', lambda: 'linux_x86_64')
    monkeypatch.setattr(runtime.shutil, 'which', lambda _: '/system/tesseract')
    assert runtime.discover_ocr_runtime().executable == '/system/tesseract'


def test_cwd_is_never_an_automatic_candidate(bundle, monkeypatch):
    monkeypatch.chdir(bundle.parent)
    monkeypatch.setattr(runtime.shutil, 'which', lambda _: None)
    assert runtime.discover_ocr_runtime() is None


def test_tessdata_binding_preserves_explicit_pinned_staging(bundle):
    checked = runtime.verify_ocr_bundle(bundle)
    command = checked.command([checked.executable, '--list-langs'])
    assert command[1:3] == ['--tessdata-dir', str(bundle / 'tessdata')]
    pinned = [checked.executable, 'input.png', 'output', '--tessdata-dir', '/staged/models']
    assert checked.command(pinned) == pinned
    assert checked.command([checked.executable, '--version']) == [checked.executable, '--version']


@pytest.fixture
def native_bundle(tmp_path, monkeypatch):
    executable = shutil.which('tesseract')
    if executable is None:
        pytest.skip('Real local Tesseract unavailable')
    report = subprocess.check_output([executable, '--list-langs'], text=True)
    source = Path(re.search(r'^List of available languages in "([^"\r\n]+)"', report, re.M)[1])
    root = tmp_path / 'native portable ü' / 'ocr'
    (root / 'tessdata').mkdir(parents=True)
    name = Path(executable).name
    shutil.copy2(executable, root / name)
    if os.name == 'nt':
        # The delivered Windows executable needs its private sibling DLLs.
        # They join the synthetic fixture's complete verified inventory;
        # PATH remains cleared below, just as in the production bundle check.
        for dependency in Path(executable).parent.glob('*.dll'):
            shutil.copy2(dependency, root / dependency.name)
    for language in ('eng', 'osd'):
        if not (source / f'{language}.traineddata').is_file():
            pytest.skip(f'Real local {language} data unavailable')
        shutil.copyfile(source / f'{language}.traineddata', root / 'tessdata' / f'{language}.traineddata')
    (root / 'dependency-fixture.txt').write_bytes(b'additional inventoried runtime component')
    write_manifest(root, target=runtime._native_target(), executable=name)
    monkeypatch.setenv('CARR_OCR_DIR', str(root))
    monkeypatch.setenv('TESSDATA_PREFIX', str(tmp_path / 'wrong model path'))
    monkeypatch.setenv('PATH', str(tmp_path / 'no system OCR'))
    return root


def test_real_ocr_self_check_uses_portable_models_without_path(native_bundle):
    result = runtime.check_runtime()
    assert result['status'] == 'PASS' and result['synthetic_recognition'] == 'RESEARCH'
    assert result['runtime'] == 'portable' and 'eng' in result['languages']
    assert result['manifest_sha256'] == sha256((native_bundle / runtime.MANIFEST).read_bytes()).hexdigest()
    assert result['windows_execution'] == ('PASS' if os.name == 'nt' else 'NOT_RUN')


def test_cli_failure_is_nonzero_and_clear(monkeypatch, capsys):
    monkeypatch.setattr(runtime.shutil, 'which', lambda _: None)
    assert runtime.main(['--check']) == 1
    result = json.loads(capsys.readouterr().out)
    assert result['status'] == 'FAIL' and 'No local OCR' in result['error']


def test_real_fingerprint_detects_runtime_payload_change(native_bundle):
    from carr_library.extract.page_ocr import Config
    from carr_library.extract.runtime_identity import IdentityError, runtime_identity
    before = runtime_identity(Config())
    assert before['ocr']['portable']['manifest_sha256']
    assert str(native_bundle) not in json.dumps(before)
    (native_bundle / 'dependency-fixture.txt').write_bytes(b'changed runtime component')
    with pytest.raises(IdentityError):
        runtime_identity(Config())
    executable = json.loads((native_bundle / runtime.MANIFEST).read_text())['executable']
    write_manifest(native_bundle, target=runtime._native_target(), executable=executable)
    assert before != runtime_identity(Config())


def test_real_portable_scan_preserves_candidate_boundary(native_bundle, tmp_path):
    from PIL import Image, ImageDraw, ImageFont
    from carr_library.extract.page_ocr import Config, extract_page
    picture = Image.new('RGB', (1400, 400), 'white')
    ImageDraw.Draw(picture).text((45, 100), 'Portable research', fill='black',
                                font=ImageFont.load_default(size=70))
    pdf = tmp_path / 'scan.pdf'
    picture.save(pdf, resolution=300)
    result = extract_page(pdf, 1, Config())
    assert result['raw_text'].strip() == 'Portable research'
    assert result['status'] == 'review_required' and not result['printed_label_verified']
    assert 'ocr_not_verified' in result['warnings']
    assert result['provenance']['ocr_runtime']['manifest_sha256']
    (native_bundle / 'dependency-fixture.txt').write_bytes(b'tampered runtime')
    rejected = extract_page(pdf, 1, Config())
    assert 'portable_ocr_runtime_unavailable_or_invalid' in rejected['warnings']
    assert not rejected['raw_text'] and not rejected['blocks']


@pytest.mark.parametrize('selection', ['script/EnglishFixture', 'eng+script/EnglishFixture'])
def test_real_script_path_loading_fingerprint_and_review_boundary(native_bundle, tmp_path, selection):
    # This exercises Tesseract's actual subdirectory loading using renamed
    # local English bytes; it is not an accuracy test of an unavailable script pack.
    from PIL import Image, ImageDraw, ImageFont
    from carr_library.extract.page_ocr import Config, extract_page
    from carr_library.extract.runtime_identity import runtime_identity
    script = native_bundle / 'tessdata/script/EnglishFixture.traineddata'
    script.parent.mkdir()
    shutil.copyfile(native_bundle / 'tessdata/eng.traineddata', script)
    executable = json.loads((native_bundle / runtime.MANIFEST).read_text())['executable']
    write_manifest(native_bundle, target=runtime._native_target(), executable=executable)
    config = Config(languages=selection)
    assert 'script/EnglishFixture' in runtime.check_runtime()['languages']
    from carr_library.extract.ocr_languages import discover_languages
    from carr_library.ui.ocr_selection import discover_installed_models
    expected_models = {'eng', 'osd', 'script/EnglishFixture'}
    assert set(discover_languages().codes) == expected_models
    assert set(discover_installed_models()) == expected_models
    before = runtime_identity(config)
    model_hash = sha256(script.read_bytes()).hexdigest()
    assert before['ocr']['model_sha256'] == {name: model_hash for name in selection.split('+')}
    assert before['ocr']['portable']['files_sha256']['tessdata/script/EnglishFixture.traineddata'] == model_hash
    assert str(native_bundle) not in json.dumps(before)
    picture = Image.new('RGB', (1400, 400), 'white')
    ImageDraw.Draw(picture).text((45, 100), 'Script model research', fill='black',
                                font=ImageFont.load_default(size=70))
    pdf = tmp_path / 'script-model-scan.pdf'
    picture.save(pdf, resolution=300)
    result = extract_page(pdf, 1, config)
    assert result['raw_text'].strip() == 'Script model research'
    assert result['physical_index'] == 1 and result['blocks']
    assert result['status'] == 'review_required' and result['printed_label'] is None
    assert not result['printed_label_verified'] and 'ocr_not_verified' in result['warnings']
    assert result['provenance']['config']['languages'] == selection
    assert result['provenance']['ocr_runtime'] == before['ocr']['portable']
    with script.open('ab') as stream:
        stream.write(b'synthetic model byte change')
    write_manifest(native_bundle, target=runtime._native_target(), executable=executable)
    after = runtime_identity(config)
    assert before != after
    assert before['ocr']['model_sha256']['script/EnglishFixture'] != after['ocr']['model_sha256']['script/EnglishFixture']


def test_portable_native_orientation_uses_existing_cli_without_wrapper(native_bundle, tmp_path):
    from PIL import Image, ImageDraw, ImageFont
    from carr_library.extract.page_ocr import Config, extract_page
    text = ('The archive contains letters and historical notes.\n'
            'Researchers compare editions and original pages.\n'
            'A railway bridge connects the eastern villages.\n'
            'Every quotation retains its original punctuation.\n'
            'The library contains independent collections.\n'
            'A researcher can review the evidence carefully.\n'
            'Printed numbers may differ from physical pages.\n'
            'Documents are preserved in their original form.\n'
            'The source provides evidence for each statement.\n'
            'Different authors may use the same expression.')
    # The pinned test dependency supplies the same real font on every platform.
    # Windows runners do not install the Linux DejaVu font family by default.
    import reportlab
    font = ImageFont.truetype(str(Path(reportlab.__file__).parent / 'fonts' / 'Vera.ttf'), 48)
    picture = Image.new('RGB', (1800, 1150), 'white')
    ImageDraw.Draw(picture).multiline_text((60, 60), text, fill='black', spacing=24, font=font)
    pdf = tmp_path / 'rotated.pdf'
    picture.rotate(90, expand=True).save(pdf, resolution=240)
    result = extract_page(pdf, 1, Config(dpi=240, orientation=True))
    orientation = result['provenance']['orientation']
    assert orientation['algorithm'] == 'portable-native-osd-v1'
    assert orientation['status'] == 'ocr_candidate' and orientation['clockwise'] == 90
    assert 'The archive contains' in orientation['raw_text']
    assert result['provenance']['config']['model_policy'] == 'default'
    assert 'wrapper_version' not in orientation
    assert result['status'] == 'review_required'

def test_unreadable_directory_rejects_entire_inventory(bundle, monkeypatch):
    hidden = bundle / 'unreadable'
    hidden.mkdir()
    (hidden / 'unlisted.dll').write_bytes(b'unreviewed')
    original = runtime.os.scandir

    def denied(path):
        if Path(path) == hidden:
            raise PermissionError('synthetic enumeration failure')
        return original(path)

    monkeypatch.setattr(runtime.os, 'scandir', denied)
    with pytest.raises(runtime.OcrRuntimeError, match='unavailable or invalid'):
        runtime.verify_ocr_bundle(bundle)


def test_empty_directory_inventory_has_a_bound(bundle, monkeypatch):
    monkeypatch.setattr(runtime, 'MAX_FILES', 4)
    for index in range(4):
        (bundle / f'empty-{index}').mkdir()
    with pytest.raises(runtime.OcrRuntimeError, match='directory limit'):
        runtime.verify_ocr_bundle(bundle)


def test_directory_inventory_checks_budget_between_entries(bundle, monkeypatch):
    # Isolate the inventory stage: budget is checked before accepting each
    # entry, including empty directories, not only once per os.walk yield.
    for index in range(10):
        (bundle / f'empty-{index}').mkdir()
    count = 0

    def remaining():
        nonlocal count
        count += 1
        if count == 4:
            raise TimeoutError('synthetic budget expiry')

    with pytest.raises(TimeoutError, match='budget expiry'):
        runtime._inventory(bundle, remaining)
    assert count == 4

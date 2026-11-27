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
from carr_library.extract.ocr_process import run_ocr


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
    result = run_ocr(runtime, [runtime.executable, str(path), 'stdout',
        '-l', 'eng', '--psm', '6'], remaining=lambda: 20)
    assert result.stdout.decode('utf-8').strip() == 'Managed research library'
    runtime.assert_unchanged()
    assert sha256(path.read_bytes()).hexdigest() == before
    assert runtime.selection == store.select('fast', 'eng', orientation=True)


def test_real_list_languages_uses_managed_model_directory(real_models, monkeypatch, tmp_path):
    runtime, _ = real_models
    monkeypatch.setenv('TESSDATA_PREFIX', str(tmp_path / 'missing'))
    result = run_ocr(runtime, [runtime.executable, '--list-langs'], remaining=lambda: 10)
    report = result.stdout.decode('utf-8')
    assert str(runtime.tessdata) in report and report.splitlines()[1:] == ['eng', 'osd']


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
    from carr_library.extract import page_ocr as page_module
    execute = page_module.run_ocr
    def traced(runtime, argv, *, remaining):
        try:
            completed = execute(runtime, argv, remaining=remaining)
            print('OCR_PROCESS_DIAGNOSTIC', json.dumps({
                'arguments': completed.args,
                'stdout': completed.stdout.decode('utf-8', 'replace'),
                'stderr': completed.stderr.decode('utf-8', 'replace'),
                'returncode': completed.returncode}, ensure_ascii=False), flush=True)
            return completed
        except subprocess.CalledProcessError as exc:
            raise AssertionError({'arguments': exc.cmd,
                                  'stderr': exc.stderr.decode('utf-8', 'replace')}) from exc
    monkeypatch.setattr(page_module, 'run_ocr', traced)
    read_output = page_module._read_ocr_output
    def traced_read(path):
        try:
            return read_output(path)
        except OSError as exc:
            raise AssertionError({'sidecar': str(path), 'error': repr(exc),
                                  'siblings': [item.name for item in path.parent.iterdir()]}) from exc
    monkeypatch.setattr(page_module, '_read_ocr_output', traced_read)
    config = Config(model_profile='fast')
    identity = runtime_identity(config)
    result = extract_page(source, 1, config)
    print('OCR_RESULT_DIAGNOSTIC', json.dumps(result, ensure_ascii=False), flush=True)
    assert result['raw_text'].strip() == 'Managed source evidence', result['warnings']
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



def test_real_unicode_image_output_and_temp_use_exact_managed_models(real_models, tmp_path, monkeypatch):
    """Actual inference with Unicode at every user-writable path boundary."""
    import sys
    import tempfile
    from PIL import Image, ImageDraw, ImageFont
    from carr_library.extract import ocr_process
    runtime, _ = real_models
    temporary = tmp_path / 'Окрема тимчасова папка'
    temporary.mkdir()
    monkeypatch.setattr(tempfile, 'tempdir', str(temporary))
    source = tmp_path / 'Скан джерела.png'
    output = tmp_path / 'Результат розпізнавання'
    picture = Image.new('RGB', (1600, 320), 'white')
    ImageDraw.Draw(picture).text((40, 80), 'Unicode research evidence', fill='black',
                                font=ImageFont.load_default(size=65))
    picture.save(source, dpi=(300, 300))
    original = sha256(source.read_bytes()).hexdigest()
    model_hash = sha256((runtime.tessdata / 'eng.traineddata').read_bytes()).hexdigest()
    calls = []
    copy = ocr_process._copy
    def record(source, target, *args, **kwargs):
        calls.append((source, target))
        return copy(source, target, *args, **kwargs)
    monkeypatch.setattr(ocr_process, '_copy', record)
    result = run_ocr(runtime, [runtime.executable, str(source), str(output),
        '-l', 'eng', '--psm', '6', '-c', 'tessedit_create_txt=1',
        '-c', 'tessedit_create_tsv=1'], remaining=lambda: 20)
    assert result.returncode == 0
    assert Path(str(output) + '.txt').read_text(encoding='utf-8').strip() == 'Unicode research evidence'
    assert Path(str(output) + '.tsv').read_text(encoding='utf-8').startswith('level\tpage_num\t')
    assert sha256(source.read_bytes()).hexdigest() == original
    assert sha256((runtime.tessdata / 'eng.traineddata').read_bytes()).hexdigest() == model_hash
    assert list(temporary.iterdir()) == []
    runtime.assert_unchanged()
    if sys.platform == 'win32':
        assert any(origin == runtime.tessdata / 'eng.traineddata' for origin, _ in calls)
        assert all(arg.isascii() for arg in result.args[1:])


def test_real_unicode_pinned_stage_preserves_review_and_model_provenance(real_models, tmp_path, monkeypatch):
    import tempfile
    from PIL import Image, ImageDraw, ImageFont
    from carr_library.extract.page_ocr import Config, extract_page
    from carr_library.extract.pinned_tessdata import POLICY, MODELS
    runtime, _ = real_models
    temporary = tmp_path / 'Тимчасові перевірені файли'
    temporary.mkdir()
    monkeypatch.setattr(tempfile, 'tempdir', str(temporary))
    monkeypatch.delenv('CARR_OCR_DIR', raising=False)
    picture = Image.new('RGB', (1600, 400), 'white')
    ImageDraw.Draw(picture).text((40, 100), 'Pinned Unicode evidence', fill='black',
                                font=ImageFont.load_default(size=65))
    source = tmp_path / 'Українська назва.pdf'
    picture.save(source, resolution=300)
    before = sha256(source.read_bytes()).hexdigest()
    result = extract_page(source, 1, Config(model_profile='fast', model_policy=POLICY))
    assert result['raw_text'].strip() == 'Pinned Unicode evidence'
    assert result['status'] == 'review_required' and result['blocks']
    assert result['provenance']['ocr_model']['model_sha256'] == {'eng': MODELS['eng'][1]}
    assert result['provenance']['ocr_runtime']['managed_models']['profile'] == 'fast'
    assert not result['printed_label_verified'] and 'ocr_not_verified' in result['warnings']
    assert before == sha256(source.read_bytes()).hexdigest()
    assert list(temporary.iterdir()) == []
    runtime.assert_unchanged()


def test_real_unicode_inventory_keeps_bounded_native_discovery(real_models, monkeypatch):
    from carr_library.extract import ocr_runtime
    from carr_library.ui.ocr_selection import discover_installed_models
    runtime, _ = real_models
    monkeypatch.setattr(ocr_runtime, 'discover_ocr_runtime', lambda *args, **kwargs: runtime)
    assert set(discover_installed_models()) == {'eng', 'osd'}
    runtime.assert_unchanged()


@pytest.mark.parametrize('failure', ['digest', 'deadline', 'size'], ids=['wrong-digest', 'cancelled', 'size-limit'])
def test_private_copy_failure_removes_only_its_partial_output(tmp_path, failure):
    from carr_library.extract.ocr_process import _copy
    source, target = tmp_path / 'source.bin', tmp_path / 'private.bin'
    source.write_bytes(b'preserved input bytes')
    before = source.read_bytes()
    def remaining():
        if failure == 'deadline':
            raise TimeoutError('test cancellation')
        return 1
    with pytest.raises((OcrRuntimeError, TimeoutError)):
        _copy(source, target, 2 if failure == 'size' else 1024, remaining,
              '0' * 64 if failure == 'digest' else None)
    assert not target.exists() and source.read_bytes() == before


def test_private_copy_never_clobbers_a_competing_output(tmp_path):
    from carr_library.extract.ocr_process import _copy
    source, target = tmp_path / 'source.bin', tmp_path / 'existing.bin'
    source.write_bytes(b'new source')
    target.write_bytes(b'keep existing output')
    with pytest.raises(FileExistsError):
        _copy(source, target, 1024, lambda: 1)
    assert target.read_bytes() == b'keep existing output'


def test_private_copy_rejects_a_linked_model(tmp_path):
    from carr_library.extract.ocr_process import _copy
    source, alias, target = (tmp_path / name for name in ('source.bin', 'alias.bin', 'staged.bin'))
    source.write_bytes(b'linked model')
    os.link(source, alias)
    with pytest.raises(OcrRuntimeError, match='unlinked'):
        _copy(source, target, 1024, lambda: 1)
    assert not target.exists() and source.read_bytes() == alias.read_bytes() == b'linked model'


def test_inventory_normalization_checks_the_actual_reported_directory(tmp_path):
    from carr_library.extract.ocr_process import normalize_ocr_query_output
    selected, other = tmp_path / 'Вибрані моделі', tmp_path / 'other'
    selected.mkdir()
    other.mkdir()
    report = b'List of available languages in "./" (1):\neng\n'
    normalized = normalize_ocr_query_output(report, selected).decode('utf-8')
    assert str(selected) in normalized and normalized.endswith('\neng\n')
    wrong = f'List of available languages in "{other}" (1):\neng\n'.encode('utf-8')
    with pytest.raises(OcrRuntimeError, match='different'):
        normalize_ocr_query_output(wrong, selected)


def test_explicit_equals_model_directory_is_not_overridden(tmp_path):
    runtime = OcrRuntime('tesseract', tessdata=tmp_path / 'base')
    command = ['tesseract', 'scan.png', 'stdout', '--tessdata-dir=' + str(tmp_path / 'selected')]
    assert runtime.command(command) == command



def test_relative_unicode_model_directory_uses_the_original_child_cwd(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from carr_library.extract import ocr_process
    root = tmp_path / 'engine'
    models = root / 'моделі'
    models.mkdir(parents=True)
    runtime = OcrRuntime('tesseract', root=root)
    monkeypatch.setattr(ocr_process, 'sys', SimpleNamespace(platform='win32'))
    command, cwd, report_directory = ocr_process.prepare_ocr_query(runtime,
        ['tesseract', '--list-langs', '--tessdata-dir', 'моделі'])
    assert cwd == report_directory == models
    assert command == ['tesseract', '--list-langs', '--tessdata-dir', '.']
    source = root / 'input.png'
    source.write_bytes(b'path-binding fixture; native inference is separate')
    calls = []
    def execute(command, cwd, remaining):
        calls.append((command, cwd))
        return subprocess.CompletedProcess(command, 0, b'', b'')
    monkeypatch.setattr(ocr_process, '_execute', execute)
    ocr_process.run_ocr(runtime,
        ['tesseract', 'input.png', 'stdout', '--tessdata-dir', 'моделі'],
        remaining=lambda: 1)
    assert len(calls) == 1 and calls[0][1] == models
    assert (models / calls[0][0][1]).samefile(source)


def test_unbound_runtime_never_reinterprets_relative_models_after_cwd_change(tmp_path, monkeypatch):
    import tempfile
    from types import SimpleNamespace
    from carr_library.extract import ocr_process
    source = tmp_path / 'Зображення.png'
    source.write_bytes(b'path-binding fixture; native process must not run')
    temporary = tmp_path / 'Інша тимчасова папка'
    temporary.mkdir()
    monkeypatch.setattr(tempfile, 'tempdir', str(temporary))
    monkeypatch.setattr(ocr_process, 'sys', SimpleNamespace(platform='win32'))
    monkeypatch.setenv('TESSDATA_PREFIX', './tessdata')
    monkeypatch.setattr(ocr_process, '_execute',
        lambda *args: pytest.fail('An unbound engine must not run under a different cwd.'))
    with pytest.raises(OcrRuntimeError, match='explicitly bound'):
        run_ocr(OcrRuntime('tesseract', root=tmp_path),
                ['tesseract', str(source), 'stdout'], remaining=lambda: 1)
    assert source.read_bytes() == b'path-binding fixture; native process must not run'
    assert list(temporary.iterdir()) == []

"""Inventory, scope and durable OCR preference boundaries; no quality claim."""
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Event, Timer
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from carr_library.extract import ocr_languages as models, ocr_runtime
from carr_library.extract.page_ocr import Config
from carr_library.extract.pinned_tessdata import POLICY


def report(*codes):
    return f'List of available languages in "installed models" ({len(codes)}):\n' + '\n'.join(codes) + '\n'


def runtime(monkeypatch, *, codes=('eng', 'fra', 'osd', 'script/Latin'), portable=False):
    native = SimpleNamespace(executable='selected local engine', root=None,
        portable=portable, languages=codes, manifest_sha256='a' * 64 if portable else None,
        assert_unchanged=Mock(), command=lambda args: args)
    monkeypatch.setattr(ocr_runtime, 'discover_ocr_runtime', Mock(return_value=native))
    monkeypatch.setattr(models, '_capture', Mock(side_effect=['tesseract 5.5.0\n', report(*codes)]))
    return native


def test_discovery_reports_all_safe_models_without_treating_osd_as_a_language(monkeypatch):
    native = runtime(monkeypatch)
    catalog = models.discover_languages()
    assert catalog.codes == ('eng', 'fra', 'osd', 'script/Latin')
    assert catalog.selectable == ('eng', 'fra', 'script/Latin')
    assert catalog.matching('french') == ('fra',)
    assert catalog.matching('SCRIPT/') == ('script/Latin',)
    assert catalog.integrity == 'system_installation' and catalog.manifest_sha256 is None
    assert catalog.validate('script/Latin+eng', orientation=True) == ('script/Latin', 'eng')
    assert [call.args[1] for call in models._capture.call_args_list] == ['--version', '--list-langs']
    native.assert_unchanged.assert_called_once()


def test_portable_inventory_is_rechecked_and_cannot_admit_unsealed_models(monkeypatch):
    native = runtime(monkeypatch, portable=True)
    catalog = models.discover_languages()
    assert catalog.integrity == 'portable_sha256' and catalog.manifest_sha256 == 'a' * 64
    models._capture.side_effect = ['tesseract 5.5.0', report('eng', 'fra', 'osd', 'script/Latin', 'ukr')]
    with pytest.raises(models.OcrLanguageError, match='verified portable inventory'):
        models.discover_languages()
    assert native.assert_unchanged.call_count == 1


def test_broken_explicit_runtime_does_not_fall_back_or_claim_models(monkeypatch):
    choose = Mock(side_effect=ocr_runtime.OcrRuntimeError('Portable OCR file checksum mismatch.'))
    monkeypatch.setattr(ocr_runtime, 'discover_ocr_runtime', choose)
    capture = Mock()
    monkeypatch.setattr(models, '_capture', capture)
    with pytest.raises(ocr_runtime.OcrRuntimeError, match='checksum'):
        models.discover_languages()
    choose.assert_called_once()
    capture.assert_not_called()


def test_missing_engine_is_explicit_and_does_not_run_a_process(monkeypatch):
    monkeypatch.setattr(ocr_runtime, 'discover_ocr_runtime', lambda **kwargs: None)
    monkeypatch.setattr(subprocess, 'run', Mock(side_effect=AssertionError('No process allowed')))
    catalog = models.discover_languages()
    assert catalog.codes == () and catalog.integrity == 'unavailable'


def test_runtime_change_during_discovery_is_not_presented_as_valid(monkeypatch):
    native = runtime(monkeypatch, portable=True)
    native.assert_unchanged.side_effect = ocr_runtime.OcrRuntimeError('Runtime changed.')
    with pytest.raises(ocr_runtime.OcrRuntimeError, match='changed'):
        models.discover_languages()


@pytest.mark.parametrize('value', ['', 'eng+eng', 'eng+ENG', 'eng++fra', '../eng',
    'script/../eng', 'script//Latin', 'script/Latin/eng', '/eng', 'C:/eng',
    'eng --psm 1', '-eng', 'eng\nfra', 'eng\x00', 'osd', 'OSD', 'equ', 'EQU', 'eng+osd',
    'NUL', 'script/CON', '+'.join(f'code{i}' for i in range(9))])
def test_unsafe_repeated_auxiliary_or_unbounded_selections_are_rejected(value):
    with pytest.raises(ValueError):
        Config(languages=value)


def test_script_selection_does_not_bypass_pinned_profile_or_installed_inventory():
    catalog = models.LanguageCatalog(('eng', 'fra', 'script/Latin'), integrity='system_installation')
    assert Config(languages='script/Latin+fra').languages == 'script/Latin+fra'
    with pytest.raises(ValueError, match='pinned'):
        Config(languages='script/Latin+fra', model_policy=POLICY)
    with pytest.raises(ValueError, match='not installed'):
        catalog.validate('ukr')
    with pytest.raises(ValueError, match='OSD'):
        catalog.validate('eng', orientation=True)


def test_runtime_auxiliary_data_does_not_consume_the_eight_text_model_limit():
    languages = 'eng+ukr+slk+fra+deu+spa+ita+pol'
    assert Config(languages=languages, orientation=True).languages == languages
    assert models.model_ids(languages + '+osd') == tuple(languages.split('+')) + ('osd',)
    assert models.model_ids(languages + '+ces') == tuple(languages.split('+')) + ('ces',)
    with pytest.raises(ValueError):
        Config(languages=languages + '+ces')
    with pytest.raises(ValueError):
        models.model_ids('+'.join(f'model{i}' for i in range(models.MAX_MODELS + 1)))
    with pytest.raises(ValueError):
        Config(languages=languages + '+osd')


@pytest.mark.parametrize('profile', ['fast', 'best'])
def test_managed_discovery_verifies_selected_profile_before_publishing_inventory(monkeypatch, profile):
    codes = ('eng', 'ukr', 'osd')
    native = runtime(monkeypatch, codes=codes)
    selection = SimpleNamespace(directory=Path('installed models'), languages=codes,
                                assert_unchanged=Mock())
    store = SimpleNamespace(available=Mock(return_value=codes), select=Mock(return_value=selection))
    monkeypatch.setattr('carr_library.extract.ocr_model_store.OcrModelStore', lambda: store)
    catalog = models.discover_languages(model_profile=profile)
    assert catalog.model_profile == profile
    assert catalog.integrity == 'verified_managed_profile'
    assert catalog.selectable == ('eng', 'ukr')
    store.available.assert_called_once_with(profile)
    assert store.select.call_args.args == (profile, 'eng+ukr+osd')
    assert models._capture.call_count == 2
    native.assert_unchanged.assert_called_once()
    selection.assert_unchanged.assert_called_once()


def test_empty_managed_inventory_keeps_its_selected_profile_and_never_falls_back(monkeypatch):
    native = runtime(monkeypatch)
    store = SimpleNamespace(available=Mock(return_value=()), select=Mock())
    monkeypatch.setattr('carr_library.extract.ocr_model_store.OcrModelStore', lambda: store)
    catalog = models.discover_languages(model_profile='best')
    assert catalog.model_profile == 'best' and catalog.codes == ()
    with pytest.raises(ValueError, match='not installed'):
        catalog.validate('eng')
    store.select.assert_not_called()
    native.assert_unchanged.assert_called_once()


def test_missing_engine_cannot_silently_use_a_managed_profile(monkeypatch):
    monkeypatch.setattr(ocr_runtime, 'discover_ocr_runtime', lambda **kwargs: None)
    with pytest.raises(ValueError, match='local OCR engine'):
        models.discover_languages(model_profile='fast')


@pytest.mark.parametrize('language', [' eng', 'eng ', 'eng\t'])
def test_runtime_inventory_does_not_normalize_an_invalid_model_identifier(language):
    with pytest.raises(ValueError):
        models.parse_language_report(report(language))


def test_runtime_inventory_is_bound_to_the_requested_model_directory(tmp_path):
    listing = f'List of available languages in "{tmp_path / "other"}" (1):\neng\n'
    with pytest.raises(ValueError, match='different model directory'):
        models.parse_language_report(listing, expected_directory=tmp_path / 'selected')


@pytest.mark.parametrize('text', ['eng\nfra', 'List of available languages in "x" (2):\neng\n',
    'List of available languages in "x" (2):\neng\neng',
    'List of available languages in "x" (1):\n../eng',
    'List of available languages in "x" (1):\neng --psm 1',
    'x' * (models.MAX_REPORT_BYTES + 1)],
    ids=('missing-header', 'wrong-count', 'duplicate', 'traversal', 'arguments', 'over-budget'))
def test_invalid_native_inventory_is_not_silently_filtered_into_a_valid_choice(text):
    with pytest.raises(ValueError):
        models.parse_language_report(text)


@pytest.mark.parametrize('timeout', [True, 0, -1, 31, float('nan'), float('inf'), '10'])
def test_invalid_discovery_budget_cannot_launch_runtime(monkeypatch, timeout):
    choose = Mock()
    monkeypatch.setattr(ocr_runtime, 'discover_ocr_runtime', choose)
    with pytest.raises(ValueError, match='timeout'):
        models.discover_languages(timeout=timeout)
    choose.assert_not_called()


def test_capture_uses_bound_native_arguments_and_file_backed_output(monkeypatch):
    class Process:
        def __init__(self, argv, **kwargs):
            assert argv == ['selected local engine', '--tessdata-dir', '/bound/models', '--list-langs']
            assert kwargs['cwd'] == '/bound/runtime'
            assert kwargs.get('shell', False) is False
            assert kwargs['stdout'].fileno() >= 0 and kwargs['stderr'].fileno() >= 0
            kwargs['stdout'].write(report('eng').encode('utf-8'))
            kwargs['stdout'].flush()
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def wait(self, timeout=None):
            assert timeout is None or timeout <= .05
            return 0
        def poll(self): return 0
    monkeypatch.setattr(subprocess, 'Popen', Process)
    native = SimpleNamespace(executable='selected local engine', root='/bound/runtime',
        command=lambda args: [args[0], '--tessdata-dir', '/bound/models', *args[1:]])
    assert models._capture(native, '--list-langs', lambda: 3) == report('eng')


@pytest.mark.parametrize('stream', ['stdout', 'stderr'])
def test_actual_child_oversized_output_is_stopped_and_rejected(stream):
    # Actual bounded process handling, using a synthetic Python emitter instead
    # of changing any installed native executable or model.
    code = f'import sys,time; sys.{stream}.buffer.write(b"x" * 1048576); sys.{stream}.flush(); time.sleep(5)'
    native = SimpleNamespace(executable=sys.executable, root=None,
                             command=lambda args: [sys.executable, '-c', code])
    with pytest.raises(models.OcrLanguageError, match='oversized output'):
        models._capture(native, '--list-langs', lambda: 2)


def test_actual_child_cancel_is_checked_while_inventory_process_runs():
    native = SimpleNamespace(executable=sys.executable, root=None,
        command=lambda args: [sys.executable, '-c', 'import time; time.sleep(5)'])
    stopped = Event()
    timer = Timer(.1, stopped.set)
    timer.start()
    def remaining():
        if stopped.is_set():
            raise models.OcrLanguageError('cancelled')
        return 2
    try:
        with pytest.raises(models.OcrLanguageError, match='cancelled'):
            models._capture(native, '--list-langs', remaining)
    finally:
        timer.cancel()


def test_native_timeout_is_clear_and_cannot_return_partial_inventory(monkeypatch):
    runtime(monkeypatch)
    models._capture.side_effect = subprocess.TimeoutExpired('local OCR', 1)
    with pytest.raises(ValueError, match='could not be read'):
        models.discover_languages(timeout=1)


def test_verified_portable_manifest_includes_nested_script_hashes(tmp_path):
    root = tmp_path / 'ocr'
    (root / 'tessdata' / 'script').mkdir(parents=True)
    (root / 'tesseract.exe').write_bytes(b'fixture, never executable')
    (root / 'tessdata' / 'eng.traineddata').write_bytes(b'English model fixture')
    (root / 'tessdata' / 'script' / 'Latin.traineddata').write_bytes(b'Script model fixture')
    files = {p.relative_to(root).as_posix(): dict(size=p.stat().st_size,
               sha256=sha256(p.read_bytes()).hexdigest()) for p in root.rglob('*') if p.is_file()}
    (root / ocr_runtime.MANIFEST).write_text(json.dumps(dict(schema=1, target='win_amd64',
        executable='tesseract.exe', tessdata='tessdata', files=files)), encoding='utf-8')
    checked = ocr_runtime.verify_ocr_bundle(root)
    assert checked.languages == ('eng', 'script/Latin')
    checked.require_languages('script/Latin+eng')
    assert dict(checked.files_sha256)['tessdata/script/Latin.traineddata'] == files['tessdata/script/Latin.traineddata']['sha256']
    (root / 'tessdata' / 'script' / 'Latin.traineddata').write_bytes(b'changed model fixture')
    with pytest.raises(ocr_runtime.OcrRuntimeError):
        checked.assert_unchanged()


def test_presets_reopen_keep_order_and_require_explicit_replacement(tmp_path):
    first = Config(languages='fra+eng', orientation=True)
    models.store_preset(tmp_path, 'Французька й англійська', first)
    row, = models.load_presets(tmp_path)
    assert row == dict(name='Французька й англійська', languages='fra+eng',
                       orientation=True, model_policy='default')
    before = (tmp_path / models.FILENAME).read_bytes()
    with pytest.raises(FileExistsError):
        models.store_preset(tmp_path, row['name'], Config())
    assert (tmp_path / models.FILENAME).read_bytes() == before
    models.store_preset(tmp_path, row['name'], Config(languages='script/Latin'), replace=True)
    assert models.load_presets(tmp_path)[0]['languages'] == 'script/Latin'
    models.delete_preset(tmp_path, row['name'])
    assert models.load_presets(tmp_path) == ()


def test_interrupted_preset_write_preserves_previous_bytes_and_cleans_stage(tmp_path, monkeypatch):
    models.store_preset(tmp_path, 'First', Config())
    before = (tmp_path / models.FILENAME).read_bytes()
    monkeypatch.setattr(models.os, 'replace', Mock(side_effect=OSError('simulated disk failure')))
    with pytest.raises(OSError, match='disk failure'):
        models.store_preset(tmp_path, 'Second', Config(languages='fra'))
    assert (tmp_path / models.FILENAME).read_bytes() == before
    assert not list(tmp_path.glob('.ocr-language-presets-*.tmp'))


def test_writer_collision_never_loses_existing_presets(tmp_path):
    from carr_library.ingest.writer_guard import exclusive_writer, WriterBusy
    models.store_preset(tmp_path, 'First', Config())
    before = (tmp_path / models.FILENAME).read_bytes()
    with exclusive_writer(tmp_path):
        with pytest.raises(WriterBusy):
            models.store_preset(tmp_path, 'Second', Config())
    assert (tmp_path / models.FILENAME).read_bytes() == before


def test_linked_preset_file_is_not_read_or_overwritten(tmp_path):
    original = tmp_path / 'unrelated-file'
    original.write_bytes(b'private bytes')
    os.link(original, tmp_path / models.FILENAME)
    with pytest.raises(ValueError, match='namespace'):
        models.load_presets(tmp_path)
    with pytest.raises(ValueError, match='namespace'):
        models.store_preset(tmp_path, 'Blocked', Config())
    assert original.read_bytes() == b'private bytes'


@pytest.mark.parametrize('raw', [b'{}', b'{"version":true,"presets":[]}',
    b'{"version":1,"version":1,"presets":[]}', b'{"version":1,"presets":[],"path":"outside"}',
    b'\xff', b'x' * (models.MAX_PRESET_BYTES + 1)],
    ids=('empty-object', 'boolean-version', 'duplicate', 'extra-field', 'invalid-utf8', 'over-budget'))
def test_invalid_or_unbounded_saved_preferences_fail_closed(raw):
    with pytest.raises(ValueError):
        models.validate_presets_bytes(raw)


def test_managed_presets_preserve_legacy_records_and_survive_actual_backup_restore(tmp_path):
    from carr_library.application import LibraryApp
    from carr_library.library_backup import restore_backup
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('OCR profile preservation')
    source = app.add_text(workspace, 'Retained original', 'Unmodified original source.')
    original = app.assets.resolve(source['asset']).read_bytes()
    models.store_preset(app.root, 'Legacy installed', Config(languages='ukr+eng'))
    first = models.load_presets(app.root)[0]
    assert json.loads((app.root / models.FILENAME).read_bytes())['version'] == 1
    pack = Config(languages='slk+eng', model_pack_path=str(tmp_path / 'reviewed-models'),
                  model_pack_sha256='a' * 64)
    models.store_preset(app.root, 'Existing reviewed pack', pack)
    second = models.load_presets(app.root)[1]
    assert json.loads((app.root / models.FILENAME).read_bytes())['version'] == 2
    models.store_preset(app.root, 'Managed Best', Config(languages='script/Latin+ukr', model_profile='best'))
    raw = (app.root / models.FILENAME).read_bytes()
    saved = models.validate_presets_bytes(raw)
    assert saved['version'] == 3 and saved['presets'][:2] == [first, second]
    assert saved['presets'][2]['model_profile'] == 'best'
    assert saved['presets'][2]['languages'] == 'script/Latin+ukr'
    receipt = app.backup(tmp_path / 'archive')
    restored = tmp_path / 'restored'
    restore_backup(tmp_path / 'archive', restored, expected_manifest_sha256=receipt)
    assert (restored / models.FILENAME).read_bytes() == raw
    assert models.load_presets(restored) == models.load_presets(app.root)
    assert app.assets.resolve(source['asset']).read_bytes() == original


@pytest.mark.parametrize('damage', ['unknown-profile', 'installed-marker', 'pinned-best', 'mixed-pack', 'old-version'])
def test_invalid_profile_bindings_do_not_change_a_valid_preset_file(tmp_path, damage):
    models.store_preset(tmp_path, 'Managed', Config(model_profile='best'))
    path = tmp_path / models.FILENAME
    original = path.read_bytes()
    data = json.loads(original)
    row = data['presets'][0]
    if damage == 'unknown-profile': row['model_profile'] = 'remote'
    elif damage == 'installed-marker': row['model_profile'] = 'installed'
    elif damage == 'pinned-best': row['model_policy'] = POLICY
    elif damage == 'mixed-pack':
        row.update(model_pack_path=str(tmp_path / 'models'), model_pack_sha256='a' * 64)
    elif damage == 'old-version': data['version'] = 2
    with pytest.raises(ValueError):
        models.validate_presets_bytes(json.dumps(data).encode())
    assert path.read_bytes() == original


def test_config_cannot_mix_a_managed_profile_and_an_existing_reviewed_pack(tmp_path):
    with pytest.raises(ValueError, match='cannot be combined'):
        Config(model_profile='fast', model_pack_path=str(tmp_path / 'pack'), model_pack_sha256='a' * 64)


def test_native_windows_script_separators_are_normalized_only_in_inventory(monkeypatch):
    _use_windows_report_paths(monkeypatch)
    assert models.parse_language_report(report('eng', r'script\Latin', 'osd')) == (
        'eng', 'script/Latin', 'osd')
    with pytest.raises(ValueError):
        models.model_ids(r'script\Latin')


@pytest.mark.parametrize('codes', [
    ('script/Latin', r'script\Latin'), (r'script\..\eng',),
    (r'C:\eng',), (r'\script\Latin',), (r'other\Latin',),
])
def test_native_separator_normalization_keeps_collision_and_path_guards(codes, monkeypatch):
    _use_windows_report_paths(monkeypatch)
    with pytest.raises(ValueError):
        models.parse_language_report(report(*codes))



def _use_windows_report_paths(monkeypatch):
    # Change only this module's OS binding, not global os.name/Path behavior.
    monkeypatch.setattr(models, 'os', SimpleNamespace(name='nt', path=models.os.path))


def test_windows_report_script_separator_is_canonicalized_only_at_engine_boundary(monkeypatch):
    _use_windows_report_paths(monkeypatch)
    assert models.parse_language_report(report('eng', r'script\EnglishFixture', 'osd')) == (
        'eng', 'script/EnglishFixture', 'osd')
    assert not models.valid_model_code(r'script\EnglishFixture')
    with pytest.raises(ValueError):
        models.split_languages(r'script\EnglishFixture')


@pytest.mark.parametrize('codes', [
    (r'script\EnglishFixture', 'script/EnglishFixture'),
    (r'script\EnglishFixture', 'script/englishfixture'),
], ids=('native-canonical-alias', 'case-folded-alias'))
def test_windows_report_rejects_duplicate_script_aliases_after_canonicalization(monkeypatch, codes):
    _use_windows_report_paths(monkeypatch)
    with pytest.raises(ValueError, match='invalid model inventory'):
        models.parse_language_report(report(*codes))


@pytest.mark.parametrize('code', [
    'script\\', r'script\\EnglishFixture', r'script\..\eng',
    r'script\Latin\eng', r'\script\Latin', r'\\host\share\eng',
    r'C:\models\eng', r'other\Latin', r'script\CON',
    'script\\Latin ', r'script\Latin --psm 1',
], ids=('missing-name', 'repeated-separator', 'parent-traversal', 'nested-path',
        'rooted-path', 'unc-path', 'drive-path', 'other-prefix', 'device-name',
        'trailing-whitespace', 'arguments'))
def test_windows_report_separator_handling_rejects_malformed_native_identifiers(monkeypatch, code):
    _use_windows_report_paths(monkeypatch)
    with pytest.raises(ValueError):
        models.parse_language_report(report('eng', code))


def test_posix_report_does_not_reinterpret_a_literal_backslash_filename(monkeypatch):
    monkeypatch.setattr(models, 'os', SimpleNamespace(name='posix', path=models.os.path))
    with pytest.raises(ValueError, match='invalid model inventory'):
        models.parse_language_report(report(r'script\EnglishFixture'))


def test_windows_report_keeps_exact_count_and_directory_binding(monkeypatch, tmp_path):
    _use_windows_report_paths(monkeypatch)
    listing = f'List of available languages in "{tmp_path}" (2):\nscript\\EnglishFixture\n'
    with pytest.raises(ValueError, match='invalid model inventory'):
        models.parse_language_report(listing, expected_directory=tmp_path)
    listing = f'List of available languages in "{tmp_path}" (1):\nscript\\EnglishFixture\n'
    with pytest.raises(ValueError, match='different model directory'):
        models.parse_language_report(listing, expected_directory=tmp_path / 'other')


def _diagnostic_engine_response(monkeypatch, listing):
    from carr_library.extract import ocr_process
    native = SimpleNamespace(executable='synthetic response fixture', portable=False,
        tessdata=Path('installed models'), manifest_sha256=None, assert_unchanged=Mock())
    monkeypatch.setattr(ocr_runtime, 'discover_ocr_runtime', lambda *args, **kwargs: native)
    execute = Mock(side_effect=[SimpleNamespace(stdout=raw) for raw in
        (b'tesseract 5.5.4\n', listing.encode('utf-8'), b'RESEARCH\n')])
    monkeypatch.setattr(ocr_process, 'run_ocr', execute)
    return native, execute


def test_runtime_diagnostic_preserves_windows_script_inventory_using_shared_parser(monkeypatch):
    _use_windows_report_paths(monkeypatch)
    native, execute = _diagnostic_engine_response(
        monkeypatch, report('eng', 'osd', r'script\EnglishFixture'))
    result = ocr_runtime.check_runtime()
    assert result['languages'] == ['eng', 'osd', 'script/EnglishFixture']
    assert execute.call_count == 3
    native.assert_unchanged.assert_called_once()


def test_runtime_diagnostic_rejects_invalid_entries_instead_of_silently_filtering(monkeypatch):
    _use_windows_report_paths(monkeypatch)
    native, execute = _diagnostic_engine_response(
        monkeypatch, report('eng', r'script\..\outside'))
    with pytest.raises(ValueError, match='invalid model inventory'):
        ocr_runtime.check_runtime()
    assert execute.call_count == 2
    native.assert_unchanged.assert_not_called()

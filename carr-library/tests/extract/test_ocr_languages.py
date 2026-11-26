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


@pytest.mark.parametrize('text', ['eng\nfra', 'List of available languages in "x" (2):\neng\n',
    'List of available languages in "x" (2):\neng\neng',
    'List of available languages in "x" (1):\n../eng',
    'List of available languages in "x" (1):\neng --psm 1',
    'x' * (models.MAX_REPORT_BYTES + 1)])
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
                       orientation=True, model_policy='default', model_profile='installed')
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
    b'\xff', b'x' * (models.MAX_PRESET_BYTES + 1)])
def test_invalid_or_unbounded_saved_preferences_fail_closed(raw):
    with pytest.raises(ValueError):
        models.validate_presets_bytes(raw)


def test_legacy_preset_load_and_backup_validation_preserve_exact_original_bytes(tmp_path):
    row = dict(name='Українська спочатку', languages='ukr+eng', model_policy='default', orientation=True)
    raw = json.dumps(dict(version=1, presets=[row]), ensure_ascii=False, indent=3).encode('utf-8') + b'\n\n'
    path = tmp_path / models.FILENAME
    path.write_bytes(raw)
    assert models.load_presets(tmp_path) == (row,)
    assert models.validate_presets_bytes(raw) == dict(version=1, presets=[row])
    assert path.read_bytes() == raw
    assert not list(tmp_path.glob('.ocr-language-presets-*.tmp'))


@pytest.mark.parametrize('profile', ['installed', 'fast', 'best'])
def test_new_preset_retains_explicit_profile_orientation_and_priority(tmp_path, profile):
    config = Config(languages='ukr+script/Latin+eng', model_profile=profile, orientation=True)
    models.store_preset(tmp_path, 'Exact selection', config)
    payload = json.loads((tmp_path / models.FILENAME).read_bytes())
    assert payload['version'] == 2
    row, = models.load_presets(tmp_path)
    assert row['model_profile'] == profile and row['orientation'] is True
    assert row['languages'] == config.languages


def test_explicit_new_save_upgrades_legacy_shape_but_keeps_its_installed_meaning(tmp_path):
    old = dict(name='Legacy', languages='ukr+eng', model_policy=POLICY, orientation=False)
    path = tmp_path / models.FILENAME
    path.write_text(json.dumps(dict(version=1, presets=[old])), encoding='utf-8')
    models.store_preset(tmp_path, 'Best', Config(languages='ukr', model_profile='best'))
    legacy, best = models.load_presets(tmp_path)
    assert legacy == dict(old, model_profile='installed')
    assert best['model_profile'] == 'best'
    assert json.loads(path.read_bytes())['version'] == 2


@pytest.mark.parametrize('version,profile,policy', [
    (1, 'fast', 'default'), (2, None, 'default'), (2, 'auto-download', 'default'),
    (2, [], 'default'), (2, 'best', POLICY),
])
def test_profile_schema_and_policy_cannot_be_silently_reinterpreted(version, profile, policy):
    row = dict(name='Invalid', languages='eng', orientation=False, model_policy=policy)
    if profile is not None:
        row['model_profile'] = profile
    with pytest.raises(ValueError):
        models.validate_presets_bytes(json.dumps(dict(version=version, presets=[row])).encode())

"""Local inventory processes and native-routing doubles; no NVDA assertion."""
from concurrent.futures import Future
from dataclasses import replace
from pathlib import Path
from threading import Event, Timer
from types import SimpleNamespace
from unittest.mock import Mock
import sys
import time

import pytest

from carr_library.extract.page_ocr import Config
from carr_library.extract.pinned_tessdata import POLICY
from carr_library.ui import ocr_selection as selection


def listing(identifiers, directory='/trusted/models'):
    return (f'List of available languages in "{directory}" ({len(identifiers)}):\n'
            + '\n'.join(identifiers) + '\n').encode('utf-8')


def test_inventory_parser_preserves_script_identity_case_and_order():
    identifiers = ('ukr', 'Latin', 'script/Latin', 'osd')
    assert selection.parse_installed_models(listing(identifiers),
        expected_directory='/trusted/models/') == identifiers


@pytest.mark.parametrize('data', [
    b'', b'not a listing', listing(('eng', 'eng')), listing(('script/../../bad',)),
    listing((' eng',)), listing(('eng+ukr',)), listing(('script/Latin/extra',)),
    listing(('eng',)).replace(b'(1)', b'(9)'), b'\xff',
    b'x' * (selection.MAX_INVENTORY_BYTES + 1),
], ids=('empty', 'not-listing', 'duplicate', 'traversal', 'leading-space',
        'combined-id', 'nested-path', 'wrong-count', 'invalid-utf8', 'over-budget'))
def test_untrusted_or_incomplete_model_listing_is_rejected(data):
    with pytest.raises(ValueError):
        selection.parse_installed_models(data)


def test_inventory_parser_rejects_different_bound_model_directory():
    with pytest.raises(ValueError, match='different model directory'):
        selection.parse_installed_models(listing(('eng',)), expected_directory='/other/models')


def runtime_program(monkeypatch, tmp_path, program):
    runtime = SimpleNamespace(executable=sys.executable, root=tmp_path, tessdata=tmp_path,
        command=Mock(side_effect=lambda arguments: [sys.executable, '-c',
            "print('tesseract 5.5.0')" if arguments[-1] == '--version' else program]),
        assert_unchanged=Mock(), portable=False, manifest_sha256=None)
    discover = Mock(return_value=runtime)
    monkeypatch.setattr('carr_library.extract.ocr_runtime.discover_ocr_runtime', discover)
    return runtime, discover


def test_actual_subprocess_inventory_uses_trusted_runtime_and_bound_directory(monkeypatch, tmp_path):
    raw = listing(('ukr', 'script/Latin', 'osd'), str(tmp_path))
    runtime, discover = runtime_program(monkeypatch, tmp_path,
        f'import sys; sys.stdout.buffer.write({raw!r})')
    assert selection.discover_installed_models() == ('ukr', 'script/Latin', 'osd')
    assert [call.args[0][-1] for call in runtime.command.call_args_list] == ['--version', '--list-langs']
    assert discover.call_args.args == ()
    assert callable(discover.call_args.kwargs['remaining'])
    runtime.assert_unchanged.assert_called_once()


def test_actual_inventory_process_has_bounded_stdout(monkeypatch, tmp_path):
    runtime_program(monkeypatch, tmp_path, "import sys; sys.stdout.write('x' * 1000000)")
    with pytest.raises(ValueError, match='output limit'):
        selection.discover_installed_models()


def test_actual_inventory_process_has_bounded_stderr(monkeypatch, tmp_path):
    runtime_program(monkeypatch, tmp_path, "import sys; sys.stderr.write('x' * 1000000)")
    with pytest.raises(ValueError, match='output limit'):
        selection.discover_installed_models()


def test_actual_inventory_process_is_killed_at_deadline(monkeypatch, tmp_path):
    runtime_program(monkeypatch, tmp_path, 'import time; time.sleep(15)')
    began = time.monotonic()
    with pytest.raises(TimeoutError, match='timed out'):
        selection.discover_installed_models(timeout=0.2)
    assert time.monotonic() - began < 5


def test_actual_inventory_process_cooperatively_cancels(monkeypatch, tmp_path):
    runtime_program(monkeypatch, tmp_path, 'import time; time.sleep(15)')
    cancel = Event()
    timer = Timer(0.15, cancel.set)
    timer.start()
    try:
        began = time.monotonic()
        with pytest.raises(InterruptedError):
            selection.discover_installed_models(cancelled=cancel.is_set)
        assert time.monotonic() - began < 5
    finally:
        timer.cancel()


def test_missing_runtime_is_an_explicit_setup_error(monkeypatch):
    monkeypatch.setattr('carr_library.extract.ocr_runtime.discover_ocr_runtime', lambda *a, **k: None)
    with pytest.raises(selection.OcrEngineMissingError, match='No local OCR runtime'):
        selection.discover_installed_models()


def test_selectable_rows_label_known_models_exclude_auxiliary_and_preserve_initial_order():
    rows = selection.selectable_models('installed', ('eng', 'ukr', 'script/Latin', 'osd', 'equ', 'custom_1'),
                                      initial=Config(languages='ukr+eng'))
    assert [row[0] for row in rows[:2]] == ['ukr', 'eng']
    assert {row[0] for row in rows} == {'eng', 'ukr', 'script/Latin', 'custom_1'}
    assert any('Ukrainian' in row[1] for row in rows)
    assert any(row[0] == 'script/Latin' and 'script' in row[1] for row in rows)


def test_managed_profile_cannot_select_unknown_catalogue_models():
    with pytest.raises(ValueError, match='catalogue'):
        selection.selectable_models('fast', ('custom_1',))


def test_config_keeps_resource_limits_and_uses_selected_profile_ids_and_orientation():
    initial = Config(languages='ukr', dpi=150, timeout=73, max_pixels=123456, orientation=False)
    result = selection.selected_config('best', ('ukr', 'script/Latin'),
        ('eng', 'ukr', 'script/Latin', 'osd'), orientation=True, initial=initial)
    assert result == replace(initial, languages='ukr+script/Latin', model_profile='best', orientation=True)


@pytest.mark.parametrize('chosen,available,orientation', [
    ((), ('eng',), False), (('eng', 'eng'), ('eng',), False),
    (('ukr',), ('eng',), False), (('osd',), ('osd',), False),
    (('equ',), ('equ',), False), (('eng',), ('eng',), True),
    (('script/../../bad',), ('script/../../bad',), False),
    ('eng', ('eng',), False), (('eng',), 'eng', False),
])
def test_invalid_or_auxiliary_model_selection_cannot_create_config(chosen, available, orientation):
    with pytest.raises(ValueError):
        selection.selected_config('installed', chosen, available, orientation=orientation)


def test_compatible_pinned_policy_survives_but_explicit_incompatible_choice_uses_default():
    initial = Config(languages='ukr+eng', model_policy=POLICY)
    result = selection.selected_config('installed', ('ukr', 'eng'), ('eng', 'ukr'), initial=initial)
    assert result.model_policy == POLICY
    assert selection.selected_config('best', ('eng',), ('eng',), initial=initial).model_policy == 'default'
    assert selection.selected_config('installed', ('deu',), ('deu',), initial=initial).model_policy == 'default'

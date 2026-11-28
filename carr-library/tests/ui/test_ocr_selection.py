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

from carr_library.extract.page_ocr import Config, ExtendedConfig
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
        command=Mock(side_effect=lambda arguments: [sys.executable, '-c', program]),
        assert_unchanged=Mock())
    discover = Mock(return_value=runtime)
    monkeypatch.setattr('carr_library.extract.ocr_runtime.discover_ocr_runtime', discover)
    return runtime, discover


def test_actual_subprocess_inventory_uses_trusted_runtime_and_bound_directory(monkeypatch, tmp_path):
    raw = listing(('ukr', 'script/Latin', 'osd'), str(tmp_path))
    runtime, discover = runtime_program(monkeypatch, tmp_path,
        f'import sys; sys.stdout.buffer.write({raw!r})')
    assert selection.discover_installed_models() == ('ukr', 'script/Latin', 'osd')
    runtime.command.assert_called_once_with([sys.executable, '--list-langs'])
    assert discover.call_args.args == ('eng',)
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


class Control:
    def __init__(self, *args, **kwargs):
        self.value = kwargs.get('value', '')
        self.items = kwargs.get('choices', ())
        self.selection = -1
        self.checked = set()
        self.enabled = True
        self.focused = self.destroyed = False
        self.bindings = {}
        self.name = ''
        self.modal_result = None
    def SetValue(self, value): self.value = value
    def GetValue(self): return self.value
    def SetName(self, value): self.name = value
    def SetSelection(self, index): self.selection = index
    def GetSelection(self): return self.selection
    def Check(self, index, value=True):
        self.checked.add(index) if value else self.checked.discard(index)
    def IsChecked(self, index): return index in self.checked
    def SetFocus(self): self.focused = True
    def Enable(self, value): self.enabled = value
    def Bind(self, event, callback): self.bindings[event] = callback
    def Add(self, *args): pass
    def Wrap(self, width): pass
    def SetSizer(self, value): pass
    def SetMinSize(self, value): pass
    def SetEscapeId(self, value): self.escape_id = value
    def SetDefault(self): pass
    def SetOKCancelLabels(self, accepted, cancelled): self.button_labels = accepted, cancelled
    def EndModal(self, result): self.modal_result = result
    def Destroy(self): self.destroyed = True


@pytest.fixture
def wx(monkeypatch):
    wx = SimpleNamespace(on_show=lambda dialog: dialog.accept(), dialogs=[], profile_index=0,
                         profile_result=None, missing_engine_result=None, questions=[],
                         MessageBox=Mock(), callbacks=[])
    names = ('DEFAULT_DIALOG_STYLE', 'RESIZE_BORDER', 'VERTICAL', 'HORIZONTAL', 'EXPAND',
             'ALL', 'ALIGN_RIGHT', 'TE_READONLY', 'TE_MULTILINE', 'ID_CANCEL', 'ID_OK',
             'EVT_BUTTON', 'EVT_CLOSE', 'OK', 'ICON_ERROR', 'CANCEL', 'ICON_INFORMATION')
    for index, name in enumerate(names):
        setattr(wx, name, 1 << index)
    class Dialog(Control):
        def ShowModal(self):
            wx.dialogs.append(self)
            wx.on_show(self)
            return self.modal_result
    class ProfileChoice(Control):
        def ShowModal(self):
            return wx.profile_result if wx.profile_result is not None else wx.ID_OK
        def GetSelection(self): return wx.profile_index
    class Question(Control):
        def ShowModal(self):
            wx.questions.append(self)
            return wx.missing_engine_result if wx.missing_engine_result is not None else wx.ID_CANCEL
    wx.Dialog, wx.SingleChoiceDialog = Dialog, ProfileChoice
    wx.MessageDialog = Question
    for name in ('TextCtrl', 'CheckListBox', 'CheckBox', 'Button', 'StaticText', 'BoxSizer'):
        setattr(wx, name, Control)
    wx.CallAfter = lambda callback, *args: wx.callbacks.append(lambda: callback(*args))
    monkeypatch.setitem(sys.modules, 'wx', wx)
    return wx


def test_native_selection_defaults_to_only_english_and_excludes_auxiliary(wx):
    result = selection._choose_languages(None, 'installed', ('ukr', 'eng', 'osd', 'equ'), Config())
    dialog = wx.dialogs[-1]
    assert result.languages == 'eng'
    assert not result.orientation
    assert len(dialog.entries.checked) == 1
    assert len(dialog.entries.items) == 2
    assert dialog.entries.focused and dialog.destroyed
    assert dialog.orientation.enabled
    assert 'does not identify languages' in dialog.status.value


def test_native_selection_chooses_one_non_english_model_and_explains_absent_osd(wx):
    result = selection._choose_languages(None, 'installed', ('ukr', 'slk'), Config())
    dialog = wx.dialogs[-1]
    assert result.languages in ('ukr', 'slk') and '+' not in result.languages
    assert not dialog.orientation.enabled
    assert 'osd is not installed' in dialog.status.value


def test_native_manual_mixed_selection_keeps_script_ids_and_orientation(wx):
    def choose(dialog):
        dialog.entries.checked = {0, 1}
        dialog.orientation.SetValue(True)
        dialog.accept()
    wx.on_show = choose
    result = selection._choose_languages(None, 'fast', ('ukr', 'script/Latin', 'osd'),
                                        Config(languages='ukr+script/Latin'))
    assert result.languages == 'ukr+script/Latin'
    assert result.orientation and result.model_profile == 'fast'


def test_native_empty_selection_stays_open_with_readable_error(wx):
    def choose(dialog):
        dialog.entries.checked.clear()
        dialog.accept()
        assert dialog.result is None
        assert dialog.modal_result is None
        assert dialog.status.focused
        dialog.EndModal(wx.ID_CANCEL)
    wx.on_show = choose
    assert selection._choose_languages(None, 'installed', ('eng',), Config()) is None


def test_profile_cancel_never_discovers_or_opens_model_store(wx, monkeypatch):
    wx.profile_result = wx.ID_CANCEL
    discovery = Mock()
    monkeypatch.setattr(selection, '_discover_with_dialog', discovery)
    assert selection.choose_model_config(None) is None
    discovery.assert_not_called()


def test_installed_profile_routes_to_background_inventory_and_respects_cancellation(wx, monkeypatch):
    discovery = Mock(return_value=None)
    monkeypatch.setattr(selection, '_discover_with_dialog', discovery)
    assert selection.choose_model_config(None) is None
    discovery.assert_called_once_with(None)
    assert wx.dialogs == []


def test_managed_profile_uses_store_inventory_and_never_runs_system_discovery(wx, monkeypatch):
    wx.profile_index = 2
    store = SimpleNamespace(available=Mock(return_value=('eng', 'osd')))
    monkeypatch.setattr('carr_library.extract.ocr_model_store.OcrModelStore', lambda: store)
    discovery = Mock(side_effect=AssertionError('Managed inventory must not execute a runtime'))
    monkeypatch.setattr(selection, '_discover_with_dialog', discovery)
    result = selection.choose_model_config(None)
    assert result.model_profile == 'best' and result.languages == 'eng'
    store.available.assert_called_once_with('best')
    discovery.assert_not_called()


@pytest.mark.parametrize('index', [-1, 3, 999])
def test_invalid_native_profile_index_is_explained_not_executed(wx, index):
    wx.profile_index = index
    assert selection.choose_model_config(None) is None
    assert 'valid OCR model profile' in wx.MessageBox.call_args.args[0]


def test_empty_managed_profile_explains_setup_without_choosing_auxiliary_as_text(wx, monkeypatch):
    wx.profile_index = 1
    monkeypatch.setattr('carr_library.extract.ocr_model_store.OcrModelStore',
                        lambda: SimpleNamespace(available=lambda profile: ('osd',)))
    assert selection.choose_model_config(None) is None
    assert 'offline OCR model manager' in wx.MessageBox.call_args.args[0]
    assert wx.questions == []


def test_missing_engine_requires_explicit_continue_and_preserves_initial_config(wx, monkeypatch):
    monkeypatch.setattr(selection, '_discover_with_dialog',
                        Mock(side_effect=selection.OcrEngineMissingError('No runtime')))
    initial = Config(languages='ukr', dpi=150, timeout=60)
    assert selection.choose_model_config(None, initial=initial) is None
    assert wx.questions[-1].button_labels == ('Continue with text extraction', 'Cancel')
    assert wx.questions[-1].destroyed
    wx.missing_engine_result = wx.ID_OK
    assert selection.choose_model_config(None, initial=initial) == initial


@pytest.mark.parametrize('error', [ValueError('Invalid portable OCR manifest'),
                                 RuntimeError('OCR executable identity changed')])
def test_broken_runtime_never_offers_native_only_fallback(wx, monkeypatch, error):
    monkeypatch.setattr(selection, '_discover_with_dialog', Mock(side_effect=error))
    assert selection.choose_model_config(None) is None
    assert wx.questions == []
    assert str(error) in wx.MessageBox.call_args.args[0]


def test_missing_managed_profile_does_not_fallback_to_installed_model_settings(wx, monkeypatch):
    wx.profile_index = 1
    monkeypatch.setattr('carr_library.extract.ocr_model_store.OcrModelStore',
        lambda: SimpleNamespace(available=Mock(side_effect=selection.OcrEngineMissingError('Missing pack'))))
    assert selection.choose_model_config(None) is None
    assert wx.questions == []


def test_explicit_native_only_choice_reads_real_pdf_but_keeps_raster_page_unverified(wx, monkeypatch, tmp_path):
    from PIL import Image, ImageDraw
    from reportlab.pdfgen.canvas import Canvas
    from reportlab.lib.utils import ImageReader
    from carr_library.extract.page_ocr import extract_page
    source = tmp_path / 'native-and-raster.pdf'
    pdf = Canvas(str(source))
    pdf.drawString(72, 700, 'Native source remains readable.')
    pdf.showPage()
    scan = Image.new('RGB', (240, 80), 'white')
    ImageDraw.Draw(scan).text((10, 20), 'Scanned source needs OCR', fill='black')
    pdf.drawImage(ImageReader(scan), 72, 500, 360, 120)
    pdf.showPage()
    pdf.save()
    monkeypatch.setattr(selection, '_discover_with_dialog',
                        Mock(side_effect=selection.OcrEngineMissingError('No runtime')))
    monkeypatch.setattr('carr_library.extract.page_ocr.discover_ocr_runtime', lambda *a, **k: None)
    wx.missing_engine_result = wx.ID_OK
    config = selection.choose_model_config(None)
    assert config == Config()
    native = extract_page(source, 1, config)
    assert native['raw_text'] == 'Native source remains readable.\n'
    assert native['provenance']['method'] == 'native'
    assert native['status'] == 'candidate' and not native['printed_label_verified']
    raster = extract_page(source, 2, config)
    assert raster['status'] == 'review_required'
    assert 'ocr_engine_missing' in raster['warnings']
    assert not raster['printed_label_verified']


def test_inventory_dialog_runs_future_off_ui_thread_and_can_cancel(wx, monkeypatch):
    calls = []
    class Executor:
        def __init__(self, **kwargs): pass
        def submit(self, operation, **kwargs):
            future = Future()
            calls.append((operation, kwargs, future))
            return future
        def shutdown(self, **kwargs): pass
    monkeypatch.setattr(selection, 'ThreadPoolExecutor', Executor)
    operation = Mock(return_value=('eng',))
    monkeypatch.setattr(selection, 'discover_installed_models', operation)
    def cancel(dialog):
        operation.assert_not_called()
        assert dialog.status.focused
        dialog.request_cancel()
        assert calls[0][1]['cancelled']()
        calls[0][2].set_exception(InterruptedError('Cancelled'))
        while wx.callbacks:
            wx.callbacks.pop(0)()
    wx.on_show = cancel
    assert selection._discover_with_dialog(None) is None
    assert wx.dialogs[-1].destroyed


def test_current_native_picker_preserves_special_model_filter_and_mixed_selection(wx):
    def choose(dialog):
        assert len(dialog.entries.items) == 2
        assert 'eng' in dialog.entries.items[0]
        assert 'script/Latin' in dialog.entries.items[1]
        assert dialog.entries.checked == {0}
        assert dialog.entries.focused
        dialog.entries.Check(1)
        dialog.accept()
    wx.on_show = choose
    result = selection._choose_languages(None, 'installed',
        ('eng', 'equ', 'osd', 'script/Latin'), Config())
    assert result.languages == 'eng+script/Latin'
    assert not result.orientation
    assert wx.dialogs[-1].destroyed


def test_automatic_language_selection_requires_explicit_checkbox_and_manual_override(wx):
    def choose(dialog):
        assert not dialog.automatic.GetValue()
        dialog.automatic.SetValue(True)
        dialog.accept()
    wx.on_show = choose
    result = selection._choose_languages(None, 'installed', ('eng', 'ukr'), Config(languages='eng+ukr'))
    assert result.automatic_languages is True and result.languages == 'eng+ukr'
    assert wx.dialogs[-1].automatic.name.startswith('Automatic OCR language detection')
    def manual(dialog):
        assert dialog.automatic.GetValue() is True
        dialog.automatic.SetValue(False)
        dialog.accept()
    wx.on_show = manual
    selected = selection._choose_languages(None, 'installed', ('eng', 'ukr'), result)
    assert selected.automatic_languages is False and selected.languages == result.languages


def test_automatic_selection_cancellation_does_not_change_the_prior_frozen_config(wx):
    initial = ExtendedConfig(languages='eng', automatic_languages=False)
    def cancel(dialog):
        dialog.automatic.SetValue(True)
        dialog.EndModal(wx.ID_CANCEL)
    wx.on_show = cancel
    assert selection._choose_languages(None, 'installed', ('eng',), initial) is None
    assert initial == ExtendedConfig(languages='eng', automatic_languages=False)


def test_automatic_pool_limit_is_visible_and_never_accepts_invalid_native_selection(wx):
    inventory = tuple('model' + str(index) for index in range(9))
    def over_budget(dialog):
        dialog.entries.checked = set(range(9))
        dialog.automatic.SetValue(True)
        dialog.accept()
        assert dialog.modal_result is None
        assert 'eight' in dialog.status.GetValue()
        assert dialog.status.focused
        dialog.EndModal(wx.ID_CANCEL)
    wx.on_show = over_budget
    assert selection._choose_languages(None, 'installed', inventory, Config(languages=inventory[0])) is None

"""Ordered language intent and async UI handlers; native/NVDA tested separately."""
from concurrent.futures import Future
from types import SimpleNamespace
from threading import Event
from unittest.mock import Mock
import sys

import pytest

from carr_library.extract.ocr_languages import LanguageCatalog, store_preset, FILENAME
from carr_library.extract.page_ocr import Config
from carr_library.ui.ocr_language_dialog import OcrOptionsActions


class Control:
    def __init__(self, value='', selection=-1):
        self.value, self.selection = value, selection
        self.items, self.enabled, self.focused = [], True, False

    def SetValue(self, value): self.value = value
    def GetValue(self): return self.value
    def SetSelection(self, index): self.selection = index
    def GetSelection(self): return self.selection
    def SetItems(self, items): self.items, self.selection = list(items), -1
    def SetFocus(self): self.focused = True
    def Enable(self): self.enabled = True
    def Disable(self): self.enabled = False
    def SetLabel(self, label): self.label = label


@pytest.fixture
def dialog(monkeypatch, tmp_path):
    pending = []
    monkeypatch.setitem(sys.modules, 'wx', SimpleNamespace(ID_OK=1, CallAfter=lambda fn: pending.append(fn)))
    view = OcrOptionsActions()
    view.initial = Config(languages='eng', dpi=200, timeout=45)
    view.selected_codes = ['eng']
    view.catalog = LanguageCatalog(('eng', 'fra', 'ukr', 'slk', 'osd', 'script/Latin'),
                                   'tesseract 5.5.0', 'system_installation')
    view.busy = view.closed = False
    view.cancel_event = Event()
    view.result = None
    view.filter, view.available, view.chosen = Control(), Control(), Control()
    view.profile, view.orientation, view.presets = Control(selection=0), Control(False), Control()
    view.automatic = Control(False)
    view.status, view.ok, view.rescan_button = Control(), Control(), Control()
    view.preset_root = tmp_path
    view.EndModal = Mock()
    view.future = Future()
    view.executor = SimpleNamespace(submit=Mock(return_value=view.future))
    view.pending = pending
    view.filter_models()
    view._show_selected()
    view._presets()
    return view


def test_filter_does_not_drop_hidden_selected_models_and_order_reaches_real_config(dialog):
    dialog.filter.SetValue('French')
    dialog.filter_models(object())
    assert dialog.visible_codes == ('fra',)
    dialog.add_model()
    assert dialog.selected_codes == ['eng', 'fra']
    dialog.move_model(-1)
    assert dialog.selected_codes == ['fra', 'eng']
    dialog.filter.SetValue('nothing installed')
    dialog.filter_models(object())
    assert dialog.visible_codes == () and dialog.selected_codes == ['fra', 'eng']
    dialog.orientation.SetValue(True)
    dialog.accept()
    assert dialog.result == Config(languages='fra+eng', dpi=200, timeout=45, orientation=True)
    dialog.EndModal.assert_called_once_with(1)


def test_unavailable_model_does_not_silently_fall_back_to_english(dialog):
    dialog.selected_codes = ['deu']
    dialog.accept()
    dialog.EndModal.assert_not_called()
    assert dialog.result is None and 'not installed: deu' in dialog.status.value
    assert dialog.status.focused


def test_empty_choice_invalid_pinned_profile_and_missing_osd_keep_dialog_open(dialog):
    dialog.selected_codes = []
    dialog.accept()
    assert 'at least one' in dialog.status.value
    dialog.selected_codes = ['fra']
    dialog.profile.SetSelection(1)
    dialog.accept()
    assert 'pinned' in dialog.status.value
    dialog.profile.SetSelection(0)
    dialog.catalog = LanguageCatalog(('eng', 'fra'), integrity='system_installation')
    dialog.orientation.SetValue(True)
    dialog.accept()
    assert 'OSD' in dialog.status.value
    dialog.EndModal.assert_not_called()


def test_saved_preset_reopens_with_order_and_missing_models_cannot_replace_current_choice(dialog):
    store_preset(dialog.preset_root, 'French first', Config(languages='fra+eng'))
    store_preset(dialog.preset_root, 'Not installed here', Config(languages='deu'))
    dialog._presets()
    dialog.presets.SetSelection(next(i for i, row in enumerate(dialog.preset_items) if row[2] == 'French first'))
    dialog.apply_preset()
    assert dialog.selected_codes == ['fra', 'eng']
    dialog.presets.SetSelection(next(i for i, row in enumerate(dialog.preset_items) if row[2] == 'Not installed here'))
    dialog.apply_preset()
    assert dialog.selected_codes == ['fra', 'eng']
    assert 'not installed' in dialog.status.value


def test_async_discovery_disables_accept_until_main_thread_receives_verified_result(dialog):
    catalog = dialog.catalog
    dialog.rescan()
    assert dialog.busy and not dialog.ok.enabled and not dialog.rescan_button.enabled
    dialog.accept()
    dialog.EndModal.assert_not_called()
    dialog.future.set_result(catalog)
    assert dialog.busy and not dialog.ok.enabled
    dialog.pending.pop(0)()
    assert not dialog.busy and dialog.ok.enabled and dialog.rescan_button.enabled
    assert 'publisher authenticity is not verified' in dialog.status.value


def test_destroyed_dialog_drops_late_native_discovery_result(dialog):
    catalog = dialog.catalog
    dialog.rescan()
    dialog.future.set_result(catalog)
    dialog.closed = True
    dialog.pending.pop(0)()
    assert not dialog.ok.enabled


def test_invalid_runtime_does_not_leave_previous_valid_inventory_usable(dialog):
    dialog.rescan()
    dialog.future.set_exception(ValueError('Model checksum mismatch.'))
    dialog.pending.pop(0)()
    assert dialog.catalog is None and not dialog.ok.enabled
    assert dialog.available.items == [] and 'checksum mismatch' in dialog.status.value
    dialog.accept()
    dialog.EndModal.assert_not_called()


def test_missing_engine_reports_native_text_only_without_claiming_ocr(dialog):
    dialog.rescan()
    dialog.future.set_result(LanguageCatalog(()))
    dialog.pending.pop(0)()
    assert 'No local OCR engine' in dialog.status.value and 'scanned pages will remain for review' in dialog.status.value
    dialog.accept()
    assert dialog.result == dialog.initial


def test_invalid_preset_file_is_preserved_but_does_not_disable_manual_model_selection(dialog):
    path = dialog.preset_root / FILENAME
    path.write_bytes(b'not valid presets')
    catalog = dialog.catalog
    dialog.rescan()
    dialog.future.set_result(catalog)
    dialog.pending.pop(0)()
    assert dialog.ok.enabled and 'Manual model selection is available' in dialog.status.value
    assert path.read_bytes() == b'not valid presets'
    dialog.accept()
    assert dialog.result == dialog.initial


@pytest.mark.parametrize('profile,index', [('fast', 1), ('best', 2)])
def test_ordered_chooser_captures_managed_profile_and_preserves_resource_limits(dialog, profile, index):
    dialog.model_source = Control(selection=index)
    dialog.catalog = LanguageCatalog(('ukr', 'eng', 'osd'), integrity='managed_inventory',
                                    model_profile=profile)
    dialog.selected_codes = ['ukr', 'eng']
    dialog.orientation.SetValue(True)
    dialog.accept()
    assert dialog.result == Config(languages='ukr+eng', model_profile=profile,
                                   dpi=200, timeout=45, orientation=True)


def test_profile_switch_cannot_reuse_previous_inventory_or_preset_silently(dialog):
    dialog.model_source = Control(selection=2)
    dialog.accept()
    assert dialog.result is None and 'model set changed' in dialog.status.value
    store_preset(dialog.preset_root, 'Managed best', Config(languages='ukr', model_profile='best'))
    dialog.model_source.SetSelection(0)
    dialog._presets()
    dialog.presets.SetSelection(next(i for i, row in enumerate(dialog.preset_items) if row[2] == 'Managed best'))
    before = list(dialog.selected_codes)
    dialog.apply_preset()
    assert dialog.selected_codes == before and 'preset uses Best' in dialog.status.value


def test_explicit_automatic_choice_keeps_order_and_resource_limits(dialog):
    dialog.selected_codes = ['ukr', 'eng']
    dialog.automatic.SetValue(True)
    dialog.accept()
    assert dialog.result == Config(languages='ukr+eng', dpi=200, timeout=45,
                                   automatic_languages=True)
    assert dialog.initial.automatic_languages is False
    dialog.EndModal.assert_called_once_with(1)


def test_automatic_preset_round_trip_and_legacy_manual_override(dialog):
    store_preset(dialog.preset_root, 'Automatic pool',
                 Config(languages='ukr+eng', automatic_languages=True))
    store_preset(dialog.preset_root, 'Manual French', Config(languages='fra'))
    dialog._presets()
    for label, expected, languages in [('Automatic pool', True, ['ukr', 'eng']),
                                        ('Manual French', False, ['fra'])]:
        dialog.presets.SetSelection(next(i for i, row in enumerate(dialog.preset_items)
                                         if row[2] == label))
        dialog.apply_preset()
        assert dialog.selected_codes == languages
        assert dialog.automatic.GetValue() is expected
    dialog.accept()
    assert dialog.result == Config(languages='fra', dpi=200, timeout=45)

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


@pytest.fixture
def pack_dialog(dialog, tmp_path):
    dialog.active_pack = None
    dialog.pack_profile = Control(selection=0)
    dialog.pack_codes = Control('eng ukr slk')
    dialog.pack_budget = Control('256')
    dialog.pack_source = Control(str(tmp_path / 'models'))
    dialog.pack_prepared = Control(str(tmp_path / 'prepared'))
    dialog.pack_install_source = Control(str(tmp_path / 'prepared'))
    dialog.pack_installed = Control(str(tmp_path / 'installed'))
    dialog.pack_digest = Control('a' * 64)
    dialog.pack_filter, dialog.pack_models = Control(), Control()
    dialog.pack_stop = Control()
    dialog.pack_controls = [dialog.pack_source, dialog.pack_prepared, dialog.pack_digest,
                            dialog.pack_installed, dialog.pack_codes, dialog.pack_budget]
    return dialog


def test_pack_preparation_uses_explicit_snapshot_without_activating_models(pack_dialog, monkeypatch):
    view = pack_dialog
    prepare = Mock(return_value=SimpleNamespace(path=view.pack_prepared.value, models=('eng', 'slk', 'ukr'),
                                                profile='fast', manifest_sha256='b' * 64))
    monkeypatch.setattr('carr_library.extract.ocr_model_catalog.prepare_offline_pack', prepare)
    source = view.pack_source.value
    view.prepare_pack()
    assert view.busy and not view.ok.enabled and all(not control.enabled for control in view.pack_controls)
    work = view.executor.submit.call_args.args[0]
    view.pack_source.value = 'Changed after request snapshot'
    result = work()
    assert prepare.call_args.args == (source, view.pack_prepared.value, ['eng', 'ukr', 'slk'])
    assert prepare.call_args.kwargs['max_bytes'] == 256 * 1024**2
    view.future.set_result(result)
    assert view.pack_digest.value == 'a' * 64
    view.pending.pop(0)()
    assert not view.busy and view.pack_digest.value == 'b' * 64
    assert view.active_pack is None and 'Keep this manifest SHA256 separately' in view.status.value


@pytest.mark.parametrize('value', ['0', '-1', '4097', 'all', '1.5'])
def test_invalid_pack_budget_does_not_start_preparation(pack_dialog, value):
    pack_dialog.pack_budget.value = value
    pack_dialog.prepare_pack()
    pack_dialog.executor.submit.assert_not_called()
    assert '1 to 4096' in pack_dialog.status.value


def test_installation_requires_separate_use_and_retains_manifest_identity(pack_dialog, monkeypatch):
    view = pack_dialog
    install = Mock(return_value=dict(path=view.pack_installed.value, manifest_sha256='a' * 64, models=3))
    monkeypatch.setattr('carr_library.extract.language_manager.install_offline_pack', install)
    view.install_pack()
    result = view.executor.submit.call_args.args[0]()
    assert install.call_args.kwargs['expected_manifest_sha256'] == 'a' * 64
    view.future.set_result(result)
    view.pending.pop(0)()
    assert view.active_pack is None and 'Choose Use verified pack' in view.status.value


def test_pack_activation_binds_only_verified_inventory_and_preserves_language_intent(pack_dialog, monkeypatch):
    view = pack_dialog
    catalogue = LanguageCatalog(('ukr', 'slk'), 'tesseract 5.5.0', 'verified_model_pack', 'a' * 64)
    discover = Mock(return_value=catalogue)
    monkeypatch.setattr('carr_library.ui.ocr_language_dialog.discover_languages', discover)
    view.use_pack()
    result = view.executor.submit.call_args.args[0]()
    assert discover.call_args.kwargs['model_pack_path'] == view.pack_installed.value
    view.future.set_result(result)
    view.pending.pop(0)()
    assert view.active_pack == (view.pack_installed.value, 'a' * 64)
    assert view.selected_codes == ['eng']  # No silent fallback to another language.
    view.accept()
    assert 'not installed: eng' in view.status.value
    view.selected_codes = ['ukr']
    view.accept()
    assert view.result.model_pack_sha256 == 'a' * 64
    assert view.result.model_pack_path == view.pack_installed.value
    assert view.result.languages == 'ukr'


def test_late_cancelled_pack_verification_does_not_activate(pack_dialog):
    view = pack_dialog
    view.use_pack()
    view.future.set_result(LanguageCatalog(('eng',), integrity='verified_model_pack'))
    view.stop_pack()
    view.pending.pop(0)()
    assert view.active_pack is None and 'selection is unchanged' in view.status.value


def test_pack_verification_failure_preserves_previous_selection(pack_dialog):
    view = pack_dialog
    previous = view.catalog
    view.use_pack()
    view.future.set_exception(ValueError('Checksum mismatch'))
    view.pending.pop(0)()
    assert view.catalog is previous and view.active_pack is None
    assert view.ok.enabled and 'Checksum mismatch' in view.status.value


def test_successful_pack_check_recovers_acceptance_after_initial_discovery_failure(pack_dialog):
    view = pack_dialog
    view.catalog = None
    view.ok.Disable()
    view.use_pack()
    view.future.set_result(LanguageCatalog(('eng',), integrity='verified_model_pack'))
    view.pending.pop(0)()
    assert view.ok.enabled and view.current_config().model_pack_path == view.pack_installed.value


def test_builtin_language_preset_keeps_selected_pack_binding(pack_dialog):
    view = pack_dialog
    view.active_pack = (view.pack_installed.value, 'a' * 64)
    view.catalog = LanguageCatalog(('eng', 'ukr', 'slk'), integrity='verified_model_pack')
    view._presets()
    view.presets.selection = next(i for i, row in enumerate(view.preset_items) if row[0] == 'Ukrainian')
    view.apply_preset()
    view.executor.submit.assert_not_called()
    assert view.current_config().model_pack_path == view.pack_installed.value
    assert view.current_config().languages == 'ukr'


def test_saved_pack_preset_revalidates_before_replacing_current_selection(pack_dialog):
    view = pack_dialog
    config = Config(languages='ukr', model_pack_path=view.pack_installed.value, model_pack_sha256='a' * 64)
    store_preset(view.preset_root, 'Reviewed model pack', config)
    view._presets()
    view.presets.selection = next(i for i, row in enumerate(view.preset_items) if row[2] == 'Reviewed model pack')
    view.apply_preset()
    assert view.busy and view.active_pack is None
    view.future.set_result(LanguageCatalog(('ukr',), integrity='verified_model_pack'))
    view.pending.pop(0)()
    assert view.selected_codes == ['ukr']
    assert view.current_config().model_pack_sha256 == 'a' * 64

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
    def SetItems(self, items):
        self.items, self.selection, self.checked = list(items), -1, set()
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
             'EVT_BUTTON', 'EVT_CLOSE', 'OK', 'ICON_ERROR', 'CANCEL', 'ICON_INFORMATION',
             'EVT_WINDOW_DESTROY', 'LEFT', 'RIGHT', 'YES_NO', 'NO_DEFAULT', 'ICON_QUESTION', 'ID_YES')
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
    for name in ('TextCtrl', 'CheckListBox', 'CheckBox', 'Choice', 'Button', 'StaticText', 'BoxSizer'):
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


@pytest.fixture
def managed_presets(tmp_path, monkeypatch):
    """Real local store and checked synthetic model bytes; no model download."""
    from hashlib import sha1
    from carr_library.extract import model_catalog as catalogue
    from carr_library.extract import ocr_model_store as store_module
    from carr_library.extract.ocr_languages import store_preset, load_presets
    library = tmp_path / 'library'
    library.mkdir()
    source = tmp_path / 'chosen-models'
    source.mkdir()
    rows = []
    for identifier in ('eng', 'ukr', 'script/Latin', 'osd'):
        raw = ('Explicit synthetic model ' + identifier + '\n').encode() * 13
        relative = identifier + '.traineddata'
        path = source / relative
        path.parent.mkdir(exist_ok=True, parents=True)
        path.write_bytes(raw)
        kind = 'script' if identifier.startswith('script/') else 'auxiliary' if identifier == 'osd' else 'language'
        blob = sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()
        rows.append(catalogue.Model(identifier, identifier, kind, 'fast',
            catalogue.REVISIONS['fast'], len(raw), blob, relative))
    monkeypatch.setattr(store_module, 'models', lambda profile: tuple(replace(row, profile=profile,
        revision=catalogue.REVISIONS[profile]) for row in rows))
    monkeypatch.setattr(store_module, 'catalogue_sha256', lambda: 'b' * 64)
    monkeypatch.setattr(store_module, 'license_bytes', lambda profile: b'Synthetic license for model fixture only.\n')
    store = store_module.OcrModelStore(tmp_path / 'managed-models')
    store.install('best', source, ('eng', 'ukr', 'script/Latin', 'osd'))
    store.install('fast', source, ('eng', 'ukr'))
    monkeypatch.setenv('CARR_OCR_MODELS_DIR', str(store.root))
    config = Config(languages='ukr+script/Latin+eng', model_profile='best', orientation=True)
    store_preset(library, 'Best ordered', config)
    return SimpleNamespace(root=library, store=store, config=config, row=load_presets(library)[0])


def test_managed_preset_checks_real_model_bytes_without_installing_or_running_system_ocr(managed_presets, monkeypatch):
    fixture = managed_presets
    def contents():
        return {path: path.read_bytes() for path in fixture.store.root.rglob('*') if path.is_file()}
    before = contents()
    monkeypatch.setattr(selection, 'discover_installed_models', Mock(side_effect=AssertionError('No system inventory for Best')))
    monkeypatch.setattr(type(fixture.store), 'install', Mock(side_effect=AssertionError('Applying never installs models')))
    initial = Config(dpi=144, timeout=77, max_pixels=123456)
    result, available = selection.verify_saved_preset(fixture.root, fixture.row, initial=initial)
    assert result == replace(initial, languages=fixture.config.languages, model_profile='best', orientation=True)
    assert {'eng', 'ukr', 'script/Latin', 'osd'} == set(available)
    assert contents() == before


@pytest.mark.parametrize('failure', ['changed', 'missing', 'missing-license'])
def test_saved_preset_never_trusts_only_managed_manifest_inventory(managed_presets, failure):
    fixture = managed_presets
    directory = fixture.store._profile('best')
    target = directory / ('LICENSE' if failure == 'missing-license' else 'tessdata/ukr.traineddata')
    if failure == 'changed':
        raw = target.read_bytes()
        target.write_bytes(b'x' * len(raw))
    else:
        target.unlink()
    # The cheap manifest inventory still lists the model, but application fails.
    assert 'ukr' in fixture.store.available('best')
    with pytest.raises((ValueError, OSError)):
        selection.verify_saved_preset(fixture.root, fixture.row)


def test_orientation_requires_real_osd_in_the_saved_profile(managed_presets):
    from carr_library.extract.ocr_languages import store_preset, load_presets
    fixture = managed_presets
    store_preset(fixture.root, 'Fast with OSD', Config(languages='eng', model_profile='fast', orientation=True))
    selected = next(row for row in load_presets(fixture.root) if row['name'] == 'Fast with OSD')
    with pytest.raises(ValueError, match='selected OCR languages and orientation'):
        selection.verify_saved_preset(fixture.root, selected)


def test_cancel_after_real_model_check_does_not_publish_a_preset_result(managed_presets, monkeypatch):
    fixture = managed_presets
    cancelled = Event()
    original = type(fixture.store).select
    def check_then_cancel(*args, **kwargs):
        result = original(*args, **kwargs)
        cancelled.set()
        return result
    monkeypatch.setattr(type(fixture.store), 'select', check_then_cancel)
    with pytest.raises(InterruptedError):
        selection.verify_saved_preset(fixture.root, fixture.row, cancelled=cancelled.is_set)


def test_preset_change_during_model_check_rejects_the_old_selection(managed_presets, monkeypatch):
    from carr_library.extract.ocr_languages import store_preset
    fixture = managed_presets
    original = type(fixture.store).select
    def check_then_change(*args, **kwargs):
        result = original(*args, **kwargs)
        store_preset(fixture.root, fixture.row['name'], Config(model_profile='fast'), replace=True)
        return result
    monkeypatch.setattr(type(fixture.store), 'select', check_then_change)
    with pytest.raises(ValueError, match='changed or was deleted'):
        selection.verify_saved_preset(fixture.root, fixture.row)


def test_legacy_preset_uses_fresh_actual_inventory_process_and_retains_file_bytes(monkeypatch, tmp_path):
    import json
    from carr_library.extract.ocr_languages import FILENAME, load_presets
    row = dict(name='Legacy priority', languages='ukr+eng', orientation=True, model_policy='default')
    raw = json.dumps(dict(version=1, presets=[row]), indent=4).encode() + b'\n'
    (tmp_path / FILENAME).write_bytes(raw)
    report = listing(('eng', 'ukr', 'osd'), str(tmp_path))
    runtime, discover = runtime_program(monkeypatch, tmp_path, f'import sys; sys.stdout.buffer.write({report!r})')
    result, available = selection.verify_saved_preset(tmp_path, load_presets(tmp_path)[0],
        initial=Config(model_profile='best', dpi=144))
    assert result == Config(languages='ukr+eng', orientation=True, model_profile='installed', dpi=144)
    assert available == ('eng', 'ukr', 'osd') and runtime.assert_unchanged.called
    assert (tmp_path / FILENAME).read_bytes() == raw


def test_verified_saved_profile_updates_authoritative_controls_and_preserves_order(wx, managed_presets, monkeypatch):
    fixture = managed_presets
    parent = SimpleNamespace(library=SimpleNamespace(root=fixture.root))
    invocations = []
    def verify(parent, *, operation, status):
        invocations.append(status)
        return operation(cancelled=lambda: False)
    monkeypatch.setattr(selection, '_discover_with_dialog', verify)
    def choose(dialog):
        assert dialog.presets.name and all(button.name for button in dialog.preset_buttons)
        dialog.load_preset()
        assert dialog.profile == 'best'
        assert [row[0] for row in dialog.rows[:3]] == ['ukr', 'script/Latin', 'eng']
        assert dialog.orientation.GetValue() and dialog.entries.focused
        assert 'Profile: best' in dialog.status.value
        dialog.accept()
    wx.on_show = choose
    initial = Config(languages='eng', dpi=150, timeout=83, max_pixels=123456)
    result = selection._choose_languages(parent, 'installed', ('eng',), initial)
    assert result == replace(initial, languages=fixture.config.languages, model_profile='best', orientation=True)
    assert len(invocations) == 1


@pytest.mark.parametrize('outcome', ['cancelled', 'corrupt', 'scope-change'])
def test_failed_or_cancelled_load_keeps_primary_selection_unchanged(wx, managed_presets, monkeypatch, tmp_path, outcome):
    fixture = managed_presets
    parent = SimpleNamespace(library=SimpleNamespace(root=fixture.root))
    if outcome == 'corrupt':
        (fixture.store._profile('best') / 'tessdata/ukr.traineddata').write_bytes(b'changed')
    def verify(parent_dialog, *, operation, status):
        if outcome == 'cancelled':
            return None
        value = operation(cancelled=lambda: False)
        if outcome == 'scope-change':
            parent.library = SimpleNamespace(root=tmp_path)
        return value
    monkeypatch.setattr(selection, '_discover_with_dialog', verify)
    def choose(dialog):
        before = dialog.current_config()
        old_rows = dialog.rows
        dialog.load_preset()
        assert dialog.profile == 'installed' and dialog.rows == old_rows
        assert dialog.config_base == before
        assert not dialog.orientation.GetValue()
        dialog.EndModal(wx.ID_CANCEL)
    wx.on_show = choose
    assert selection._choose_languages(parent, 'installed', ('eng',), Config()) is None


def preset_prompt(wx, name, *, on_show=lambda: None):
    class Prompt(Control):
        def ShowModal(self):
            on_show()
            return wx.ID_OK
        def GetValue(self):
            return name
    wx.TextEntryDialog = Prompt


def test_native_preset_save_replace_and_delete_require_explicit_confirmation(wx, tmp_path):
    from carr_library.extract.ocr_languages import FILENAME, load_presets
    parent = SimpleNamespace(library=SimpleNamespace(root=tmp_path))
    preset_prompt(wx, 'Saved priority')
    def choose(dialog):
        dialog.orientation.SetValue(True)
        dialog.save_preset()
        path = tmp_path / FILENAME
        original = path.read_bytes()
        row, = load_presets(tmp_path)
        assert row['model_profile'] == 'fast' and row['languages'] == 'ukr+eng' and row['orientation']
        dialog.orientation.SetValue(False)
        dialog.save_preset()  # Default confirmation is Cancel.
        assert path.read_bytes() == original
        wx.missing_engine_result = wx.ID_YES
        dialog.save_preset()
        assert not load_presets(tmp_path)[0]['orientation']
        retained = dialog.current_config()
        wx.missing_engine_result = wx.ID_CANCEL
        dialog.remove_preset()
        assert len(load_presets(tmp_path)) == 1
        wx.missing_engine_result = wx.ID_YES
        dialog.remove_preset()
        assert load_presets(tmp_path) == () and dialog.current_config() == retained
        dialog.accept()
    wx.on_show = choose
    result = selection._choose_languages(parent, 'fast', ('eng', 'ukr', 'osd'), Config(languages='ukr+eng'))
    assert result.model_profile == 'fast' and result.languages == 'ukr+eng'


def test_scope_change_inside_name_prompt_cannot_save_to_original_or_new_folder(wx, tmp_path):
    from carr_library.extract.ocr_languages import FILENAME
    other = tmp_path / 'other'
    other.mkdir()
    parent = SimpleNamespace(library=SimpleNamespace(root=tmp_path))
    preset_prompt(wx, 'Must not save', on_show=lambda: setattr(parent, 'library', SimpleNamespace(root=other)))
    def choose(dialog):
        dialog.save_preset()
        assert 'folder changed' in dialog.status.value
        assert not (tmp_path / FILENAME).exists() and not (other / FILENAME).exists()
        dialog.EndModal(wx.ID_CANCEL)
    wx.on_show = choose
    assert selection._choose_languages(parent, 'installed', ('eng',), Config()) is None


def test_invalid_preset_file_preserves_manual_selection_and_exact_bytes(wx, tmp_path):
    from carr_library.extract.ocr_languages import FILENAME
    raw = b'{"version":99,"presets":[]}'
    path = tmp_path / FILENAME
    path.write_bytes(raw)
    def choose(dialog):
        assert 'Manual model selection remains available' in dialog.status.value
        dialog.accept()
    wx.on_show = choose
    result = selection._choose_languages(None, 'installed', ('eng',), Config(), preset_root=tmp_path)
    assert result == Config() and path.read_bytes() == raw


def test_inventory_forced_destroy_with_pending_completion_ignores_dead_window(wx, monkeypatch):
    pending = []
    class Executor:
        def __init__(self, **kwargs): pass
        def submit(self, operation, **kwargs):
            future = Future()
            pending.append(future)
            return future
        def shutdown(self, **kwargs): pass
    monkeypatch.setattr(selection, 'ThreadPoolExecutor', Executor)
    def destroy(dialog):
        dialog.on_destroy(SimpleNamespace(GetEventObject=lambda: dialog, Skip=lambda: None))
        pending[0].set_result(('eng',))
        while wx.callbacks:
            wx.callbacks.pop(0)()
        assert dialog.result is None and dialog.modal_result is None
        assert dialog.cancelled.is_set() and not dialog.alive
    wx.on_show = destroy
    assert selection._discover_with_dialog(None) is None
    # The external destroy event already owns destruction; no second Destroy call.
    assert not wx.dialogs[-1].destroyed


@pytest.mark.parametrize('legacy', [True, False])
def test_full_backup_restores_preset_bytes_without_changing_profile_or_legacy_shape(tmp_path, legacy):
    import json
    from carr_library.application import LibraryApp
    from carr_library.extract.ocr_languages import FILENAME, load_presets, store_preset
    from carr_library.library_backup import create_backup, restore_backup
    library = LibraryApp(tmp_path / 'library')
    library.create_workspace('Preset backup')
    if legacy:
        row = dict(name='Old ordered', languages='ukr+eng', orientation=True, model_policy='default')
        (library.root / FILENAME).write_bytes(json.dumps(dict(version=1, presets=[row]), indent=4).encode() + b'\n\n')
    else:
        store_preset(library.root, 'Best ordered', Config(languages='ukr+eng', model_profile='best', orientation=True))
    raw = (library.root / FILENAME).read_bytes()
    before = load_presets(library.root)
    digest = create_backup(library, tmp_path / 'backup')
    destination = restore_backup(tmp_path / 'backup', tmp_path / 'restored', digest)
    assert (destination / FILENAME).read_bytes() == raw == (library.root / FILENAME).read_bytes()
    assert load_presets(destination) == before

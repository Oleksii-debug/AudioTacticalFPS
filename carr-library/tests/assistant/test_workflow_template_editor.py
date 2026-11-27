"""Actual passive editor functions and the one semantic macro registration seam."""
from dataclasses import replace
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant.templates import WorkflowTemplate, TemplateStore, review_request
from carr_library.durable_state import encode
from carr_library.studio.project import parse_script
from carr_library.ui import workflow_templates as ui
from carr_library.ui.command_registry import CommandRegistry, ShortcutError

_fixture_spec = importlib.util.spec_from_file_location('carr_recipe_widget_fixture',
    Path(__file__).with_name('test_planning_handlers.py'))
_fixture = importlib.util.module_from_spec(_fixture_spec); _fixture_spec.loader.exec_module(_fixture)


@pytest.fixture
def wx():
    class Control(_fixture.Widget):
        def SetEditable(self, value): self.editable = value
        def SetEscapeId(self, value): self.escape = value
    state = SimpleNamespace(action=lambda dialog: None, dialogs=[], paths=[], pick=lambda: None)
    class Dialog(Control):
        def ShowModal(self):
            state.dialogs.append(self)
            self.modal_result = state.ID_CANCEL
            state.action(self)
            return self.modal_result
    class FileDialog(Control):
        def ShowModal(self):
            state.pick()
            return state.ID_OK
        def GetPath(self): return str(state.paths[-1])
    for index, name in enumerate(('DEFAULT_DIALOG_STYLE', 'RESIZE_BORDER', 'VERTICAL', 'HORIZONTAL',
                                 'ALL', 'EXPAND', 'TE_MULTILINE', 'ID_OK', 'ID_CANCEL',
                                 'EVT_BUTTON', 'FD_OPEN', 'FD_FILE_MUST_EXIST'), 1):
        setattr(state, name, 1 << index)
    state.Dialog, state.FileDialog = Dialog, FileDialog
    state.TextCtrl = state.StaticText = state.Button = Control
    state.BoxSizer = _fixture.Sizer
    return state


def record():
    return WorkflowTemplate.define('Parameterized library', [
        {'tool': 'library.create', 'arguments': {'name': {'$parameter': 'name'}}}],
        {'name': {'kind': 'text', 'label': 'Library name', 'example': 'Explicit example'}})


def test_parameter_values_are_not_implicitly_filled_or_accepted(wx):
    def use(dialog):
        assert dialog.workflow_fields['values'].GetValue() == '{}'
        dialog.workflow_handlers['use']()
        assert dialog.modal_result == wx.ID_CANCEL
        assert 'Supply every' in dialog.workflow_fields['status'].GetValue()
        dialog.workflow_handlers['examples']()
        assert json.loads(dialog.workflow_fields['values'].GetValue()) == {'name': 'Explicit example'}
        dialog.workflow_handlers['use']()
    wx.action = use
    request = ui.choose_values(None, record(), wx_module=wx)
    assert request is not None
    assert request['values'] == {'name': 'Explicit example'} and request['items'] is None
    assert 'scope' not in request and wx.dialogs[-1].destroyed


def test_explicit_batch_overrides_preserve_order_and_do_not_store_example_authority(wx):
    def use(dialog):
        dialog.workflow_fields['values'].ChangeValue('{"name":"base"}')
        dialog.workflow_fields['items'].ChangeValue('[{"name":"First"},{"name":"Second"}]')
        dialog.workflow_handlers['use']()
    wx.action = use
    request = ui.choose_values(None, record(), wx_module=wx)
    assert request['items'] == [{'name': 'First'}, {'name': 'Second'}]
    assert request['values'] == {'name': 'base'}


def test_binding_dialog_cancel_and_guard_revocation_return_no_request(wx):
    assert ui.choose_values(None, record(), wx_module=wx) is None
    allowed = [True]
    def guard():
        if not allowed[0]: raise ValueError('Library changed')
    def closed(dialog):
        allowed[0] = False
        dialog.workflow_handlers['examples']()
        assert 'Library changed' in dialog.workflow_fields['status'].GetValue()
    wx.action = closed
    with pytest.raises(ValueError, match='Library changed'):
        ui.choose_values(None, record(), guard=guard, wx_module=wx)
    assert wx.dialogs[-1].destroyed


def test_project_file_picker_copies_typed_settings_and_removes_source_refs(wx, tmp_path):
    chosen = parse_script('Original selected project words.', project_id='old-project')
    raw = chosen.to_dict(); raw['scenes'][0]['lines'][0]['source_ref'] = 'old-page-source'
    path = tmp_path / 'selected-project.json'; path.write_bytes(encode(raw))
    wx.paths.append(path)
    initial = WorkflowTemplate.define('New project recipe', [{'tool': 'library.list', 'arguments': {}}])
    def select(dialog):
        dialog.workflow_handlers['project']()
        steps = json.loads(dialog.workflow_fields['steps'].GetValue())
        assert steps[0]['arguments']['script']['id'] == 'template'
        assert steps[0]['arguments']['script']['scenes'][0]['lines'][0]['source_ref'] == ''
        dialog.workflow_handlers['use']()
    wx.action = select
    result = ui.edit_template(None, initial, wx_module=wx)
    assert result is not None and result.id == initial.id
    assert json.loads(path.read_bytes()) == raw
    assert result.parameters == {} and wx.dialogs[-1].destroyed


def test_library_change_during_project_picker_cannot_read_the_selected_file(wx, monkeypatch):
    initial = WorkflowTemplate.define('Guarded recipe', [{'tool': 'library.list', 'arguments': {}}])
    wx.paths.append('/must-not-open/project.json')
    allowed = [True]
    wx.pick = lambda: allowed.__setitem__(0, False)
    def guard():
        if not allowed[0]: raise ValueError('Library changed')
    monkeypatch.setattr(ui, 'read_json_snapshot', lambda *a, **k: pytest.fail('Revoked picker read a file.'))
    def select(dialog):
        dialog.workflow_handlers['project']()
        assert 'Library changed' in dialog.workflow_fields['status'].GetValue()
    wx.action = select
    with pytest.raises(ValueError, match='Library changed'):
        ui.edit_template(None, initial, guard=guard, wx_module=wx)
    assert wx.dialogs[-1].destroyed


def test_unknown_tool_or_old_permissions_cannot_be_saved_through_typed_editor(wx):
    initial = record()
    def invalid(dialog):
        dialog.workflow_fields['steps'].ChangeValue('[{"tool":"shell","arguments":{"command":"forbidden"}}]')
        dialog.workflow_handlers['use']()
        assert dialog.modal_result == wx.ID_CANCEL
        assert 'allowed' in dialog.workflow_fields['status'].GetValue()
    wx.action = invalid
    assert ui.edit_template(None, initial, wx_module=wx) is None


class Item:
    def __init__(self, label): self.label, self.enabled = label, True
    def Enable(self, value): self.enabled = value


class Menu:
    def __init__(self): self.items, self.submenus = [], []
    def Append(self, identity, label):
        item = Item(label); self.items.append(item); return item
    def AppendSubMenu(self, menu, label): self.submenus.append((menu, label))
    def Delete(self, item): self.items.remove(item)


def macro_environment(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    frame = SimpleNamespace(library=app, _closing=False, callbacks={}, reports=[])
    frame.Bind = lambda event, handler, item: frame.callbacks.__setitem__(id(item), handler)
    frame.report = frame.reports.append
    wx = SimpleNamespace(Menu=Menu, ID_ANY=-1, EVT_MENU=object(), CallAfter=lambda cb: cb())
    return app, frame, wx


def test_each_template_uses_one_existing_semantic_registry_and_only_opens_current_review(tmp_path, monkeypatch):
    from carr_library.ui import assistant_dialog
    app, frame, wx = macro_environment(tmp_path)
    store = TemplateStore(app.root); original = record()
    token = store.save(original, expected_token=None)
    menu = Menu(); opened = []
    monkeypatch.setattr(assistant_dialog, 'show_assistant', lambda owner, **kwargs: opened.append((owner, kwargs)))
    ui.install_template_macros(frame, menu, wx_module=wx)
    assert len(menu.submenus) == 1
    specs = frame._pending_keyboard_commands
    assert len(specs) == 1 and specs[0][0].command_id == original.command_id
    assert specs[0][0].default_shortcut == ''
    registry = CommandRegistry(spec for spec, item in specs)
    assert registry.shortcut(original.command_id, {original.command_id: 'Ctrl+Alt+F9'}) == 'Ctrl+Alt+F9'
    item = specs[0][1]
    assert opened == []
    frame.callbacks[id(item)](None)
    assert opened == [(frame, {'template_id': original.id})]
    assert not (app.root / 'state/workflows').exists()
    ui.refresh_template_macros(frame, wx_module=wx)
    assert len(frame._pending_keyboard_commands) == 1
    store.delete(original.id, expected_token=token)
    ui.refresh_template_macros(frame, wx_module=wx)
    assert not item.enabled
    frame.callbacks[id(item)](None)
    assert len(opened) == 1 and 'no longer available' in frame.reports[-1]


def test_macro_callback_cannot_retarget_after_library_change_or_close(tmp_path, monkeypatch):
    from carr_library.ui import assistant_dialog
    app, frame, wx = macro_environment(tmp_path)
    record_value = record(); TemplateStore(app.root).save(record_value, expected_token=None)
    monkeypatch.setattr(assistant_dialog, 'show_assistant', lambda *a, **k: pytest.fail('Stale macro opened.'))
    ui.install_template_macros(frame, Menu(), wx_module=wx)
    item = frame._pending_keyboard_commands[0][1]
    frame.library = LibraryApp(tmp_path / 'different')
    frame.callbacks[id(item)](None)
    ui.refresh_template_macros(frame, wx_module=wx)
    assert not item.enabled and 'different library' in frame.reports[-1]
    frame.library, frame._closing = app, True
    frame.callbacks[id(item)](None)


def test_bad_catalogue_does_not_register_a_partial_native_macro(tmp_path):
    app, frame, wx = macro_environment(tmp_path)
    store = TemplateStore(app.root); store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_bytes(b'{"schema":"bad"}')
    menu = Menu()
    ui.install_template_macros(frame, menu, wx_module=wx)
    assert not getattr(frame, '_pending_keyboard_commands', [])
    assert not menu.submenus
    assert 'needs review' in frame.reports[-1]

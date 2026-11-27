"""Exercise real template closures and the existing native command registration."""
from types import SimpleNamespace
import importlib.util
from pathlib import Path
import json
import sys

import pytest

from carr_library.assistant import Plan, Scope, Step
from carr_library.assistant.templates import TemplateStore, WorkflowTemplate
from carr_library.ui import assistant_dialog
# Reuse the existing deterministic native-widget fixture even under pytest's
# installed-origin importlib mode; the source path is this test's own sibling.
_fixture_spec = importlib.util.spec_from_file_location(
    'carr_template_handler_fixture', Path(__file__).with_name('test_planning_handlers.py'))
_fixture = importlib.util.module_from_spec(_fixture_spec)
_fixture_spec.loader.exec_module(_fixture)
native_handlers, Widget = _fixture.native_handlers, _fixture.Widget


def choose_template(environment, index=0, *, before_return=lambda: None):
    class Choice(Widget):
        def ShowModal(self):
            before_return()
            self.selection = index
            return environment.wx.ID_OK
    environment.wx.SingleChoiceDialog = Choice


def confirmation(environment, answer=True, *, callback=lambda: None):
    for name, value in [('YES_NO', 1), ('NO_DEFAULT', 2), ('ICON_QUESTION', 4), ('YES', 8), ('NO', 16)]:
        setattr(environment.wx, name, value)
    seen = []
    def message(body, title, flags, parent):
        assert flags & environment.wx.NO_DEFAULT
        seen.append((body, title))
        callback()
        return environment.wx.YES if answer else environment.wx.NO
    environment.wx.MessageBox = message
    return seen


def reviewed_template(e, name='Named workflow'):
    e.handlers['preview']()
    e.controls['template_name'].ChangeValue(name)
    e.handlers['save_template']()
    records, token = TemplateStore(e.app.root).list()
    assert len(records) == 1
    return records[0], token


def test_save_load_requires_fresh_preview_and_separate_new_submission(native_handlers):
    e = native_handlers
    record, token = reviewed_template(e)
    assert e.dialog.assistant_service.jobs() == () and not e.calls
    assert not e.executor.pending
    e.controls['input'].ChangeValue('/unreviewed/old-input')
    e.controls['output'].ChangeValue('/unreviewed/old-output')
    e.controls['full_backup'].SetValue(True)
    e.controls['planning_source'].SetSelection(1)
    choose_template(e)
    e.handlers['templates']()
    assert e.controls['input'].GetValue() == '' and e.controls['output'].GetValue() == ''
    assert e.controls['full_backup'].GetValue() is False
    assert e.controls['planning_source'].GetSelection() == 0
    assert 'scope' not in json.loads(e.controls['instruction'].GetValue())
    assert not e.controls['buttons']['run'].IsEnabled()
    e.handlers['run']()
    assert e.dialog.assistant_service.jobs() == () and not e.executor.pending
    e.handlers['preview']()
    assert e.controls['buttons']['run'].IsEnabled() and not e.calls
    e.handlers['run']()
    assert len(e.dialog.assistant_service.jobs()) == 1
    e.executor.complete(); e.drain()
    assert e.dialog.assistant_service.jobs()[0].status == 'complete'


def test_save_refuses_unreviewed_and_silently_edited_goal(native_handlers):
    e = native_handlers
    e.controls['template_name'].ChangeValue('Name')
    e.handlers['save_template']()
    assert not TemplateStore(e.app.root).path.exists()
    e.handlers['preview']()
    e.controls['instruction'].ChangeValue('Create library "Not reviewed"')
    e.handlers['save_template']()
    assert not TemplateStore(e.app.root).path.exists()
    assert e.app.workspaces() == () and not e.dialog.assistant_service.jobs()


def test_default_no_keeps_existing_template_and_delete_cancels(native_handlers):
    e = native_handlers; record, token = reviewed_template(e)
    before = TemplateStore(e.app.root).path.read_bytes()
    confirmations = confirmation(e, False)
    e.handlers['save_template']()
    e.handlers['delete_template']()
    assert len(confirmations) == 2
    assert TemplateStore(e.app.root).path.read_bytes() == before
    assert not e.dialog.assistant_service.jobs()


def test_catalogue_changed_during_native_choice_is_not_loaded(native_handlers):
    from dataclasses import replace
    e = native_handlers; record, token = reviewed_template(e)
    before = e.controls['instruction'].GetValue()
    store = TemplateStore(e.app.root)
    choose_template(e, before_return=lambda: store.save(
        replace(record, summary='Concurrent update'), expected_token=token))
    e.handlers['templates']()
    assert e.controls['instruction'].GetValue() == before
    assert 'changed' in e.controls['review'].GetValue()
    assert not e.dialog.assistant_service.jobs()


def test_catalogue_or_scope_changed_during_replace_confirmation_cannot_overwrite(native_handlers):
    from dataclasses import replace
    e = native_handlers; record, token = reviewed_template(e)
    store = TemplateStore(e.app.root)
    confirmation(e, callback=lambda: store.save(
        replace(record, summary='Concurrent authoritative version'), expected_token=token))
    e.handlers['save_template']()
    assert store.get(record.id)[0].summary == 'Concurrent authoritative version'
    assert 'changed' in e.controls['review'].GetValue()
    assert not e.dialog.assistant_service.jobs()


def test_close_or_root_change_during_choice_cannot_admit_template(native_handlers, tmp_path):
    from carr_library.application import LibraryApp
    e = native_handlers; reviewed_template(e)
    before = e.controls['instruction'].GetValue()
    choose_template(e, before_return=lambda: setattr(e.frame, 'library', LibraryApp(tmp_path / 'other')))
    e.handlers['templates']()
    assert e.controls['instruction'].GetValue() == before
    assert 'location changed' in e.controls['review'].GetValue()
    e.frame.library = e.app
    choose_template(e, before_return=e.handlers['close'])
    e.handlers['templates']()
    assert e.controls['instruction'].GetValue() == before
    assert not e.dialog.assistant_service.jobs()


def test_keyboard_macro_kind_is_typed_data_and_delete_preserves_existing_job(native_handlers):
    e = native_handlers
    e.controls['template_kind'].SetSelection(2)
    record, token = reviewed_template(e, 'Typed macro')
    assert record.kind == 'keyboard-macro'
    e.handlers['run'](); e.executor.complete(); e.drain()
    job, = e.dialog.assistant_service.jobs()
    confirmation(e)
    e.handlers['delete_template']()
    assert TemplateStore(e.app.root).list()[0] == ()
    assert e.dialog.assistant_service.get(job.id).status == 'complete'


def test_new_semantic_keyboard_command_opens_existing_template_review_only(monkeypatch):
    from carr_library.ui import keyboard_settings
    registered, bindings, opened = [], {}, []
    class Item:
        def __init__(self, identity): self.identity = identity
    class Menu:
        def __init__(self): self.items = []
        def Append(self, any_id, title):
            item = Item(len(self.items) + 1); self.items.append(item); return item
    wx = SimpleNamespace(Menu=Menu, ID_ANY=-1, EVT_MENU=object())
    monkeypatch.setitem(sys.modules, 'wx', wx)
    monkeypatch.setattr(keyboard_settings, 'register_menu_command',
        lambda frame, spec, item: registered.append((spec, item)))
    monkeypatch.setattr(assistant_dialog, 'show_assistant',
        lambda frame, **kwargs: opened.append(kwargs))
    frame = SimpleNamespace(
        Bind=lambda event, callback, item: bindings.__setitem__(item.identity, callback),
        menu_bar=SimpleNamespace(Append=lambda *args: None))
    assistant_dialog.install_assistant_actions(frame)
    matches = [(spec, item) for spec, item in registered if spec.command_id == 'assistant.templates']
    spec, item = matches[0]
    assert len(matches) == 1 and spec.default_shortcut == ''
    assert not opened
    bindings[item.identity](None)
    assert opened == [{'templates': True}]

def test_new_name_saves_a_separate_template_without_replacing_selected(native_handlers):
    e = native_handlers; original, first_token = reviewed_template(e)
    e.controls['template_name'].ChangeValue('Second reusable copy')
    e.handlers['save_template']()
    records, token = TemplateStore(e.app.root).list()
    assert len(records) == 2 and token != first_token
    assert records[0] == original and records[1].id != original.id
    assert not e.dialog.assistant_service.jobs()


def test_template_actions_do_not_run_while_proposal_worker_is_active(native_handlers):
    e = native_handlers; reviewed_template(e)
    before = TemplateStore(e.app.root).path.read_bytes()
    e.controls['planning_source'].SetSelection(1)
    e.controls['instruction'].ChangeValue('Create one named library.')
    e.handlers['preview']()
    assert len(e.executor.pending) == 1
    for name in ('templates', 'save_template', 'delete_template'):
        e.handlers[name]()
    assert TemplateStore(e.app.root).path.read_bytes() == before
    assert e.dialog.assistant_service.jobs() == ()
    e.handlers['cancel'](); e.executor.complete(); e.drain()


def test_parameterized_macro_load_clears_scope_and_requires_fresh_preview(native_handlers, monkeypatch):
    from carr_library.assistant.templates import review_request
    from carr_library.ui import workflow_templates
    e = native_handlers
    record = WorkflowTemplate.define('Typed new library', [
        {'tool': 'library.create', 'arguments': {'name': {'$parameter': 'name'}}}],
        {'name': {'kind': 'text', 'label': 'Library name', 'example': 'Example only'}})
    TemplateStore(e.app.root).save(record, expected_token=None)
    monkeypatch.setattr(workflow_templates, 'choose_values',
        lambda *args, **kwargs: review_request(record, {'name': 'New explicit use'}))
    e.controls['input'].ChangeValue('/old/permission')
    e.controls['full_backup'].SetValue(True)
    e.handlers['templates'](identity=record.id)
    assert not e.controls['buttons']['run'].IsEnabled()
    assert not e.dialog.assistant_service.jobs() and not e.app.workspaces()
    assert e.controls['input'].GetValue() == '' and e.controls['full_backup'].GetValue() is False
    e.handlers['preview']()
    assert e.controls['buttons']['run'].IsEnabled()
    assert not e.dialog.assistant_service.jobs() and not e.calls


def test_catalogue_changed_during_parameter_choice_cannot_load_old_recipe(native_handlers, monkeypatch):
    from dataclasses import replace
    from carr_library.assistant.templates import review_request
    from carr_library.ui import workflow_templates
    e = native_handlers
    record = WorkflowTemplate.define('Typed recipe', [
        {'tool': 'library.create', 'arguments': {'name': {'$parameter': 'name'}}}],
        {'name': {'kind': 'text', 'label': 'Name', 'example': 'Example'}})
    store = TemplateStore(e.app.root); token = store.save(record, expected_token=None)
    old_goal = e.controls['instruction'].GetValue()
    def changed(*args, **kwargs):
        store.save(replace(record, summary='New authoritative settings'), expected_token=token)
        return review_request(record, {'name': 'Old review'})
    monkeypatch.setattr(workflow_templates, 'choose_values', changed)
    e.handlers['templates'](identity=record.id)
    assert e.controls['instruction'].GetValue() == old_goal
    assert 'changed' in e.controls['review'].GetValue()
    assert not e.dialog.assistant_service.jobs()


def test_typed_editor_replacement_has_default_no_and_catalogue_cas(native_handlers, monkeypatch):
    from dataclasses import replace
    from carr_library.ui import workflow_templates
    e = native_handlers; original, token = reviewed_template(e)
    store = TemplateStore(e.app.root); before = store.path.read_bytes()
    monkeypatch.setattr(workflow_templates, 'edit_template',
        lambda *a, **k: replace(original, summary='Edited typed definition'))
    confirmation(e, False)
    e.handlers['edit_template']()
    assert store.path.read_bytes() == before
    confirmation(e, callback=lambda: store.save(replace(original, summary='Concurrent copy'), expected_token=token))
    e.handlers['edit_template']()
    assert store.get(original.id)[0].summary == 'Concurrent copy'
    assert 'changed' in e.controls['review'].GetValue()
    assert not e.dialog.assistant_service.jobs()

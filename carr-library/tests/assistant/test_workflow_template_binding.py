"""Existing assistant dialog authority is cleared before any loaded template runs."""
import json
from types import SimpleNamespace

import pytest

from carr_library.assistant import AssistantService
from carr_library.ui import assistant_templates
from tests.assistant.test_planning_handlers import native_handlers


DRAFT = json.dumps({'schema': 'carr-assistant-plan/1', 'summary': 'An inert named draft',
                   'steps': [{'tool': 'library.list', 'arguments': {}}]})


def _picker(monkeypatch, action, *, draft=DRAFT):
    captured = []
    def create(parent, service, **kwargs):
        dialog = SimpleNamespace(template_result=draft, destroyed=False, closed=False)
        def close():
            dialog.closed = True
        dialog.template_handlers = {'close': close}
        dialog.ShowModal = lambda: action(service, kwargs)
        dialog.Destroy = lambda: setattr(dialog, 'destroyed', True)
        captured.append((service, kwargs, dialog))
        return dialog
    monkeypatch.setattr(assistant_templates, 'create_templates_dialog', create)
    return captured


def test_loaded_template_clears_prior_scope_and_requires_fresh_preview_then_run(
        native_handlers, monkeypatch, tmp_path):
    e = native_handlers
    workspace = e.app.create_workspace('Prior selected workspace')
    inputs, outputs = tmp_path / 'inputs', tmp_path / 'outputs'
    inputs.mkdir(); outputs.mkdir()
    captured = _picker(monkeypatch, lambda service, options: None)
    def scenario(e):
        e.controls['scope'].SetSelection(1)
        e.controls['input'].ChangeValue(str(inputs))
        e.controls['output'].ChangeValue(str(outputs))
        e.controls['full_backup'].SetValue(True)
        e.controls['budget'].ChangeValue('64')
        e.controls['file_budget'].ChangeValue('25')
        e.controls['deadline'].ChangeValue('45')
        e.press('preview'); e.finish()
        assert e.controls['run'].enabled and not e.calls
        e.press('templates')
        service, options, child = captured[0]
        assert options['current_plan'].scope.workspace_id == workspace
        assert options['current_plan'].scope.allow_full_backup
        assert options['executor'] is e.executor and options['is_current']() is False
        assert child.closed and child.destroyed
        assert e.controls['instruction'].GetValue() == DRAFT
        assert e.controls['scope'].GetSelection() == 0
        assert e.controls['input'].GetValue() == e.controls['output'].GetValue() == ''
        assert e.controls['full_backup'].GetValue() is False
        assert e.controls['budget'].GetValue() == '1024'
        assert e.controls['file_budget'].GetValue() == '5000'
        assert e.controls['deadline'].GetValue() == '3600'
        assert not e.controls['run'].enabled
        e.press('run')
        assert not e.executor.pending and service.jobs() == ()
        e.press('preview'); e.finish()
        assert e.controls['run'].enabled and service.jobs() == ()
        e.press('run'); e.finish()
        rows = service.jobs()
        assert len(rows) == 1 and rows[0].status == 'complete'
        record = service.store.load(rows[0].id)
        assert record['plan']['scope']['workspace_id'] is None
        assert record['plan']['scope']['input_roots'] == []
        assert record['plan']['scope']['output_root'] is None
        assert record['plan']['scope']['allow_full_backup'] is False
        assert not e.calls
    e.show(scenario)


def test_closing_template_settings_without_load_keeps_the_reviewed_plan(
        native_handlers, monkeypatch):
    captured = _picker(monkeypatch, lambda *args: None, draft=None)
    def scenario(e):
        e.press('preview'); e.finish()
        before = e.controls['instruction'].GetValue()
        e.press('templates')
        assert e.controls['run'].enabled
        assert e.controls['instruction'].GetValue() == before
        assert captured[0][1]['current_plan'] is not None
        assert AssistantService(e.app).jobs() == ()
    native_handlers.show(scenario)


@pytest.mark.parametrize('change', ['input_without_event', 'revision', 'library', 'close'])
def test_changed_owner_refuses_a_late_loaded_draft(native_handlers, monkeypatch, change):
    e = native_handlers
    def change_owner(service, options):
        assert options['is_current']()
        if change == 'input_without_event':
            e.controls['instruction'].ChangeValue('A newer instruction')
        elif change == 'revision':
            e.invalidate()
        elif change == 'library':
            # Use an actual new application object at the same selected path.
            from carr_library.application import LibraryApp
            e.frame.library = LibraryApp(e.app.root)
        else:
            e.press('close')
    _picker(monkeypatch, change_owner)
    def scenario(e):
        e.press('preview'); e.finish()
        e.press('templates')
        assert e.controls['instruction'].GetValue() != DRAFT
        if change != 'close':
            assert not e.controls['run'].enabled
            e.press('run')
        assert not e.executor.pending and AssistantService(e.app).jobs() == ()
    e.show(scenario)


def test_template_handler_does_not_compete_with_an_inflight_preview(native_handlers, monkeypatch):
    captured = _picker(monkeypatch, lambda *args: None)
    def scenario(e):
        e.press('preview')
        assert not e.controls['templates'].enabled
        e.press('templates')
        assert not captured and len(e.executor.pending) == 1
        e.finish()
        assert e.controls['templates'].enabled
    native_handlers.show(scenario)

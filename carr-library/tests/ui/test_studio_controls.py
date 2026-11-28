"""Real Studio controller/draft boundaries with only native widgets substituted.

The separate installed Windows probe drives the actual named wx controls.
"""
from dataclasses import replace
from pathlib import Path
from threading import Event
from types import MethodType, SimpleNamespace
import sys

import pytest

from carr_library.audio.production import parse_script
from carr_library.audio.studio_controls import ScriptProductionEditor
from carr_library.ui import studio_controls
from carr_library.ui.audio_studio import AudioStudioDialog, StudioController


ROWS = ({'id': 'test:voice', 'name': 'Synthetic role voice',
         'languages': ('uk', 'en-US'), 'controls': ('rate', 'pitch', 'volume'), 'available': True},)


class Registry:
    def __init__(self):
        self.calls = 0
        self.hook = lambda: None

    def voices(self, *, cancelled=lambda: False):
        if cancelled():
            raise InterruptedError('Synthetic discovery cancelled')
        self.calls += 1
        self.hook()
        return ROWS


class Control:
    def __init__(self, owner, value='', index=0):
        self.owner, self.value, self.index = owner, value, index
        self.items, self.focused = [], 0
        self.enabled, self.shown, self.deleting = True, True, False

    def GetValue(self):
        return self.value

    def ChangeValue(self, value):
        self.value = value

    def GetSelection(self):
        return self.index

    def SetSelection(self, index):
        self.index = index

    def SetItems(self, values):
        self.items = list(values)

    def SetFocus(self):
        self.focused += 1

    def IsEnabled(self):
        return self.enabled

    def IsShown(self):
        return self.shown

    def IsBeingDeleted(self):
        return self.deleting


@pytest.fixture
def window(tmp_path):
    root = tmp_path / 'library'
    root.mkdir()
    registry = Registry()
    controller = StudioController(private_root=root, registry=registry)
    controller.new('Narrator: Точні слова.\r\n', input_kind='script')
    controller.configure(voice='test:voice', language='uk')
    owner = SimpleNamespace(SetTitle=lambda title: None)
    errors, messages, runs = [], [], []
    value = SimpleNamespace(
        controller=controller, closed=False, close_pending=False, busy=False,
        cancelled=Event(), dialog=owner, editing_dirty=False, _script_dirty=False,
        _loading=False, voice_rows=[], _voice_inventory_rows=list(ROWS),
        _error=errors.append, _message=lambda message, **kwargs: messages.append(message))
    for name in ('title', 'language', 'script', 'output', 'differences', 'plan_text', 'voice', 'input_kind'):
        setattr(value, name, Control(owner))
    value.wx = SimpleNamespace(Window=Control, NOT_FOUND=-1, GetTopLevelParent=lambda target: target.owner)
    for name in ('_capture', '_refresh_text', '_show_voice_choices',
                 '_production_form_state', '_production_form_focus', '_edit_production_controls'):
        setattr(value, name, MethodType(getattr(AudioStudioDialog, name), value))

    def run(label, action, done):
        runs.append(label)
        value.busy = True
        try:
            result = action()
        finally:
            value.busy = False
        done(result)

    value._run = run
    value._refresh_text()
    value.plan_text.ChangeValue(controller.preview())
    return value, errors, messages, runs


def accept_output(parent, editor, rows, **kwargs):
    assert kwargs['current']()
    editor.set_output('flac', False)
    return editor.script()


def test_cancelled_form_never_changes_saved_source_draft_preview_or_undo(window, monkeypatch):
    value, errors, messages, runs = window
    before = value.controller.to_dict()
    preview = value.plan_text.GetValue()

    def cancel(parent, editor, rows, **kwargs):
        editor.set_role('Narrator', replace(editor.role('Narrator'), pan=.5))
        editor.set_output('flac', False)
        return None

    monkeypatch.setattr(studio_controls, 'show_production_editor', cancel)
    AudioStudioDialog.edit_roles(value)
    assert not errors and not messages and len(runs) == 1
    assert value.controller.to_dict() == before and not value.controller.draft.edits
    assert value.plan_text.GetValue() == preview
    assert value.script.focused == 1


def test_accepted_form_is_one_undoable_edit_and_invalidates_previous_render_and_preview(window, monkeypatch):
    value, errors, messages, _ = window
    original = value.controller.script
    value.controller.result = {'old': 'completed-render'}
    target = Control(value.dialog)
    monkeypatch.setattr(studio_controls, 'show_production_editor', accept_output)
    AudioStudioDialog.edit_output(value, SimpleNamespace(GetEventObject=lambda: target))
    assert not errors and messages and target.focused == 1
    assert value.controller.result is None and value.controller.dirty
    assert len(value.controller.draft.edits) == 1
    assert value.controller.draft.source_text == original
    assert 'Preview the current plan' in value.plan_text.GetValue()
    assert parse_script(value.controller.script).output_format == 'flac'
    assert value.controller.draft.undo() and value.controller.script == original
    assert value.controller.draft.redo() and parse_script(value.controller.script).export_stems is False


def test_unchanged_acceptance_preserves_existing_render_and_draft_history(window, monkeypatch):
    value, errors, _, _ = window
    result = value.controller.result = {'existing': 'result'}
    preview = value.plan_text.GetValue()
    monkeypatch.setattr(studio_controls, 'show_production_editor',
                        lambda parent, editor, rows, **kwargs: editor.script())
    AudioStudioDialog.edit_roles(value)
    assert not errors and value.controller.result is result and not value.controller.draft.edits
    assert value.plan_text.GetValue() == preview


@pytest.mark.parametrize('change', [
    'script', 'draft', 'project', 'title', 'voice', 'language', 'mode', 'output',
    'ffmpeg', 'registry', 'private_root', 'trust', 'project_path',
    'native_script', 'native_title', 'busy', 'cancelled', 'closing', 'closed', 'controller',
])
def test_form_acceptance_rechecks_exact_current_owner_source_and_selection(
        window, monkeypatch, tmp_path, change):
    value, errors, messages, _ = window
    controller = value.controller

    def stale(parent, editor, rows, **kwargs):
        editor.set_output('flac', False)
        if change == 'script':
            controller.edit_script(controller.script + 'Narrator: New words.')
        elif change == 'draft':
            from carr_library.reader.narration_text import NarrationDraft
            controller.draft = NarrationDraft(controller.script, source_id='different-source')
        elif change == 'project':
            controller.project_id = '1' * 32
        elif change == 'title':
            controller.title = 'New title'
        elif change == 'voice':
            controller.voice = 'test:other'
        elif change == 'language':
            controller.language = 'en-US'
        elif change == 'mode':
            controller.input_kind = 'narration'
        elif change == 'output':
            controller.output_dir = tmp_path / 'other-output'
        elif change == 'ffmpeg':
            controller.ffmpeg = tmp_path / 'other-ffmpeg'
        elif change == 'registry':
            controller.registry = Registry()
        elif change == 'private_root':
            controller.private_root = tmp_path / 'another-library'
        elif change == 'trust':
            controller.trust_controller = object()
        elif change == 'project_path':
            controller.project_path = tmp_path / 'another.json'
        elif change == 'native_script':
            value.script.ChangeValue('New unsaved native text')
        elif change == 'native_title':
            value.title.ChangeValue('New unsaved title')
        elif change == 'busy':
            value.busy = True
        elif change == 'cancelled':
            value.cancelled.set()
        elif change == 'closing':
            value.close_pending = True
        elif change == 'closed':
            value.closed = True
        else:
            value.controller = StudioController(private_root=tmp_path / 'different')
        assert not kwargs['current']()
        return editor.script()

    monkeypatch.setattr(studio_controls, 'show_production_editor', stale)
    AudioStudioDialog.edit_output(value)
    assert len(errors) == 1 and 'changed' in str(errors[0])
    assert parse_script(controller.script).output_format == 'wav'
    assert not messages


def test_inflight_registry_replacement_cannot_pair_old_rows_with_new_authority(window, monkeypatch):
    value, errors, _, _ = window
    original = value.controller.registry
    replacement = Registry()
    original.hook = lambda: setattr(value.controller, 'registry', replacement)
    monkeypatch.setattr(studio_controls, 'show_production_editor',
                        lambda *args, **kwargs: pytest.fail('Stale voice rows opened a form.'))
    AudioStudioDialog.edit_roles(value)
    assert original.calls == 1 and replacement.calls == 0
    assert len(errors) == 1 and 'registry changed during discovery' in str(errors[0])
    assert not value.controller.draft.edits


def test_discovery_result_after_root_change_never_opens_a_native_form(window, monkeypatch):
    value, errors, _, _ = window

    def later(label, action, done):
        result = action()
        value.controller.project_id = '2' * 32
        done(result)

    value._run = later
    monkeypatch.setattr(studio_controls, 'show_production_editor',
                        lambda *args, **kwargs: pytest.fail('Stale discovery opened a form.'))
    AudioStudioDialog.edit_roles(value)
    assert len(errors) == 1 and 'changed while controls were loading' in str(errors[0])


def test_literal_source_is_never_parsed_or_rewritten_by_the_role_form(window, monkeypatch):
    value, errors, _, runs = window
    source = '[role Alice voice=untrusted]\nLiteral: exact text'
    value.controller.new(source, input_kind='narration')
    value._refresh_text()
    monkeypatch.setattr(studio_controls, 'show_production_editor',
                        lambda *args, **kwargs: pytest.fail('Literal source opened production controls.'))
    AudioStudioDialog.edit_roles(value)
    assert len(errors) == 1 and 'Structured production script' in str(errors[0])
    assert not runs and value.controller.script == source
    assert not value.controller.draft.edits


@pytest.mark.parametrize('route', ['roles', 'output'])
def test_busy_studio_never_discovers_or_opens_parallel_settings(window, monkeypatch, route):
    value, errors, _, runs = window
    value.busy = True
    monkeypatch.setattr(studio_controls, 'show_production_editor',
                        lambda *args, **kwargs: pytest.fail('Busy Studio opened a form.'))
    getattr(AudioStudioDialog, 'edit_' + route)(value)
    assert len(errors) == 1 and not runs and value.controller.registry.calls == 0


def test_forged_modal_return_cannot_bypass_the_typed_working_copy(window, monkeypatch):
    value, errors, _, _ = window
    monkeypatch.setattr(studio_controls, 'show_production_editor',
                        lambda parent, editor, rows, **kwargs: '[output format=flac]\nForeign spoken words.')
    AudioStudioDialog.edit_roles(value)
    assert len(errors) == 1 and not value.controller.draft.edits


def test_default_voice_inventory_retains_saved_unavailable_id_without_implicit_selection(window):
    value, _, _, _ = window
    value.controller.voice = 'missing:original'
    value._show_voice_choices(ROWS)
    index = value.voice.GetSelection()
    assert value.voice_rows[index]['id'] == 'missing:original'
    assert not value.voice_rows[index]['available']
    assert 'not in the current list' in value.voice.items[index]
    value._capture()
    assert value.controller.voice == 'missing:original'
    assert value.controller.language == 'uk'
    value._show_voice_choices([])
    assert len(value.voice_rows) == 1 and value.voice_rows[0]['id'] == 'missing:original'


def test_non_dataclass_voice_keeps_its_actual_advertised_controls(window):
    value, _, _, _ = window
    value.controller.registry.voices = lambda **kwargs: (
        SimpleNamespace(id='test:limited', name='Actual limited adapter row', languages=('uk',),
                        engine='test', available=True, controls=('volume',)),)
    assert value.controller.voices(cancelled=lambda: False)[0]['controls'] == ('volume',)


@pytest.mark.parametrize('target', ['foreign', 'deleted', 'hidden'])
def test_focus_return_never_targets_foreign_or_unusable_windows(window, target):
    value, _, _, _ = window
    control = Control(object() if target == 'foreign' else value.dialog)
    if target == 'deleted':
        control.deleting = True
    if target == 'hidden':
        control.shown = False
    value._production_form_focus(control)
    assert control.focused == 0 and value.script.focused == 1


def test_initial_native_construction_failure_still_destroys_exact_owned_dialog(monkeypatch):
    destroyed = []
    dialog = object()
    fake = SimpleNamespace(
        Dialog=lambda *args, **kwargs: SimpleNamespace(Destroy=lambda: destroyed.append(dialog)),
        DEFAULT_DIALOG_STYLE=1, RESIZE_BORDER=2)
    monkeypatch.setitem(sys.modules, 'wx', fake)
    def fail_after_allocation(*args, **kwargs):
        raise ValueError('The saved role failed native initial loading')
    monkeypatch.setattr(studio_controls, '_run_production_editor', fail_after_allocation)
    with pytest.raises(ValueError, match='initial loading'):
        studio_controls.show_production_editor(object(), ScriptProductionEditor('Exact words.'), ROWS)
    assert destroyed == [dialog]


def test_managed_save_restore_rebackup_keeps_source_and_controls_without_voice_activation(tmp_path):
    from carr_library.application import LibraryApp
    from carr_library.library_backup import create_backup, restore_backup, verify_backup
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Role form')
    controller = StudioController(private_root=app.root, registry=Registry())
    source = '[role Alice voice=test:voice language=uk]\r\nAlice: Точні слова.\r\n'
    controller.new(source, input_kind='script')
    editor = ScriptProductionEditor(source)
    editor.set_role('Alice', replace(editor.role('Alice'), pan=-.5, rate=2, volume=.25))
    editor.set_output('m4b', False)
    controller.edit_script(editor.script())
    controller.save_in_library()
    relative = controller.project_path.relative_to(app.root)
    digest = create_backup(app, tmp_path / 'backup')
    verify_backup(tmp_path / 'backup', digest)
    restore_backup(tmp_path / 'backup', tmp_path / 'restored', digest)
    blocked = Registry()
    blocked.hook = lambda: pytest.fail('Passive restore activated a voice registry.')
    opened = StudioController(private_root=tmp_path / 'restored', registry=blocked).open(
        tmp_path / 'restored' / relative)
    assert opened.draft.source_text == source and opened.script == controller.script
    assert opened.to_dict()['version'] == 2 and opened.plan().output_format == 'm4b'
    assert opened.plan().roles[1].pan == -.5 and opened.plan().export_stems is False
    second = create_backup(LibraryApp(tmp_path / 'restored'), tmp_path / 'rebackup')
    verify_backup(tmp_path / 'rebackup', second)
    assert blocked.calls == 0

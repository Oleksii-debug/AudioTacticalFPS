"""Real StudioDialog methods with a headless native-control protocol harness."""
from concurrent.futures import Future
from types import SimpleNamespace
import threading

import pytest

from carr_library.studio import ProjectStore
from carr_library.ui.studio_actions import StudioDialog


class Control:
    def __init__(self, value='', selection=0):
        self.value, self.selection = value, selection
        self.enabled = True
        self.focused = False
        self.modified = False

    def GetValue(self):
        return self.value

    def ChangeValue(self, value):
        self.value = value

    def GetSelection(self):
        return self.selection

    def Enable(self, enabled=True):
        self.enabled = enabled

    def DiscardEdits(self):
        self.modified = False

    def IsModified(self):
        return self.modified

    def SetInsertionPoint(self, position):
        pass

    def SetFocus(self):
        self.focused = True


class Dialog:
    ended = destroyed = False

    def EndModal(self, code):
        self.ended = True

    def ShowModal(self):
        return 0

    def Destroy(self):
        self.destroyed = True


class Executor:
    def __init__(self):
        self.future = Future()
        self.shutdowns = []

    def submit(self, operation):
        self.operation = operation
        return self.future

    def shutdown(self, **options):
        self.shutdowns.append(options)


class Wx:
    YES, NO, CANCEL, ID_CANCEL = 1, 2, 3, 4
    YES_NO, ICON_QUESTION = 8, 16

    def __init__(self):
        self.messages, self.after = [], []
        self.answer = self.CANCEL

    def MessageBox(self, *args):
        self.messages.append(args)
        return self.answer

    def CallAfter(self, function, *args):
        self.after.append(lambda: function(*args))

    def deliver(self):
        while self.after:
            self.after.pop(0)()


@pytest.fixture
def studio(tmp_path):
    # Importing StudioDialog does not import wx or create any native windows.
    studio = object.__new__(StudioDialog)
    studio.wx, studio.dialog = Wx(), Dialog()
    studio.store = ProjectStore(tmp_path / 'studio')
    studio.executor = Executor()
    studio.cancel, studio.pause = threading.Event(), threading.Event()
    studio.busy = studio.closed = False
    studio.saved_revision = None
    studio.player = studio.result = None
    studio.title = Control('Reviewed narration')
    studio.editor = Control('The original source words are preserved.')
    studio.status = Control()
    studio.mode = Control(selection=0)
    studio.voices = (SimpleNamespace(id='fixture-one'), SimpleNamespace(id='fixture-two'))
    studio.cast = [(Control(label), Control(selection=0), Control(selection=0))
                   for label in ('Narrator', 'Speaker one', 'Speaker two')]
    studio.saved_form = None
    assert studio.save() is not None
    return studio


def controls(studio):
    return (studio.title, studio.editor, studio.mode, *(control for row in studio.cast for control in row))


@pytest.mark.parametrize('field', ['title', 'mode', 'role', 'voice', 'pan'])
def test_non_editor_changes_prompt_and_cancel_preserves_the_entire_form(studio, field):
    if field == 'title':
        studio.title.value = 'A new unsaved title'
    elif field == 'mode':
        studio.mode.selection = 1
    elif field == 'role':
        studio.cast[0][0].value = 'New role name'
    elif field == 'voice':
        studio.cast[0][1].selection = 1
    else:
        studio.cast[0][2].selection = 1
    assert studio.editor.IsModified() is False
    before = studio._form_state()
    studio.close()
    assert len(studio.wx.messages) == 1
    assert studio.closed is False and studio.dialog.ended is False
    assert studio._form_state() == before


def test_invalid_json_save_on_close_preserves_draft_and_previous_revision(studio):
    original = studio.store.load()
    studio.mode.selection = 2
    studio.editor.value = '{invalid but valuable unsaved author draft'
    before = studio._form_state()
    studio.wx.answer = studio.wx.YES
    studio.close()
    assert studio.closed is False and studio.dialog.ended is False
    assert studio._form_state() == before
    assert studio.store.load() == original
    assert 'Could not save' in studio.status.value and studio.status.focused


def test_save_accepts_full_form_then_close_does_not_repeat_prompt(studio):
    studio.title.value = 'A deliberately saved new title'
    studio.cast[0][1].selection = 1
    studio.cast[0][2].selection = 2
    project = studio.save()
    assert project.title == 'A deliberately saved new title'
    assert project.roles[0].voice_id == 'fixture-two' and project.roles[0].pan == 1
    studio.close()
    assert studio.wx.messages == []
    assert studio.closed and studio.dialog.ended


def test_explicit_discard_never_publishes_unsaved_form(studio):
    original = studio.store.load()
    studio.title.value = 'Discard this title'
    studio.wx.answer = studio.wx.NO
    studio.close()
    assert studio.closed and studio.dialog.ended
    assert studio.store.load() == original


def test_narration_ignores_unused_dialogue_controls(studio):
    for row in studio.cast[1:]:
        row[0].value = 'Narrator'
        row[1].selection = -1
        row[2].selection = -1
    project = studio.project()
    assert len(project.roles) == 1
    assert project.scenes[0].lines[0].text == studio.editor.GetValue()
    studio.mode.selection = 1
    with pytest.raises(ValueError, match='voice'):
        studio.project()


def test_player_cleanup_error_does_not_trap_the_modal_or_executor(studio):
    def failed_close():
        raise RuntimeError('Playback driver stopped responding')
    studio.player = SimpleNamespace(close=failed_close)
    studio.close()
    assert studio.closed and studio.dialog.ended
    assert studio.player is None
    assert studio.executor.shutdowns == [dict(wait=False, cancel_futures=True)]
    assert 'driver' in studio.cleanup_error


def test_show_finally_destroys_controls_despite_modal_and_player_errors(studio):
    def failed_modal():
        raise RuntimeError('Native modal failed')
    def failed_player():
        raise RuntimeError('Playback driver cleanup failed')
    studio.dialog.ShowModal = failed_modal
    studio.player = SimpleNamespace(close=failed_player)
    with pytest.raises(RuntimeError, match='Native modal failed'):
        studio.show()
    assert studio.closed and studio.dialog.destroyed and studio.cancel.is_set()
    assert studio.executor.shutdowns


def test_submission_failure_restores_every_edit_control_and_saved_draft(studio):
    def failed_submit(operation):
        raise RuntimeError('Executor could not start')
    studio.executor.submit = failed_submit
    before = studio._form_state()
    studio.render()
    assert studio.busy is False
    assert all(c.enabled for c in controls(studio))
    assert studio._form_state() == before
    assert 'Could not start rendering' in studio.status.value


def test_failed_worker_result_restores_full_form_after_dispatch(studio):
    before = studio._form_state()
    studio.render()
    assert studio.busy and all(not c.enabled for c in controls(studio))
    studio.executor.future.set_exception(RuntimeError('Local engine unavailable'))
    assert studio.busy  # Only the UI event loop may touch native controls.
    studio.wx.deliver()
    assert not studio.busy and all(c.enabled for c in controls(studio))
    assert studio._form_state() == before
    assert 'Local engine unavailable' in studio.status.value
    assert studio.status.focused


def test_queued_completion_after_dialog_close_never_touches_destroyed_controls(studio):
    studio.render()
    studio.closed = True
    before = studio.status.value
    studio.executor.future.set_result({'master': {'duration_seconds': 1, 'path': '/fixture.wav'}})
    studio.wx.deliver()
    assert studio.status.value == before
    assert studio.result is None


def test_close_during_render_requests_pause_and_preserves_live_controls(studio):
    studio.render()
    vetoes = []
    event = SimpleNamespace(CanVeto=lambda: True, Veto=lambda: vetoes.append(True))
    studio.close(event)
    assert studio.pause.is_set() and not studio.closed
    assert vetoes == [True]
    assert not studio.dialog.ended and not studio.executor.shutdowns

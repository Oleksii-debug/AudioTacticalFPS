"""Native choice orchestration with actual conversion/storage and synthetic widgets."""
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from carr_library.audio.managed import ManagedProjects
from carr_library.ui import legacy_studio_import as action
from carr_library.ui.audio_studio import StudioController
from tests.audio.test_canonical_project_import import selected, with_cue, pcm


class Text:
    def __init__(self, value=''):
        self.value, self.focused = value, False
    def GetValue(self):
        return self.value
    def ChangeValue(self, value):
        self.value = value
    def SetFocus(self):
        self.focused = True


class NativeChoices:
    ID_OK, ID_YES, ID_NO = 1, 2, 3
    FD_OPEN, FD_FILE_MUST_EXIST, YES_NO, NO_DEFAULT, ICON_QUESTION = 1, 2, 4, 8, 16
    def __init__(self, paths, *, accept=True):
        self.paths, self.accept = list(paths), accept
        self.destroyed, self.questions = [], []
    def FileDialog(self, parent, title, **kwargs):
        path = self.paths.pop(0)
        return SimpleNamespace(ShowModal=lambda: self.ID_OK if path is not None else self.ID_NO,
            GetPath=lambda: str(path), Destroy=lambda: self.destroyed.append(title))
    def MessageDialog(self, parent, text, title, style):
        self.questions.append((title, text))
        return SimpleNamespace(ShowModal=lambda: self.ID_YES if self.accept else self.ID_NO,
            Destroy=lambda: self.destroyed.append(title))
    def SingleChoiceDialog(self, parent, text, title, choices):
        return SimpleNamespace(ShowModal=lambda: self.ID_OK, GetSelection=lambda: 0,
            Destroy=lambda: self.destroyed.append(title))


class Dialog:
    def __init__(self, app, paths, *, accept=True):
        self.controller = StudioController(private_root=app.root)
        self.wx = NativeChoices(paths, accept=accept)
        self.dialog = object()
        self.closed = self.busy = self.close_pending = False
        self.cancelled = Event()
        self.voice_rows = [{'id': 'sapi5:default', 'name': 'Selected local voice', 'available': True}]
        self.voice = SimpleNamespace(GetSelection=lambda: 0)
        self.language = Text('uk')
        self.plan_text, self.script = Text(), Text()
        self.phases, self.previews, self.messages, self.errors = [], [], [], []
        self.before_preview = lambda: None
        self.refreshed = False
    def _unsaved(self):
        return True
    def _stop_for_project_change(self):
        return True
    def _run(self, label, operation, completed):
        assert not self.busy
        self.phases.append(label)
        self.busy = True
        try:
            result = operation()
        except Exception as exc:
            self.errors.append(exc)
            self.busy = False
            return
        self.busy = False
        completed(result)
    def _show_text(self, title, text):
        self.previews.append((title, text))
        self.before_preview()
    def _refresh_text(self):
        self.refreshed = True
    def _message(self, value, **kwargs):
        self.messages.append(value)


@pytest.mark.parametrize('accept', [False, True])
def test_native_choices_require_separate_review_then_import(tmp_path, accept):
    app, path, raw = selected(tmp_path)
    dialog = Dialog(app, (path,), accept=accept)
    before = dialog.controller
    action.import_canonical_project(dialog)
    assert not dialog.errors
    assert len(dialog.previews) == 1 and len(dialog.wx.questions) == 1
    assert len(dialog.phases) == (3 if accept else 2)
    assert path.read_bytes() == raw
    if accept:
        assert dialog.controller is not before and before.project_path is None
        assert len(ManagedProjects(app.root).projects()) == 1
        assert dialog.refreshed and dialog.script.focused
        assert dialog.controller.source_reference['document_utf8'].encode('utf-8') == raw
    else:
        assert dialog.controller is before and before.project_path is None
        assert not (app.root / 'audio-projects').exists()


def test_native_cue_choice_grants_only_the_explicit_current_file(tmp_path):
    sound = tmp_path / 'current.wav'
    pcm(sound)
    value = with_cue(Path('/disconnected/no-longer-current.wav'))
    app, path, raw = selected(tmp_path, value)
    dialog = Dialog(app, (path, sound))
    action.import_canonical_project(dialog)
    assert not dialog.errors and dialog.controller.project_path is not None
    assert len(dialog.wx.destroyed) == 3  # project picker, cue picker, question
    assert dialog.controller.source_reference['document_utf8'].encode('utf-8') == raw
    clip, = dialog.controller.plan().scenes[0].clips
    assert (dialog.controller.output_dir / 'assets' / clip.asset).read_bytes() == sound.read_bytes()


def test_native_context_change_before_acceptance_never_creates_a_project(tmp_path):
    app, path, _ = selected(tmp_path)
    dialog = Dialog(app, (path,))
    dialog.before_preview = lambda: setattr(dialog.controller, 'project_id', 'a' * 32)
    action.import_canonical_project(dialog)
    assert not dialog.errors and not dialog.wx.questions
    assert not (app.root / 'audio-projects').exists()


def test_native_source_change_after_inspection_requires_another_review(tmp_path, monkeypatch):
    app, path, raw = selected(tmp_path)
    dialog = Dialog(app, (path,))
    inspect = action.inspect_project
    def changed(*args, **kwargs):
        result = inspect(*args, **kwargs)
        path.write_bytes(raw + b' ')
        return result
    monkeypatch.setattr(action, 'inspect_project', changed)
    action.import_canonical_project(dialog)
    assert len(dialog.errors) == 1 and 'changed after inspection' in str(dialog.errors[0])
    assert not dialog.previews and not dialog.wx.questions
    assert not (app.root / 'audio-projects').exists()


def test_native_file_choice_cancellation_does_not_start_a_worker(tmp_path):
    app, _, _ = selected(tmp_path)
    dialog = Dialog(app, (None,))
    action.import_canonical_project(dialog)
    assert not dialog.phases and len(dialog.wx.destroyed) == 1
    assert not (app.root / 'audio-projects').exists()


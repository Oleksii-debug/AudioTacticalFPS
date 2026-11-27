"""Exercise actual dialog handlers with widget stand-ins; Windows smoke is separate."""
import importlib.util
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from carr_library.ui.operation_progress import ProgressTracker, operation_outcome
from carr_library.ui.transcription_dialog import create_transcription_dialog, describe_job, install_transcription_actions

# Reuse explicitly synthetic subprocess fixtures; never import the private worker.
_spec = importlib.util.spec_from_file_location('transcription_protocol_fixtures', Path(__file__).with_name('test_transcription.py'))
_fixtures = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixtures)
engine = _fixtures.engine
source = _fixtures.source


class Widget:
    def __init__(self, parent=None, *args, value='', label='', choices=(), **kwargs):
        self.parent, self.value, self.label = parent, value, label
        self.choices, self.selection, self.enabled = list(choices), -1, True
        self.name, self.editable = '', True
    def SetName(self, name): self.name = name
    def GetName(self): return self.name
    def GetValue(self): return self.value
    def SetValue(self, value): self.value = value
    def ChangeValue(self, value): self.value = value
    def SetLabel(self, value): self.label = value
    def GetLabel(self): return self.label
    def Wrap(self, width): pass
    def Bind(self, *args, **kwargs): pass
    def Enable(self, value=True): self.enabled = value
    def Disable(self): self.enabled = False
    def IsEnabled(self): return self.enabled
    def SetEditable(self, value): self.editable = value
    def SetStringSelection(self, value): self.selection = self.choices.index(value)
    def GetStringSelection(self): return self.choices[self.selection] if self.selection >= 0 else ''
    def SetSelection(self, index): self.selection = index
    def GetSelection(self): return self.selection
    def Set(self, values): self.choices = list(values); self.selection = -1
    def Clear(self): self.Set([])
    def SetSizer(self, sizer): pass
    def SetScrollRate(self, x, y): pass
    def FitInside(self): pass
    def IsModal(self): return False
    def Hide(self): self.hidden = True
    def Destroy(self): pass


class Sizer:
    def __init__(self, *args, **kwargs): pass
    def Add(self, *args, **kwargs): pass
    def AddGrowableCol(self, *args): pass


class Progress:
    def __init__(self): self.tracker = ProgressTracker(); self.events = []
    @property
    def event(self): return self.tracker.event
    def start(self, title): return self.tracker.start(title).token
    def update(self, *args, **kwargs):
        event = self.tracker.update(*args, **kwargs)
        if event is not None: self.events.append(event)
    def pause(self, phase, **kwargs): self.tracker.transition('paused', phase, **kwargs)
    def resume(self, phase, **kwargs): self.tracker.transition('running', phase, **kwargs)


@pytest.fixture
def desktop(tmp_path):
    root = tmp_path / 'library'; root.mkdir()
    constants = ('DEFAULT_DIALOG_STYLE','RESIZE_BORDER','VERTICAL','HORIZONTAL','EXPAND','ALL',
        'ALIGN_CENTER_VERTICAL','TE_MULTILINE','TE_READONLY','VSCROLL','EVT_BUTTON','EVT_CHOICE',
        'EVT_CLOSE','EVT_MENU','FD_SAVE','FD_OPEN','FD_FILE_MUST_EXIST','FD_MULTIPLE','ID_OK','ID_CANCEL','ID_CLOSE')
    wx = SimpleNamespace(**{name:index + 1 for index,name in enumerate(constants)})
    for name in ('Dialog','StaticText','TextCtrl','Button','Choice','ListBox','CheckBox','ScrolledWindow'):
        setattr(wx, name, Widget)
    wx.BoxSizer = wx.FlexGridSizer = Sizer
    wx.CallAfter = lambda callback, *args: callback(*args)
    frame = SimpleNamespace(library=SimpleNamespace(root=root), working=False, _closing=False,
                            cancel_event=threading.Event(), operation_progress=Progress(), jobs=[])
    def run_job(title, operation, done, failed):
        frame.working = True; frame.cancel_event.clear()
        token = frame.operation_progress.start(title)
        frame.jobs.append(title)
        try:
            result = operation()
            done(result)
            outcome = operation_outcome(result)
            frame.operation_progress.tracker.transition(outcome, title, token=token)
        except Exception as exc:
            failed(exc)
            frame.operation_progress.tracker.transition('failed', str(exc), token=token)
        finally:
            frame.working = False
    frame.run_job = run_job
    dialog = create_transcription_dialog(frame, wx_module=wx)
    return frame, dialog, wx


def test_native_handler_waiting_input_does_not_start_background_job(desktop, source):
    frame, dialog, _ = desktop
    dialog.set_files([source])
    dialog.on_run()
    assert not frame.jobs
    assert 'Configure the reviewed local' in dialog.status.GetValue()
    assert 'waiting-input' in dialog.profile_status.GetLabel()
    assert dialog.current_job is None


def test_real_subprocess_dialog_flow_uses_shared_progress_and_derived_revision(desktop, source, engine):
    frame, dialog, _ = desktop
    dialog.service.configure_engine(engine())
    dialog.refresh_capability()
    dialog.set_files([source])
    dialog.on_run()
    assert frame.jobs == ['Local transcription']
    assert frame.operation_progress.event.state == 'succeeded', dialog.status.GetValue()
    assert not dialog.busy and not dialog.buttons['pause'].IsEnabled()
    assert len(dialog.artifacts) == 4 and dialog.editor.GetValue().startswith('Synthetic transcript')
    assert 'Synthetic fixture; no acoustic quality claim' in dialog.status.GetValue()
    assert '24-hour' in dialog.status.GetValue()
    assert 'Reviewed worker SHA-256' in dialog.profile_status.GetLabel()
    assert 'separate' in dialog.profile_status.GetLabel()
    assert any(event.total is None and 'Copying' in event.phase for event in frame.operation_progress.events)
    assert all(event.percent != 100 for event in frame.operation_progress.events)
    original = dialog.editor.GetValue()
    dialog.editor.ChangeValue('Edited derived transcript')
    dialog.on_revision()
    assert dialog.revision
    artifact = dialog.artifacts[dialog.artifact_choice.GetSelection()]
    assert dialog.service.read_transcript(dialog.current_job, artifact['path']) == original
    assert not dialog.buttons['pause'].IsEnabled()


def test_close_cancels_without_worker_touching_destroyed_controls(desktop):
    _, dialog, _ = desktop
    dialog.on_close()
    assert dialog.cancel.is_set() and not dialog.alive
    before = dialog.status.GetValue()
    dialog.report('late completion')
    assert dialog.status.GetValue() == before


def test_other_background_operation_prevents_transcription(desktop, source, engine):
    frame, dialog, _ = desktop
    dialog.service.configure_engine(engine())
    dialog.set_files([source]); frame.working = True
    dialog.on_run()
    assert frame.jobs == [] and 'Another operation' in dialog.status.GetValue()
    assert not dialog.busy


def test_library_change_prevents_engine_execution(desktop, source, engine, tmp_path):
    frame, dialog, _ = desktop
    dialog.service.configure_engine(engine()); dialog.set_files([source])
    root = tmp_path / 'different'; root.mkdir(); frame.library.root = root
    dialog.on_run()
    assert not frame.jobs and 'library location changed' in dialog.status.GetValue()


def test_large_transcript_placeholder_cannot_be_saved_as_revision(desktop, source, engine, monkeypatch):
    _, dialog, _ = desktop
    dialog.service.configure_engine(engine()); dialog.set_files([source]); dialog.on_run()
    monkeypatch.setattr(dialog.service, 'read_transcript', lambda *a, **k: 'x' * 2_000_001)
    dialog.on_artifact()
    assert not dialog.buttons['revision'].IsEnabled()
    assert not dialog.editor.editable
    assert 'exceeds the editor limit' in dialog.editor.GetValue()


def test_bad_beam_size_fails_before_submission(desktop, source, engine):
    frame, dialog, _ = desktop
    dialog.service.configure_engine(engine()); dialog.set_files([source])
    dialog.settings_controls['beam_size'].ChangeValue('9' * 500)
    dialog.on_run()
    assert not frame.jobs and 'beam size' in dialog.status.GetValue()


def test_transcript_outputs_have_explicit_accessible_names(desktop):
    _, dialog, _ = desktop
    assert dialog.selected_files.GetName() == 'Explicitly selected audio input files'
    assert dialog.editor.GetName() == 'Transcript text; save edits as a new derived revision'
    assert dialog.artifact_choice.GetName() == 'Verified transcript output'
    assert not dialog.buttons['pause'].IsEnabled()
    assert not dialog.buttons['revision'].IsEnabled()


def test_existing_tools_menu_receives_one_transcription_action(monkeypatch):
    from carr_library.ui import transcription_dialog
    added, bindings = [], []
    menu = SimpleNamespace(Append=lambda *args: added.append(args))
    bar = SimpleNamespace(GetMenuCount=lambda:2, GetMenuLabel=lambda i:('&Help','&Tools')[i], GetMenu=lambda i:menu)
    frame = SimpleNamespace(menu_bar=bar, Bind=lambda *a, **k: bindings.append((a,k)))
    monkeypatch.setitem(__import__('sys').modules,'wx',SimpleNamespace(NewIdRef=lambda:731,EVT_MENU=4))
    opened = []
    monkeypatch.setattr(transcription_dialog,'show_transcription',lambda owner:opened.append(owner))
    install_transcription_actions(frame)
    assert added == [(731,'Local audio &transcription...')]
    bindings[0][0][1](None)
    assert opened == [frame]


def test_transcript_reading_stays_inside_shared_background_operation(desktop, source, engine, monkeypatch):
    frame, dialog, _ = desktop
    dialog.service.configure_engine(engine());dialog.set_files([source])
    original = dialog.service.read_transcript
    observed = []
    def read(*args, **kwargs):
        assert frame.working, 'file verification/read must run inside the shared background operation'
        observed.append(args)
        return original(*args, **kwargs)
    monkeypatch.setattr(dialog.service, 'read_transcript', read)
    dialog.on_run()
    assert observed and frame.operation_progress.event.state == 'succeeded'
    before = len(frame.jobs)
    dialog.on_artifact()
    assert frame.jobs[before:] == ['Reading verified transcript']
    assert len(observed) == 2

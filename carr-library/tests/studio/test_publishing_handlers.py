"""Actual publication handlers and real FFmpeg exports with widget stand-ins."""
import importlib.util
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from carr_library.application import LibraryApp
from carr_library.ui.operation_progress import ProgressTracker
from carr_library.ui.publishing_dialog import create_publishing_dialog

_spec = importlib.util.spec_from_file_location('publishing_handler_fixtures', Path(__file__).with_name('test_publishing.py'))
_fixtures = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(_fixtures)
actual_job = _fixtures.actual_job
source_job = _fixtures.source_job


class Widget:
    def __init__(self, parent=None, *args, value='', label='', choices=(), **kwargs):
        self.value, self.label, self.choices = value, label, list(choices)
        self.selection, self.enabled, self.name = -1, True, ''
    def SetName(self, name): self.name = name
    def GetName(self): return self.name
    def GetValue(self): return self.value
    def ChangeValue(self, value): self.value = value
    def Set(self, values): self.choices = list(values); self.selection = -1
    def SetSelection(self, index): self.selection = index
    def GetSelection(self): return self.selection
    def Enable(self, enabled=True): self.enabled = enabled
    def IsEnabled(self): return self.enabled
    def Bind(self, *args, **kwargs): pass
    def SetSizer(self, *args): pass
    def SetScrollRate(self, *args): pass
    def FitInside(self): pass
    def Wrap(self, *args): pass
    def SetFocus(self): self.focused = True
    def IsModal(self): return False
    def Hide(self): self.hidden = True


class Sizer:
    def __init__(self, *args, **kwargs): pass
    def Add(self, *args, **kwargs): pass


@pytest.fixture
def desktop(source_job):
    service, state = source_job
    library = LibraryApp(service.root.parent.parent)
    constants = ('DEFAULT_DIALOG_STYLE','RESIZE_BORDER','VSCROLL','VERTICAL','HORIZONTAL','ALL','EXPAND',
                 'TE_MULTILINE','TE_READONLY','EVT_BUTTON','EVT_CLOSE','ID_OK','ID_CLOSE','FD_SAVE')
    wx = SimpleNamespace(**{name:index + 1 for index, name in enumerate(constants)})
    for name in ('Dialog','StaticText','TextCtrl','Choice','Button','ScrolledWindow'):
        setattr(wx, name, Widget)
    wx.BoxSizer = Sizer
    wx.CallAfter = lambda callback, *args: callback(*args)
    progress = ProgressTracker()
    frame = SimpleNamespace(library=library, cancel_event=Event(), working=False, _closing=False,
                            operation_progress=progress, operations=[], facade_calls=[], observer_errors=[])
    def run_job(title, operation, done, failed, *, with_progress=False):
        frame.working = True; frame.cancel_event.clear(); frame.operations.append(title)
        token = progress.start(title)
        def report_progress(completed=None, total=None, phase=None):
            frame.facade_calls.append(phase)
            try:
                progress.update(completed, total, phase, token=token)
            except Exception as exc:
                frame.observer_errors.append(exc)
        try:
            done(operation(report_progress) if with_progress else operation())
        except Exception as exc:
            failed(exc)
        finally:
            frame.working = False
    frame.run_job = run_job
    dialog = create_publishing_dialog(frame, wx_module=wx)
    return frame, dialog, state


def test_native_handler_requires_explicit_completed_job_and_destination(desktop):
    frame, dialog, _ = desktop
    assert not frame.operations
    dialog.on_export()
    assert not frame.operations and 'explicitly choose' in dialog.status.GetValue()
    dialog.on_refresh()
    assert len(dialog.jobs) == 1 and dialog.job_choice.GetSelection() == -1
    dialog.job_choice.SetSelection(0); dialog.on_export()
    assert len(frame.operations) == 1 and 'Choose a new EPUB' in dialog.status.GetValue()


def test_actual_native_handler_exports_and_verifies_same_receipt(desktop, tmp_path):
    frame, dialog, _ = desktop
    dialog.on_refresh(); dialog.job_choice.SetSelection(0)
    output = tmp_path / 'native-handler.epub'; dialog.output.ChangeValue(str(output))
    dialog.on_export()
    assert output.exists(), dialog.status.GetValue()
    assert not dialog.busy and not dialog.buttons['cancel'].IsEnabled()
    assert 'EPUB saved and its bytes verified' in dialog.status.GetValue()
    assert len(dialog.receipts) == 1 and dialog.receipt_choice.GetSelection() == 0
    dialog.on_verify()
    assert 'Verified exact saved EPUB bytes' in dialog.status.GetValue()
    original = output.read_bytes(); dialog.on_export()
    assert 'not be overwritten' in dialog.status.GetValue() and output.read_bytes() == original


def test_native_handler_obeys_shared_background_job_and_library_scope(desktop, tmp_path):
    frame, dialog, _ = desktop
    dialog.on_refresh(); dialog.job_choice.SetSelection(0); dialog.output.ChangeValue(str(tmp_path / 'blocked.epub'))
    frame.working = True; dialog.on_export()
    assert len(frame.operations) == 1 and 'Another operation' in dialog.status.GetValue()
    frame.working = False; frame.library = LibraryApp(tmp_path / 'other-library'); dialog.on_export()
    assert len(frame.operations) == 1 and 'library location changed' in dialog.status.GetValue()
    assert not (tmp_path / 'blocked.epub').exists()


def test_close_cancels_and_suppresses_late_native_result(desktop, tmp_path):
    frame, dialog, _ = desktop
    dialog.on_refresh(); dialog.job_choice.SetSelection(0); dialog.output.ChangeValue(str(tmp_path / 'late.epub'))
    pending = []
    frame.run_job = lambda title, operation, done, failed, **kwargs: pending.append((operation, done, failed, kwargs))
    dialog.on_export(); assert dialog.busy
    dialog.on_close(); message = dialog.status.GetValue()
    assert dialog.cancel.is_set() and frame.cancel_event.is_set()
    operation, done, failed, kwargs = pending[0]
    assert kwargs == {'with_progress': True}
    observed = []
    try: done(operation(lambda **values: None))
    except Exception as exc:
        observed.append(exc)
        failed(exc)
    assert len(observed) == 1 and isinstance(observed[0], InterruptedError)
    assert dialog.status.GetValue() == message and not (tmp_path / 'late.epub').exists()


def test_invalid_budget_does_not_start_native_export(desktop, tmp_path):
    frame, dialog, _ = desktop
    dialog.on_refresh(); dialog.job_choice.SetSelection(0); dialog.output.ChangeValue(str(tmp_path / 'invalid.epub'))
    dialog.minutes.ChangeValue('nan'); dialog.on_export()
    assert len(frame.operations) == 1 and not (tmp_path / 'invalid.epub').exists()


def test_export_uses_host_progress_facade_without_reading_shared_event(desktop, tmp_path):
    frame, dialog, _ = desktop
    dialog.on_refresh(); dialog.job_choice.SetSelection(0)
    target = tmp_path / 'shared-facade.epub'
    dialog.output.ChangeValue(str(target))
    frame.operation_progress = object()  # Host owns its tracker; consumers receive only its callback.
    dialog.on_export()
    assert target.exists() and dialog.receipts[0]['status'] == 'complete'
    assert frame.facade_calls[-1] == 'EPUB saved and its exact bytes verified'
    assert 'EPUB saved and its bytes verified' in dialog.status.GetValue()
    assert not dialog.busy and dialog.buttons['export'].IsEnabled()


def test_failed_host_admission_restores_publication_controls(desktop, tmp_path):
    frame, dialog, _ = desktop
    dialog.on_refresh(); dialog.job_choice.SetSelection(0)
    target = tmp_path / 'not-started.epub'
    dialog.output.ChangeValue(str(target))
    def fail(*args, **kwargs):
        raise RuntimeError('Synthetic host admission failure')
    frame.run_job = fail
    dialog.on_export()
    assert not target.exists() and not dialog.busy
    assert 'Synthetic host admission failure' in dialog.status.GetValue()
    assert dialog.buttons['export'].IsEnabled() and not dialog.buttons['cancel'].IsEnabled()


def test_broken_progress_observer_does_not_discard_committed_receipt(desktop, tmp_path, monkeypatch):
    frame, dialog, _ = desktop
    dialog.on_refresh(); dialog.job_choice.SetSelection(0)
    target = tmp_path / 'observer-error.epub'
    dialog.output.ChangeValue(str(target))
    def fail(*args, **kwargs):
        raise RuntimeError('Synthetic observer failure')
    monkeypatch.setattr(frame.operation_progress, 'update', fail)
    dialog.on_export()
    assert target.exists() and frame.observer_errors
    assert dialog.receipts[0]['status'] == 'complete'
    assert 'EPUB saved and its bytes verified' in dialog.status.GetValue()
    assert not dialog.busy

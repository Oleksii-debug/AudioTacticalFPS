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
        self.identity, self.bindings = id(self), {}
    def GetId(self): return self.identity
    def SetName(self, name): self.name = name
    def GetName(self): return self.name
    def GetValue(self): return self.value
    def ChangeValue(self, value): self.value = value
    def Set(self, values): self.choices = list(values); self.selection = -1
    def SetSelection(self, index): self.selection = index
    def GetSelection(self): return self.selection
    def Enable(self, enabled=True): self.enabled = enabled
    def IsEnabled(self): return self.enabled
    def Bind(self, event, callback, **kwargs): self.bindings[event] = callback
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
                 'TE_MULTILINE','TE_READONLY','EVT_BUTTON','EVT_CLOSE','EVT_WINDOW_DESTROY','ID_OK','ID_CLOSE','FD_SAVE')
    wx = SimpleNamespace(**{name:index + 1 for index, name in enumerate(constants)})
    for name in ('Dialog','StaticText','TextCtrl','Choice','Button','ScrolledWindow'):
        setattr(wx, name, Widget)
    wx.BoxSizer = Sizer
    wx.CallAfter = lambda callback, *args: callback(*args)
    progress = ProgressTracker()
    frame = SimpleNamespace(library=library, cancel_event=Event(), working=False, _closing=False,
                            operation_progress=progress, operations=[], test_wx=wx)
    def run_job(title, operation, done, failed):
        frame.working = True; frame.cancel_event.clear(); frame.operations.append(title)
        progress.start(title)
        try:
            done(operation())
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
    frame.run_job = lambda title, operation, done, failed: pending.append((operation, done, failed))
    dialog.on_export(); assert dialog.busy
    dialog.on_close(); message = dialog.status.GetValue()
    assert dialog.cancel.is_set() and frame.cancel_event.is_set()
    operation, done, failed = pending[0]
    try: done(operation())
    except Exception as exc: failed(exc)
    assert dialog.status.GetValue() == message and not (tmp_path / 'late.epub').exists()


def test_invalid_budget_does_not_start_native_export(desktop, tmp_path):
    frame, dialog, _ = desktop
    dialog.on_refresh(); dialog.job_choice.SetSelection(0); dialog.output.ChangeValue(str(tmp_path / 'invalid.epub'))
    dialog.minutes.ChangeValue('nan'); dialog.on_export()
    assert len(frame.operations) == 1 and not (tmp_path / 'invalid.epub').exists()


@pytest.mark.parametrize('phase', ['pending-work', 'queued-result', 'queued-error'])
def test_direct_publication_destruction_detaches_pending_callbacks(desktop, phase):
    frame, dialog, _ = desktop
    pending, calls, completed = [], [], []
    frame.run_job = lambda title, operation, done, failed: pending.append((operation, done, failed))
    assert dialog.begin('Lifetime boundary', lambda: calls.append('operation') or 'durable result', completed.append)
    operation, done, failed = pending.pop()
    value = operation() if phase != 'pending-work' else None
    event = SimpleNamespace(GetId=lambda: dialog.owner_id, Skip=lambda: None,
        GetEventObject=lambda: pytest.fail('The dying wrapper must not be inspected'))
    dialog.on_destroy(event)
    assert dialog.destroyed and not dialog.alive and dialog.cancel.is_set() and frame.cancel_event.is_set()
    def dead(*args, **kwargs): pytest.fail('Destroyed publication native control accessed')
    for control in (dialog.status, dialog.job_choice, dialog.receipt_choice, *dialog.buttons.values()):
        control.ChangeValue = control.Set = control.Enable = control.SetFocus = dead
    dialog.IsModal = dialog.Hide = dead
    if phase == 'pending-work':
        with pytest.raises(InterruptedError): operation()
        failed(InterruptedError('Cancelled before queued work'))
        assert calls == []
    elif phase == 'queued-result':
        done(value)
    else:
        failed(RuntimeError('Queued failure'))
    dialog.on_close()
    dialog.report('Late progress')
    assert not dialog.busy and completed == []


def test_child_destruction_keeps_publication_owner_usable(desktop):
    _, dialog, _ = desktop
    dialog.on_destroy(SimpleNamespace(GetId=lambda: dialog.owner_id + 1, Skip=lambda: None))
    assert dialog.alive and not dialog.destroyed and not dialog.cancel.is_set()
    dialog.on_refresh()
    assert len(dialog.jobs) == 1 and not dialog.busy


def test_native_handler_reports_committed_epub_when_cancellation_arrives_after_link(
        desktop, tmp_path, monkeypatch):
    from carr_library.studio import publishing
    frame, dialog, _ = desktop
    dialog.on_refresh(); dialog.job_choice.SetSelection(0)
    target = tmp_path / 'late-native-cancel.epub'
    dialog.output.ChangeValue(str(target))
    original = publishing.publish_file

    def publish_then_cancel(staged, output, **kwargs):
        original(staged, output, **kwargs)
        frame.cancel_event.set()
        dialog.cancel.set()

    monkeypatch.setattr(publishing, 'publish_file', publish_then_cancel)
    dialog.on_export()
    assert target.exists() and len(dialog.receipts) == 1, dialog.status.GetValue()
    assert dialog.receipts[0]['status'] == 'complete'
    assert 'EPUB saved and its bytes verified' in dialog.status.GetValue()
    assert not dialog.busy and not dialog.buttons['cancel'].IsEnabled()


def test_native_handler_distinguishes_saved_output_from_prepared_receipt(
        desktop, tmp_path, monkeypatch):
    from carr_library.studio import publishing
    frame, dialog, _ = desktop
    dialog.on_refresh(); dialog.job_choice.SetSelection(0)
    target = tmp_path / 'native-receipt-warning.epub'
    dialog.output.ChangeValue(str(target))
    original = publishing.atomic_json

    def finalization_failure(path, record, **kwargs):
        if record.get('schema') == 'resonance-publication/1' and record.get('status') == 'complete':
            raise OSError('Synthetic receipt persistence failure')
        return original(path, record, **kwargs)

    monkeypatch.setattr(publishing, 'atomic_json', finalization_failure)
    dialog.on_export()
    assert target.exists() and len(dialog.receipts) == 1, dialog.status.GetValue()
    receipt = dialog.receipts[0]
    assert receipt['status'] == 'complete' and receipt['receipt_status'] == 'prepared'
    assert 'EPUB saved and its bytes verified' in dialog.status.GetValue()
    assert 'Verify this existing publication' in dialog.status.GetValue()
    assert dialog.receipt_choice.choices[0].startswith('prepared; ')
    monkeypatch.setattr(publishing, 'atomic_json', original)
    dialog.on_verify()
    assert 'Verified exact saved EPUB bytes' in dialog.status.GetValue()
    assert dialog.receipt_choice.choices[0].startswith('complete; ')


def test_queued_native_export_retains_the_selected_directory_identity(desktop, tmp_path):
    frame, dialog, _ = desktop
    dialog.on_refresh(); dialog.job_choice.SetSelection(0)
    selected = tmp_path / 'chosen-export-directory'; selected.mkdir()
    target = selected / 'book.epub'
    dialog.output.ChangeValue(str(target))
    pending = []
    frame.run_job = lambda title, operation, done, failed: pending.append((operation, done, failed))
    dialog.on_export()
    assert dialog.busy and len(pending) == 1
    moved = tmp_path / 'original-export-directory'
    selected.rename(moved)
    selected.mkdir()
    operation, done, failed = pending[0]
    try:
        done(operation())
    except Exception as exc:
        failed(exc)
    assert not dialog.busy and not dialog.receipts
    assert 'destination changed before publication' in dialog.status.GetValue()
    assert not target.exists() and not (moved / 'book.epub').exists()


def test_daisy_handler_requires_publisher_and_verifies_selected_receipt(desktop, tmp_path):
    from carr_library.studio.daisy import inspect_daisy
    frame, dialog, _ = desktop
    dialog.on_refresh(); dialog.job_choice.SetSelection(0); dialog.format_choice.SetSelection(1)
    output = tmp_path / 'native-handler-daisy.zip'; dialog.output.ChangeValue(str(output))
    dialog.on_export()
    assert not output.exists() and len(frame.operations) == 1 and 'publisher metadata' in dialog.status.GetValue()
    dialog.publisher.ChangeValue('Synthetic publisher chosen in the UI')
    dialog.on_export()
    assert output.exists(), dialog.status.GetValue()
    assert len(dialog.receipts) == 1 and dialog.receipts[0]['format'] == 'daisy-2.02'
    assert inspect_daisy(output)['publisher'] == 'Synthetic publisher chosen in the UI'
    dialog.format_choice.SetSelection(0)  # Verification follows its receipt, not a changed export preference.
    dialog.on_verify()
    assert 'Verified exact saved DAISY bytes' in dialog.status.GetValue()


def test_queued_daisy_handler_captures_reviewed_format_and_publisher(desktop, tmp_path):
    from carr_library.studio.daisy import inspect_daisy
    frame, dialog, _ = desktop
    dialog.on_refresh(); dialog.job_choice.SetSelection(0); dialog.format_choice.SetSelection(1)
    output = tmp_path / 'captured-daisy.zip'; dialog.output.ChangeValue(str(output))
    dialog.publisher.ChangeValue('Reviewed publisher')
    pending = []
    frame.run_job = lambda title, operation, done, failed: pending.append((operation, done, failed))
    dialog.on_export()
    assert dialog.busy and not dialog.format_choice.IsEnabled() and not dialog.publisher.IsEnabled()
    dialog.format_choice.SetSelection(0); dialog.publisher.ChangeValue('Later value')
    operation, done, failed = pending[0]
    try: done(operation())
    except Exception as exc: failed(exc)
    assert output.exists(), dialog.status.GetValue()
    assert inspect_daisy(output)['publisher'] == 'Reviewed publisher'


def test_queued_daisy_export_retains_the_reviewed_directory_identity(desktop, tmp_path):
    frame, dialog, _ = desktop
    dialog.on_refresh(); dialog.job_choice.SetSelection(0); dialog.format_choice.SetSelection(1)
    dialog.publisher.ChangeValue('Explicit publisher')
    selected = tmp_path / 'selected-daisy-directory'; selected.mkdir()
    target = selected / 'book.zip'
    dialog.output.ChangeValue(str(target))
    pending = []
    frame.run_job = lambda title, operation, done, failed: pending.append((operation, done, failed))
    dialog.on_export()
    assert dialog.busy and len(pending) == 1
    moved = tmp_path / 'original-daisy-directory'
    selected.rename(moved); selected.mkdir()
    operation, done, failed = pending[0]
    try:
        done(operation())
    except Exception as exc:
        failed(exc)
    assert not dialog.busy and not dialog.receipts
    assert 'destination changed before publication' in dialog.status.GetValue()
    assert not target.exists() and not (moved / 'book.zip').exists()


@pytest.mark.parametrize('termination', ('native-parent', 'picker-only'))
def test_publication_picker_detaches_before_native_modal_return(desktop, monkeypatch, termination):
    frame, dialog, _ = desktop
    forbidden, observed = [], []
    original = dialog.output.GetValue()

    def dead(name):
        def fail(*args, **kwargs):
            forbidden.append(name)
            pytest.fail('Destroyed native picker accessed: ' + name)
        return fail

    class Picker(Widget):
        def ShowModal(self):
            observed.append('modal entered')
            if termination == 'native-parent':
                dialog.on_destroy(SimpleNamespace(GetId=lambda: dialog.owner_id, Skip=lambda: None))
                dialog.output.ChangeValue = dead('output.ChangeValue')
            self.bindings[frame.test_wx.EVT_WINDOW_DESTROY](
                SimpleNamespace(GetId=self.GetId, Skip=lambda: None))
            self.GetPath, self.Destroy = dead('picker.GetPath'), dead('picker.Destroy')
            return frame.test_wx.ID_OK

    monkeypatch.setattr(frame.test_wx, 'FileDialog', Picker, raising=False)
    dialog.on_choose()
    assert observed == ['modal entered'] and forbidden == [] and not frame.operations
    assert dialog.output.GetValue() == original
    assert dialog.alive is (termination == 'picker-only')


@pytest.mark.parametrize('daisy', (False, True))
def test_live_publication_picker_preserves_chosen_path_and_disposes_once(desktop, tmp_path, monkeypatch, daisy):
    frame, dialog, _ = desktop
    dialog.format_choice.SetSelection(1 if daisy else 0)
    selected = tmp_path / ('chosen.zip' if daisy else 'chosen.epub')
    observed = []

    class Picker(Widget):
        def __init__(self, *args, **options):
            super().__init__(*args, **options)
            observed.append(options)
        def ShowModal(self):
            return frame.test_wx.ID_OK
        def GetPath(self):
            return str(selected)
        def Destroy(self):
            observed.append('disposed')
            self.bindings[frame.test_wx.EVT_WINDOW_DESTROY](
                SimpleNamespace(GetId=self.GetId, Skip=lambda: None))

    monkeypatch.setattr(frame.test_wx, 'FileDialog', Picker, raising=False)
    dialog.on_choose()
    assert dialog.output.GetValue() == str(selected) and dialog.alive
    assert observed[0]['defaultFile'] == ('publication.zip' if daisy else 'publication.epub')
    assert observed[1:] == ['disposed'] and not frame.operations

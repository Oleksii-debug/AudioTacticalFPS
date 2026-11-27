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
    _next_id = 100
    def __init__(self, parent=None, *args, value='', label='', choices=(), **kwargs):
        Widget._next_id += 1
        self.native_id, self.destroyed, self.bindings = Widget._next_id, False, {}
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
    def GetId(self): return self.native_id
    def __bool__(self): return not self.destroyed
    def Destroy(self): self.destroyed = True; return True
    def Bind(self, event, handler, **kwargs): self.bindings[event] = handler
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
                            operation_progress=progress, operations=[])
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
    assert 'Verify this existing EPUB' in dialog.status.GetValue()
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


def test_rewrapped_native_destruction_detaches_queued_callbacks_without_widget_access(
        desktop, tmp_path, monkeypatch):
    frame, dialog, _ = desktop
    dialog.on_refresh()
    dialog.job_choice.SetSelection(0)
    dialog.output.ChangeValue(str(tmp_path / 'never-published.epub'))
    pending = []
    frame.run_job = lambda title, operation, done, failed: pending.append((operation, done, failed))
    dialog.on_export()
    assert dialog.busy and len(pending) == 1
    native_id = dialog._native_id
    skipped = []

    def destruction(identity):
        # The event deliberately wraps a different object; identity is native.
        peer = SimpleNamespace(GetId=lambda: identity)
        return SimpleNamespace(GetId=lambda: identity, GetEventObject=lambda: peer,
                               Skip=lambda: skipped.append(identity))

    dialog.on_destroy(destruction(native_id + 1000))
    assert dialog.alive and not dialog.cancel.is_set()
    monkeypatch.setattr(dialog, 'GetId',
        lambda: (_ for _ in ()).throw(AssertionError('Destroyed wrapper native ID was queried.')))
    dialog.on_destroy(destruction(native_id))
    assert not dialog.alive and dialog.cancel.is_set() and frame.cancel_event.is_set()
    assert skipped == [native_id + 1000, native_id]

    def deleted_control(*args, **kwargs):
        raise AssertionError('A late callback accessed a deleted native control.')

    controls = [dialog, dialog.job_choice, dialog.language, dialog.creator, dialog.output,
                dialog.max_gib, dialog.minutes, dialog.receipt_choice, dialog.status,
                *dialog.buttons.values()]
    for control in controls:
        for name in ('GetValue', 'GetSelection', 'ChangeValue', 'SetSelection',
                     'Set', 'Enable', 'SetFocus', 'IsModal', 'Hide'):
            monkeypatch.setattr(control, name, deleted_control, raising=False)
    operation, done, failed = pending[0]
    done({'status': 'complete'})
    failed(RuntimeError('Late native operation error'))
    dialog.report('Queued progress after native deletion')
    dialog.controls()
    for callback in (dialog.on_refresh, dialog.on_choose, dialog.on_export, dialog.on_verify,
                     dialog.on_cancel, dialog.on_close):
        callback()
    assert not dialog.busy and not dialog.receipts
    assert dialog.Destroy() is False
    assert not (tmp_path / 'never-published.epub').exists()


def test_direct_destroy_detaches_before_deferred_native_deletion(desktop):
    frame, dialog, _ = desktop
    pending = []
    frame.run_job = lambda title, operation, done, failed: pending.append((operation, done, failed))
    dialog.on_refresh()
    assert dialog.busy and dialog.alive
    assert dialog.Destroy() is True
    assert not dialog and not dialog.alive and dialog.cancel.is_set() and frame.cancel_event.is_set()
    assert dialog.Destroy() is False
    _, done, failed = pending[0]
    done(((), ()))
    failed(RuntimeError('Late completion after direct Destroy'))
    dialog.report('Late progress')
    assert not dialog.busy


def test_modal_cleanup_does_not_destroy_an_already_deleted_native_wrapper(monkeypatch):
    from carr_library.ui import publishing_dialog
    calls = []

    class AlreadyDeleted:
        def ShowModal(self):
            calls.append('modal completed')
        def on_close(self):
            calls.append('detached')
        def __bool__(self):
            return False
        def Destroy(self):
            raise AssertionError('A deleted native dialog was destroyed twice.')

    monkeypatch.setattr(publishing_dialog, 'create_publishing_dialog', lambda frame: AlreadyDeleted())
    publishing_dialog.show_publishing(object())
    assert calls == ['modal completed', 'detached']

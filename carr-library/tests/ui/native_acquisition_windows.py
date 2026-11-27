"""Required Windows acquisition controls; selected-package source and owned fixtures only."""
from concurrent.futures import ThreadPoolExecutor
import importlib.util
from pathlib import Path
import sys
import threading
import time

import pytest
import carr_library

_spec = importlib.util.spec_from_file_location('acquisition_native_fixtures',
    Path(__file__).with_name('test_native_acquisition.py'))
_fixtures = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixtures)
Library, isbn_preview_fixture, _load = _fixtures.Library, _fixtures.isbn_preview_fixture, _fixtures._load


@pytest.mark.parametrize('identifier_kind', ['doi', 'isbn'])
def test_windows_native_dialog_async_lookup_focus_and_close(tmp_path, identifier_kind):
    """No network: native event dispatch plus real worker/CallAfter completion."""
    assert sys.platform == 'win32', 'This required native gate must run on Windows.'
    import wx
    ui = Path(carr_library.__file__).resolve().parent / 'ui'
    module = _load(ui / 'acquisition_dialog.py', 'carr_library.ui._acquisition_native_probe')
    app = wx.GetApp() or wx.App(False)
    library = Library(tmp_path)
    if identifier_kind == 'isbn':
        library.preview = isbn_preview_fixture()
    failures = []

    class NativeHost(wx.Frame):
        def __init__(self):
            super().__init__(None, title='Synthetic DOI dialog probe')
            self.library, self.active, self.book = library, ('a', None), None
            self.working = self.busy = self._closing = False
            self.cancel_event = threading.Event()
            self.executor = ThreadPoolExecutor(max_workers=1)
            self.status = wx.TextCtrl(self)
        def _workspace(self): return self.active[0]
        def report(self, value): self.status.SetValue(value)
        def run_job(self, title, operation, done, failed):
            self.cancel_event.clear()
            self.working = self.busy = True
            def completed(future):
                def apply():
                    self.working = self.busy = False
                    try: value = future.result()
                    except Exception as exc: failed(exc)
                    else: done(value)
                wx.CallAfter(apply)
            self.executor.submit(operation).add_done_callback(completed)

    host = NativeHost()
    form = module.AcquisitionDialog(host, 'a')
    deadline = time.monotonic() + 8

    def finish_when_ready():
        try:
            if form.pending:
                if time.monotonic() >= deadline:
                    raise AssertionError('Native DOI completion exceeded 8 seconds.')
                wx.CallLater(10, finish_when_ready)
                return
            assert form.preview is library.preview
            assert 'Олена Коваль' in form.metadata_preview.GetValue()
            assert form.metadata_preview.GetName() == f'Complete {identifier_kind.upper()} metadata and provider provenance preview'
            assert wx.Window.FindFocus() is form.metadata_preview
            assert not form.rights.GetValue() and not form.download_button.IsEnabled()
            if identifier_kind == 'isbn':
                assert all(not control.IsEnabled() for control in form.providers.values())
                assert not form.email.IsEnabled()
        except BaseException as exc:
            failures.append(exc)
        finally:
            if not form.pending or failures:
                if failures:
                    form.cancel_operation()
                    form.EndModal(wx.ID_CANCEL)
                else:
                    form.close()

    def exercise():
        try:
            assert not form.online.GetValue() and library.calls == []
            if identifier_kind == 'isbn':
                form.identifier_kind.SetSelection(1)
                event = wx.CommandEvent(wx.EVT_CHOICE.typeId, form.identifier_kind.GetId())
                event.SetEventObject(form.identifier_kind)
                form.identifier_kind.GetEventHandler().ProcessEvent(event)
                assert not form.online.GetValue()
            form.doi.SetValue('9780306406157' if identifier_kind == 'isbn' else '10.1234/reading')
            form.online.SetValue(True)
            form.on_online_changed()
            event = wx.CommandEvent(wx.EVT_BUTTON.typeId, form.lookup_button.GetId())
            event.SetEventObject(form.lookup_button)
            form.lookup_button.GetEventHandler().ProcessEvent(event)
            assert form.pending
            wx.CallLater(10, finish_when_ready)
        except BaseException as exc:
            failures.append(exc)
            form.EndModal(wx.ID_CANCEL)

    try:
        host.Show()
        wx.CallAfter(exercise)
        assert form.ShowModal() == wx.ID_CANCEL
        if failures: raise failures[0]
    finally:
        form.dispose()
        host.executor.shutdown(wait=True, cancel_futures=True)
        form.Destroy()
        host.Destroy()
        app.ProcessPendingEvents()

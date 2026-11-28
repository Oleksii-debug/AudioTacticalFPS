"""Real wx controls and isolated queue execution; this does not run NVDA."""
from pathlib import Path
import ctypes
from ctypes import wintypes
import json
import os
import sys
from tempfile import TemporaryDirectory
import time
import threading
import traceback

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT)]
os.environ.update(ORT_DISABLE_TELEMETRY='1', HF_HUB_OFFLINE='1',
                  HF_HUB_DISABLE_TELEMETRY='1', TRANSFORMERS_OFFLINE='1', DO_NOT_TRACK='1')

import wx

from carr_library.application import LibraryApp
from carr_library.ui.desktop_app import DesktopFrame


def main():
    application = wx.App(False)
    application.SetExitOnFrameDelete(False)
    application.SetAssertMode(wx.APP_ASSERT_EXCEPTION)
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.IsWindow.argtypes, user32.IsWindow.restype = [wintypes.HWND], wintypes.BOOL
    errors, state, timers = [], {'live': True}, []
    started = time.monotonic()
    finished = threading.Event()
    def watchdog():
        if not finished.wait(120):
            print('FAIL: Import Center native cleanup-inclusive watchdog.', flush=True)
            os._exit(124)
    threading.Thread(target=watchdog, daemon=True).start()

    def later(delay, callback, *args):
        timers.append(wx.CallLater(delay, callback, *args))

    def wait_until(predicate, message, timeout=8):
        until = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() >= until:
                raise AssertionError(message)
            application.Yield()
            time.sleep(.01)
    with TemporaryDirectory(prefix='carr-import-center-') as directory:
        root = Path(directory)
        sources = root / 'selected'
        sources.mkdir()
        (sources / 'one.txt').write_text('Synthetic railway manuscript quotation.', encoding='utf-8')
        (sources / 'two.txt').write_text('Synthetic second academic source.', encoding='utf-8')
        library = LibraryApp(root / 'data')
        workspace = library.create_workspace('Import Center native acceptance')
        library.create_collection(workspace, 'Sources')
        frame = DesktopFrame(library)
        frame.Show()

        handle, executor, cancel_event = frame.GetHandle(), frame.executor, frame.cancel_event

        def fail():
            error = traceback.format_exc()
            errors.append(error)
            print('FAIL: Import Center native callback\n' + error, flush=True)
            cancel_event.set()
            record = state.get('modal')
            if record is not None:
                dialog, native_handle, loop = record
                def close_active():
                    if (dialog and user32.IsWindow(native_handle)
                            and dialog.IsModal() and loop.IsRunning()):
                        dialog.EndModal(wx.ID_CANCEL)
                wx.CallAfter(close_active)

        def poll(callback):
            if not state['live']:
                return
            try:
                if time.monotonic() - started > 90:
                    raise AssertionError('Import Center native acceptance exceeded its deadline.')
                if state['dialog'].busy:
                    later(50, poll, callback)
                else:
                    callback()
            except BaseException:
                fail()

        def imported():
            dialog = state['dialog']
            assert dialog.batch['counts']['complete'] == 1, dialog.status.GetValue()
            assert dialog.batch['counts']['excluded'] == 1
            assert len(library.browse(workspace)) == 1
            hit, = library.search(workspace, 'manuscript quotation')
            assert library.copy(workspace, hit.ref, hit.revision, 'quote') == 'manuscript quotation'
            reopened = LibraryApp(library.root)
            saved = reopened.import_batch(workspace, dialog.batch['batch_id'])
            assert saved['counts']['complete'] == 1
            assert saved['rows'][0]['request_id'] == state['request_id']
            dialog.status.SetFocus()
            wx.YieldIfNeeded()
            assert wx.Window.FindFocus() is dialog.status
            state['completed'] = True
            dialog.close()

        def planned():
            dialog = state['dialog']
            assert dialog.batch['counts']['queued'] == 2, dialog.status.GetValue()
            assert library.browse(workspace) == ()
            assert dialog.rows.GetCount() == 2
            dialog.rows.SetSelection(1)
            dialog.toggle_row()
            assert dialog.batch['counts']['excluded'] == 1
            state['request_id'] = dialog.batch['rows'][0]['request_id']
            dialog.run()
            assert dialog.busy
            dialog.close()
            assert not dialog.closed
            assert 'before closing' in dialog.status.GetValue()
            later(50, poll, imported)

        def begin():
            try:
                dialog, = [window for window in wx.GetTopLevelWindows()
                           if window.GetTitle() == 'Import Center' and window.GetParent() is frame]
                state['dialog'] = dialog
                loop = wx.EventLoopBase.GetActive()
                assert loop is not None and loop.IsRunning() and not loop.IsMain() and dialog.IsModal()
                state['modal'] = (dialog, dialog.GetHandle(), loop)
                assert dialog.batches.GetName() == 'Saved import batches'
                assert dialog.rows.GetName() == 'Files and import status'
                assert dialog.details.GetName() == 'Selected file details'
                assert dialog.status.GetName() == 'Import Center status'
                assert not dialog.cancel.IsEnabled()
                assert dialog.GetSizer().GetMinSize().width <= dialog.GetClientSize().width
                assert dialog.GetSizer().GetMinSize().height <= dialog.GetClientSize().height
                original_size = dialog.GetSize()
                compact = dialog.FromDIP(wx.Size(800, 500))
                dialog.SetSize(min(original_size.width, compact.width),
                               min(original_size.height, compact.height))
                dialog.Layout()
                dialog.content.FitInside()
                application.Yield()
                assert dialog.GetSizer().GetMinSize().width <= dialog.GetClientSize().width
                assert dialog.GetSizer().GetMinSize().height <= dialog.GetClientSize().height
                assert dialog.content.GetVirtualSize().height > dialog.content.GetClientSize().height
                def inside(container, control):
                    visible = wx.Rect(container.ClientToScreen(wx.Point(0, 0)), container.GetClientSize())
                    return visible.Contains(control.GetScreenRect())
                close = wx.FindWindowByName('Close Import Center', dialog)
                assert close is not None
                assert inside(dialog, dialog.status) and inside(dialog, dialog.cancel) and inside(dialog, close)
                scroll_positions = []
                for control in (dialog.mutable[-1], dialog.collection):
                    control.SetFocus()
                    wait_until(lambda: wx.Window.FindFocus() is control
                               and inside(dialog.content, control),
                               'Focused Import Center control was not scrolled into view.')
                    scroll_positions.append(dialog.content.GetViewStart()[1])
                assert scroll_positions[0] > scroll_positions[1] >= 0
                state['compact_layout'] = True
                print('PASS: compact viewport retains fixed actions and scrolls focused controls into view', flush=True)
                dialog.SetSize(original_size)
                dialog.Layout()
                dialog.content.FitInside()
                dialog.plan((sources,))
                assert dialog.busy and dialog.cancel.IsEnabled()
                later(50, poll, planned)
            except BaseException:
                fail()

        try:
            wx.CallAfter(begin)
            frame.import_center()
        finally:
            state['live'] = False
            for timer in timers:
                timer.Stop()
            cancel_event.set()
            try:
                if user32.IsWindow(handle):
                    frame.Close(force=True)
                wait_until(lambda: not user32.IsWindow(handle),
                           'Actual host Close did not release its native owners.', timeout=20)
            finally:
                executor.shutdown(wait=True, cancel_futures=True)
            assert not user32.IsWindow(handle) and not executor.cleanup_failures
            application.ProcessPendingEvents()
        if errors:
            raise AssertionError('\n'.join(errors))
        assert state.get('completed'), 'Import Center did not complete the native workflow.'
        assert state.get('compact_layout'), 'The native compact layout was not exercised.'
        report = {'status': 'PASS', 'native_controls': 'PASS',
                  'planned_import_search_reopen': 'PASS', 'compact_layout': 'PASS',
                  'native_host_cleanup': 'PASS', 'nvda': 'NOT_RUN'}
    finished.set()
    print(json.dumps(report), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

"""Native Import Center controls + actual owned large-profile import completion.

The synthetic input is supplied to the real handler after native controls are
shown. This qualifies neither the OS file picker nor manual NVDA operation.
No import service, worker, progress observer or source reader is replaced.
"""
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import traceback


def run_probe(wx):
    from carr_library.application import LibraryApp
    from carr_library.ui.desktop_app import DesktopFrame
    from carr_library.ui.import_center import show_import_center
    from carr_library.ingest.worker import load_request

    failures, report, timers = [], {}, []
    state = dict(dialog=None, returned=False, destroyed=False, started=False)
    frame = None

    def fail(message):
        failures.append(message)
        if frame is not None:
            frame.cancel_event.set()
        for window in reversed(wx.GetTopLevelWindows()):
            if isinstance(window, wx.Dialog) and window.IsModal():
                window.EndModal(wx.ID_CANCEL)
                break
        loop = gui.GetMainLoop()
        if loop is not None:
            loop.ScheduleExit(1)

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            fail(traceback.format_exc())
            return False

        def OnEventLoopExit(self, loop):
            if failures and not loop.IsMain():
                wx.CallAfter(fail, 'A nested native dialog prevented large-profile import completion.')
            super().OnEventLoopExit(loop)

    gui = ProbeApp(False)
    with TemporaryDirectory(prefix='large-profile-native-') as directory:
        root = Path(directory)
        library = LibraryApp(root / 'library Україна')
        workspace = library.create_workspace('Native large profile')
        source = root / 'Original.txt'
        original = ('Exact native source\r\n' + 'x' * 65536 + '\fДругий фрагмент').encode('utf-8')
        source.write_bytes(original)
        frame = DesktopFrame(library)
        if frame.active != (workspace, None):
            frame._open_workspace(workspace)
        frame.SetSize((1250, 1000))
        frame.Show()

        def guarded(callback):
            if failures:
                return
            try:
                callback()
            except BaseException:
                fail(traceback.format_exc())

        def later(callback, delay=35):
            timers.append(wx.CallLater(delay, lambda: guarded(callback)))

        def open_center():
            show_import_center(frame, workspace)
            state['returned'] = True

        def ready():
            choices = [window for window in wx.GetTopLevelWindows()
                       if isinstance(window, wx.Dialog) and window.GetTitle() == 'Import Center'
                       and window.GetParent() is frame and window.IsShownOnScreen() and window.IsModal()]
            if not choices:
                later(ready)
                return
            assert len(choices) == 1
            dialog = choices[0]
            state['dialog'] = dialog
            identifier = dialog.GetId()
            def destroyed(event):
                if event.GetId() == identifier:
                    state['destroyed'] = True
                event.Skip()
            dialog.Bind(wx.EVT_WINDOW_DESTROY, destroyed)
            dialog.SetSize((1100, 1050))
            assert dialog.large_mib.GetName() == 'Maximum large-original size in MiB'
            dialog.large_kind.SetSelection(0)
            dialog.large_timeout.SetValue(90)
            dialog.large_mib.SetValue(2)
            dialog.large_mib.SetFocus()
            later(focused)

        def focused():
            dialog = state['dialog']
            focus = wx.Window.FindFocus()
            assert focus is dialog.large_mib or dialog.large_mib.IsDescendant(focus)
            assert dialog.large_mib.GetValue() == 2
            simulator = wx.UIActionSimulator()
            simulator.Char(wx.WXK_TAB)
            later(deadline_focused)

        def deadline_focused():
            dialog = state['dialog']
            focus = wx.Window.FindFocus()
            assert focus is dialog.large_timeout or dialog.large_timeout.IsDescendant(focus)
            assert dialog.large_timeout.GetValue() == 90
            simulator = wx.UIActionSimulator()
            simulator.Char(wx.WXK_TAB)
            later(begin)

        def begin():
            dialog = state['dialog']
            focus = wx.Window.FindFocus()
            assert focus.GetName() == 'Import one explicitly selected large TXT or PDF original'
            assert focus.IsShownOnScreen() and focus.IsEnabled()
            dialog.import_large_source(source)
            state['started'] = True
            assert dialog.busy and frame.working and dialog.cancel.IsEnabled()
            assert not dialog.large_mib.IsEnabled()
            later(settled)

        def settled():
            dialog = state['dialog']
            if dialog.busy or frame.working:
                later(settled)
                return
            assert '3 exact unpaginated logical fragments' in dialog.status.GetValue(), dialog.status.GetValue()
            assert dialog.large_mib.IsEnabled() and not dialog.cancel.IsEnabled()
            requests = library.import_requests(workspace)
            assert len(requests) == 1 and requests[0]['state'] == 'complete'
            record = load_request(library.root, requests[0]['request_id'])
            assert record['version'] == 3
            assert record['source_profile']['maximum_bytes'] == 2 * 1024**2
            result = record['result']
            hit = library.search(workspace, 'Exact native source')[0]
            assert hit.verified and library.copy(workspace, hit.ref, hit.revision, 'quote') == 'Exact native source'
            assert sha256(source.read_bytes()).hexdigest() == result['asset']
            assert source.read_bytes() == original
            button = wx.Window.FindWindowById(wx.ID_CANCEL, dialog)
            assert button is not None and button.GetParent() is dialog
            event = wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId())
            event.SetEventObject(button)
            assert button.GetEventHandler().ProcessEvent(event)
            later(closed)

        def closed():
            if not state['returned'] or not state['destroyed']:
                later(closed)
                return
            report.update(status='PASS', exact_fragments=3,
                checks=['native named numeric profile control', 'native focus and Tab to import action',
                        'real existing frame worker and owned import child', 'controls disabled until settlement',
                        'captured byte allowance', 'canonical request and completed result',
                        'original-byte verified quotation', 'unchanged original', 'owned native close and destruction'],
                file_picker='NOT_RUN', manual_nvda='NOT_RUN')
            frame.Close()

        watchdog = wx.CallLater(90000, lambda: fail('Native large-profile import probe exceeded 90 seconds.'))
        later(open_center, 80)
        later(ready, 100)
        try:
            gui.MainLoop()
        finally:
            watchdog.Stop()
            for timer in timers:
                timer.Stop()
            frame.cancel_event.set()
            frame.executor.shutdown(wait=True, cancel_futures=True)
            if frame:
                frame.Destroy()
                gui.ProcessPendingEvents()
    if failures or report.get('status') != 'PASS':
        print(json.dumps(dict(status='FAIL', errors=failures or ['Probe did not finish.']), ensure_ascii=False))
        return 1
    print(json.dumps(report, ensure_ascii=False))
    return 0


def main():
    from carr_library.native_runtime import activate_bundled_runtime
    handle = activate_bundled_runtime()
    try:
        if sys.platform.startswith('linux') and not os.environ.get('DISPLAY'):
            print(json.dumps(dict(status='NOT_RUN', reason='A native desktop is required.')))
            return 2
        import wx
        return run_probe(wx)
    finally:
        if handle is not None:
            handle.close()


if __name__ == '__main__':
    raise SystemExit(main())

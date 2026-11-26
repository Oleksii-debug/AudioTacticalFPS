"""Real wx desktop source-opening smoke; run with a native display or Xvfb.

From carr-library: PYTHONPATH=src:. python tests/ui/probe_source_open_native.py
Only the external-viewer callback is replaced. SQLite, imports, search, source
preparation, controls, event bindings and the wx event loop are real. This is
not Windows/NVDA acceptance, and it launches no external viewer.
"""
from hashlib import sha256
import json
from pathlib import Path
import platform
from tempfile import TemporaryDirectory
import traceback

import wx

from carr_library.application import LibraryApp
from carr_library.reader import source_file
from carr_library.ui import desktop_app


def main():
    failures = []
    report = {}

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            failures.append(traceback.format_exc())
            self.ExitMainLoop()
            return False

    gui = ProbeApp(False)
    with TemporaryDirectory(prefix='research-source-open-native-') as temporary:
        root = Path(temporary)
        library = LibraryApp(root / 'private library')
        workspace = library.create_workspace('Native source probe')
        selected_bytes = 'Selected railway original.\r\nУкраїнський точний текст.\n'.encode('utf-8')
        other_bytes = b'Another railway source, with different original bytes.\n'
        for name, title, raw in [('selected.txt', 'Selected source', selected_bytes),
                                 ('other.txt', 'Other source', other_bytes)]:
            path = root / name
            path.write_bytes(raw)
            imported = library.import_file(workspace, path, title=title, authors=('Synthetic author',))
            assert imported['state'] == 'complete', imported

        frame = desktop_app.DesktopFrame(library)
        assert frame.book is not None, frame.status.GetValue()
        assert frame.active == (workspace, None)
        panel = frame.book.GetPage(0)
        frame.SetSize((1100, 950))
        frame.Show()
        frame.Raise()
        calls = []
        original_viewer = wx.LaunchDefaultApplication

        def record_viewer(path):
            destination = Path(path)
            calls.append((destination, destination.read_bytes()))
            return True

        wx.LaunchDefaultApplication = record_viewer

        def guarded(callback):
            try:
                callback()
            except BaseException:
                failures.append(traceback.format_exc())
                gui.ExitMainLoop()

        def later(callback, milliseconds=20):
            wx.CallLater(milliseconds, lambda: guarded(callback))

        def click(button):
            assert button.IsShownOnScreen() and button.IsEnabled()
            event = wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId())
            event.SetEventObject(button)
            assert button.GetEventHandler().ProcessEvent(event), 'Native button event was not handled.'

        def assert_busy():
            assert frame.working and frame.busy
            assert frame.cancel_button.IsEnabled()
            assert not frame.choice.IsEnabled()
            assert not frame.book.IsEnabled()

        def assert_ready():
            assert not frame.working and not frame.busy
            assert not frame.cancel_button.IsEnabled()
            assert frame.choice.IsEnabled()
            assert frame.book.IsEnabled()
            assert all(frame.menu_bar.IsEnabledTop(i) for i in range(frame.menu_bar.GetMenuCount()))

        def begin_search():
            assert not calls
            panel.query.SetValue('railway')
            panel.retrieval.SetSelection(0)
            click(panel.go)
            assert_busy()
            later(search_finished)

        def search_finished():
            if frame.working:
                later(search_finished)
                return
            assert_ready()
            assert panel.model.row_count() == 2, (frame.status.GetValue(), panel.status.GetValue())
            assert not calls, 'Searching unexpectedly launched an external viewer.'
            row = next(i for i, hit in enumerate(panel.model.hits) if hit.title == 'Selected source')
            panel.grid.select_rows([row])
            panel.grid.focus_row(row)
            selected = panel.selected()
            assert selected.workspace == workspace and selected.title == 'Selected source'
            buttons = [child for child in panel.GetChildren() if isinstance(child, wx.Button)
                       and child.GetLabel().replace('&', '') == 'Open original source file']
            assert len(buttons) == 1, 'Exactly one original-source action should be available.'
            click(buttons[0])
            assert_busy()
            later(original_finished)

        def original_finished():
            if frame.working:
                later(original_finished)
                return
            assert_ready()
            assert len(calls) == 1, (frame.status.GetValue(), panel.status.GetValue())
            viewed, raw = calls[0]
            digest = sha256(selected_bytes).hexdigest()
            retained = library.assets_dir / (digest + '.txt')
            assert viewed == library.root / 'source-views' / (digest + '.txt')
            assert raw == selected_bytes
            assert retained.read_bytes() == selected_bytes
            assert (root / 'selected.txt').read_bytes() == selected_bytes
            assert not viewed.samefile(retained), 'External viewer received the retained original inode.'
            assert panel.selected().title == 'Selected source'
            status = panel.status.GetValue()
            assert 'Opened a viewing copy of Selected source.' in status, status
            assert 'Source location: Segment 1' in status, status
            assert 'retained original is unchanged' in status, status
            report['status_text'] = status
            later(check_focus, milliseconds=80)

        def check_focus():
            assert wx.Window.FindFocus() is panel.status, 'Focus did not return to readable search status.'
            assert panel.status.GetName() == 'Search status'
            report.update(status='PASS', platform=platform.system(), wx_version=wx.version(),
                checks=['actual TXT imports', 'native search event', 'exact selected result',
                        'native original-source event', 'verified viewing-copy bytes',
                        'retained original unchanged', 'controls restored', 'search-status focus'],
                external_viewer='recording callback only', windows_nvda='NOT_RUN',
                desktop_app_sha256=sha256(Path(desktop_app.__file__).read_bytes()).hexdigest(),
                source_file_sha256=sha256(Path(source_file.__file__).read_bytes()).hexdigest())
            frame.Close()

        def timed_out():
            failures.append('Native probe exceeded 20 seconds.')
            gui.ExitMainLoop()

        timeout = wx.CallLater(20000, timed_out)
        later(begin_search, milliseconds=80)
        try:
            gui.MainLoop()
        finally:
            timeout.Stop()
            wx.LaunchDefaultApplication = original_viewer
            frame.executor.shutdown(wait=True, cancel_futures=True)
            if frame:
                frame.Destroy()
                gui.ProcessPendingEvents()

    if failures:
        print(json.dumps({'status': 'FAIL', 'errors': failures, 'windows_nvda': 'NOT_RUN'}, ensure_ascii=False))
        return 1
    assert report.get('status') == 'PASS', 'Native probe exited without completing the action.'
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

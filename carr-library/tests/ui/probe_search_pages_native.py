"""Actual installed desktop paging, worker jobs, focus and exact quotation copy.

Uses synthetic retained originals and the real DesktopSearchPanel. No service,
control or worker is replaced. This does not qualify manual NVDA behavior.
"""
from hashlib import sha256
import json
import os
from pathlib import Path
import platform
import sys
from tempfile import TemporaryDirectory
import traceback


def run_probe(wx, native_active):
    from carr_library.application import LibraryApp
    from carr_library.ui import desktop_app, search_paging

    failures, report, timers = [], {}, []
    frame = None

    def fail(message):
        failures.append(message)
        if frame is not None:
            frame.cancel_event.set()
        loop = gui.GetMainLoop()
        if loop is not None:
            loop.ScheduleExit(1)
        for window in reversed(wx.GetTopLevelWindows()):
            if isinstance(window, wx.Dialog) and window.IsModal():
                window.EndModal(wx.ID_CANCEL)
                break

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            fail(traceback.format_exc())
            return False

        def OnEventLoopExit(self, loop):
            if failures and not loop.IsMain():
                wx.CallAfter(fail, 'A nested native dialog prevented paging completion.')
            super().OnEventLoopExit(loop)

    gui = ProbeApp(False)
    with TemporaryDirectory(prefix='research-search-pages-native-') as temporary:
        library = LibraryApp(Path(temporary) / 'private library Україна')
        workspace = library.create_workspace('Selected paging source')
        other = library.create_workspace('Foreign scope')
        texts = tuple(f'Railway source segment {number:04d} — Україна.' for number in range(205))
        assert library.add_text(workspace, 'Immutable paged original', '\f'.join(texts))['state'] == 'complete'
        assert library.add_text(other, 'Must not appear', 'Railway foreign source.')['state'] == 'complete'
        original_hashes = {path.name: sha256(path.read_bytes()).hexdigest()
                           for path in library.assets_dir.glob('*.txt')}
        frame = desktop_app.DesktopFrame(library)
        if frame.active != (workspace, None):
            frame._open_workspace(workspace)
        assert frame.active == (workspace, None) and frame.book is not None
        panel = frame.book.GetPage(0)
        frame.SetSize((1250, 1050))
        frame.Show()
        frame.Raise()
        simulator = wx.UIActionSimulator()
        seen, pages = {}, {}

        def guarded(callback):
            if failures:
                return
            try:
                callback()
            except BaseException:
                fail(traceback.format_exc())

        def later(callback, milliseconds=35):
            timers.append(wx.CallLater(milliseconds, lambda: guarded(callback)))

        def click(button):
            assert button.IsShownOnScreen() and button.IsEnabled(), button.GetName()
            event = wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId())
            event.SetEventObject(button)
            assert button.GetEventHandler().ProcessEvent(event), button.GetLabel()

        def busy():
            assert frame.working and frame.busy
            assert frame.cancel_button.IsEnabled()
            assert not frame.book.IsEnabled()
            assert not panel.previous_search_page.IsEnabled()
            assert not panel.next_search_page.IsEnabled()

        def ready():
            assert not frame.working and not frame.busy
            assert not frame.cancel_button.IsEnabled()
            assert frame.book.IsEnabled() and frame.choice.IsEnabled()

        def settled(callback):
            if frame.working:
                later(lambda: settled(callback))
                return
            ready()
            callback()

        def rows():
            hits = tuple(panel.model.hits)
            assert all(hit.workspace == workspace for hit in hits)
            assert all(panel.results[hit.ref].verified for hit in hits)
            return tuple(hit.quote for hit in hits)

        def begin():
            for control, name in ((panel.previous_search_page, 'Previous search page'),
                                  (panel.next_search_page, 'Next search page')):
                assert control.GetName() == name and control.AcceptsFocusFromKeyboard()
                assert not control.IsEnabled()
            panel.query.SetValue('Railway')
            panel.retrieval.SetSelection(0)
            panel.result_mode.SetSelection(0)
            click(panel.go)
            busy()
            later(lambda: settled(first_done))

        def first_done():
            first = rows()
            assert len(first) == 100, (len(first), frame.status.GetValue(), panel.status.GetValue())
            assert panel._navigation().number == 1
            assert not panel.previous_search_page.IsEnabled() and panel.next_search_page.IsEnabled()
            assert 'More sources may remain' in panel.status.GetValue()
            pages[1] = first
            click(panel.next_search_page)
            busy()
            later(lambda: settled(second_done))

        def second_done():
            second = rows()
            assert len(second) == 100 and not set(second).intersection(pages[1])
            assert panel._navigation().number == 2
            assert panel.previous_search_page.IsEnabled() and panel.next_search_page.IsEnabled()
            pages[2] = second
            panel.previous_search_page.SetFocus()
            later(tab_next, 80)

        def tab_next():
            assert wx.Window.FindFocus() is panel.previous_search_page
            assert simulator.KeyDown(wx.WXK_TAB)
            assert simulator.KeyUp(wx.WXK_TAB)
            later(tab_next_checked, 80)

        def tab_next_checked():
            assert wx.Window.FindFocus() is panel.next_search_page
            assert simulator.KeyDown(wx.WXK_SHIFT)
            try:
                assert simulator.KeyDown(wx.WXK_TAB)
                assert simulator.KeyUp(wx.WXK_TAB)
            finally:
                simulator.KeyUp(wx.WXK_SHIFT)
            later(tab_back_checked, 80)

        def tab_back_checked():
            assert wx.Window.FindFocus() is panel.previous_search_page
            click(panel.previous_search_page)
            busy()
            later(lambda: settled(previous_done))

        def previous_done():
            assert rows() == pages[1] and panel._navigation().number == 1
            assert not panel.previous_search_page.IsEnabled()
            click(panel.next_search_page)
            busy()
            later(lambda: settled(second_again))

        def second_again():
            assert rows() == pages[2] and panel._navigation().number == 2
            click(panel.next_search_page)
            busy()
            later(lambda: settled(last_done))

        def last_done():
            pages[3] = rows()
            assert len(pages[3]) == 5
            assert panel._navigation().number == 3
            assert not panel.next_search_page.IsEnabled()
            assert panel.previous_search_page.IsEnabled()
            assert 'selected source search is complete' in panel.status.GetValue()
            combined = tuple(quote for number in (1, 2, 3) for quote in pages[number])
            assert len(combined) == len(set(combined)) == 205
            assert set(combined) == set(texts)
            selected = panel.selected()
            assert selected.quote in texts
            seen['copied'] = selected.quote
            buttons = [child for child in panel.GetChildren()
                       if isinstance(child, wx.Button)
                       and child.GetLabel().replace('&', '') == 'Copy quote']
            assert len(buttons) == 1, 'Exactly one local source Copy quote control is required.'
            click(buttons[0])
            busy()
            later(lambda: settled(copy_done))

        def copy_done():
            assert panel.status.GetValue().startswith('Copied quote'), panel.status.GetValue()
            assert wx.TheClipboard.Open(), 'The synthetic clipboard could not be opened.'
            try:
                copied = wx.TextDataObject()
                assert wx.TheClipboard.GetData(copied)
                assert copied.GetText() == seen['copied']
            finally:
                wx.TheClipboard.Close()
            panel.query.SetValue('changed query')
            later(options_changed, 60)

        def options_changed():
            assert panel._navigation().number == 0
            assert not panel.results and not panel.model.hits
            assert not panel.next_search_page.IsEnabled() and not panel.previous_search_page.IsEnabled()
            assert original_hashes == {path.name: sha256(path.read_bytes()).hexdigest()
                                       for path in library.assets_dir.glob('*.txt')}
            assert panel.status.GetName() == 'Search status' and panel.status.AcceptsFocusFromKeyboard()
            report.update(status='PASS', platform=platform.system(), wx_version=wx.version(),
                native_runtime_active=native_active, windows_nvda='NOT_RUN',
                retained_source_segments=205, displayed_pages=[100, 100, 5],
                checks=['actual retained TXT import', 'foreign workspace excluded',
                        'real asynchronous first/next/previous/final page jobs',
                        'previous returns the same exact source excerpts',
                        'all 205 source segments reachable without overlap',
                        'named focusable page controls', 'native Tab and Shift+Tab',
                        'controls disabled during real jobs and restored afterwards',
                        'final exhaustion is explicit', 'actual verified quotation clipboard',
                        'query edit withdraws obsolete continuation', 'original hashes unchanged'],
                modules_sha256={name: sha256(Path(module.__file__).read_bytes()).hexdigest()
                    for name, module in (('desktop_app', desktop_app), ('search_paging', search_paging))})
            frame.Close()

        timeout = wx.CallLater(60000, lambda: fail('Native search-page probe exceeded 60 seconds.'))
        later(begin, 100)
        try:
            gui.MainLoop()
        finally:
            timeout.Stop()
            for timer in timers:
                timer.Stop()
            frame.cancel_event.set()
            frame.executor.shutdown(wait=True, cancel_futures=True)
            if frame:
                frame.Destroy()
                gui.ProcessPendingEvents()

    if failures or report.get('status') != 'PASS':
        print(json.dumps(dict(status='FAIL', errors=failures or ['Probe did not finish.'],
                              windows_nvda='NOT_RUN'), ensure_ascii=False))
        return 1
    print(json.dumps(report, ensure_ascii=False))
    return 0


def main():
    from carr_library.native_runtime import activate_bundled_runtime, _discover
    handle = activate_bundled_runtime()
    try:
        bundle = _discover()
        if sys.platform == 'win32' and bundle is not None:
            manifest = json.loads((bundle / 'bundle-manifest.json').read_text(encoding='utf-8'))
            if manifest.get('bundled_native_runtime') is True:
                assert handle is not None, 'The declared bundled Windows runtime was not activated.'
        if sys.platform.startswith('linux') and not os.environ.get('DISPLAY'):
            print(json.dumps(dict(status='NOT_RUN', reason='A native display or Xvfb is required.',
                                  windows_nvda='NOT_RUN')))
            return 2
        import wx
        return run_probe(wx, handle is not None)
    finally:
        if handle is not None:
            handle.close()


if __name__ == '__main__':
    raise SystemExit(main())

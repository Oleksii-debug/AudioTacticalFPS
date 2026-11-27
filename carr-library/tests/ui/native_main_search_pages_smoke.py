"""Installed Windows Main Search navigation using actual wx controls and jobs.

The sources are synthetic. Space activates real named Previous/Next buttons.
Copy writes only the selected synthetic quotation to the test clipboard.
This is automated native acceptance, not manual NVDA or semantic-model quality.
"""
import argparse
from importlib import metadata
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
import traceback


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--native', action='store_true', required=True)
    parser.add_argument('--timeout', type=float, default=120)
    options = parser.parse_args(argv)
    if not 1 <= options.timeout <= 300:
        raise ValueError('Use a bounded native acceptance timeout.')
    if os.name != 'nt':
        print(json.dumps({'status': 'NOT_RUN', 'reason': 'Actual Windows wx is required.', 'nvda': 'NOT_RUN'}))
        return 77
    try:
        import wx
    except ImportError:
        print(json.dumps({'status': 'NOT_RUN', 'reason': 'Installed wxPython is required.', 'nvda': 'NOT_RUN'}))
        return 77
    from carr_library.application import LibraryApp
    import carr_library.ui.desktop_app as desktop

    installed = Path(metadata.distribution('universal-research-library').locate_file('carr_library')).resolve()
    assert Path(desktop.__file__).resolve().is_relative_to(installed), 'Desktop loaded outside the installed wheel.'
    checks, failures = [], []
    frame = None
    deadline = monotonic() + options.timeout

    def fail(message):
        failures.append(str(message))
        if frame is not None:
            frame.cancel_event.set()

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            fail(traceback.format_exc())
            return False

    application = ProbeApp(False)
    loop = wx.GUIEventLoop()
    activation = wx.EventLoopActivator(loop)
    watchdog = wx.CallLater(int(options.timeout * 1000), fail, 'Main search pagination deadline exceeded.')

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)
        print('PASS:', name, flush=True)

    def pump(predicate):
        while monotonic() < deadline:
            application.Yield()
            if failures:
                raise AssertionError('; '.join(failures))
            if predicate():
                return
            wx.MilliSleep(10)
        raise TimeoutError('Main search pagination deadline exceeded.')

    def click(button):
        assert button.IsEnabled(), button.GetName()
        event = wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId())
        event.SetEventObject(button)
        button.ProcessWindowEvent(event)

    def key_space(button, panel):
        assert button.IsEnabled() and button.IsShownOnScreen(), button.GetName()
        epoch = panel._context_epoch
        button.SetFocus()
        pump(lambda: wx.Window.FindFocus() is button)
        simulator = wx.UIActionSimulator()
        assert simulator.KeyDown(wx.WXK_SPACE)
        assert simulator.KeyUp(wx.WXK_SPACE)
        pump(lambda: panel._context_epoch != epoch and not frame.working)

    try:
        with TemporaryDirectory(prefix='carr-native-main-search-') as temporary:
            try:
                library = LibraryApp(Path(temporary) / 'Synthetic library')
                workspace = library.create_workspace('Native Main search')
                topic = library.create_topic(workspace, 'Result-page source evidence')
                original_text = '\f'.join(
                    f'Original needle passage number {number:03d}. Україна.' for number in range(101))
                imported = library.add_text(workspace, 'Synthetic pagination source', original_text)
                original = library.assets.resolve(imported['asset'])
                original_bytes = original.read_bytes()
                frame = desktop.DesktopFrame(library)
                application.SetTopWindow(frame)
                frame._open_workspace(workspace, topic)
                frame.SetSize((1100, 980))
                frame.Show()
                frame.Raise()
                pump(lambda: frame.active == (workspace, topic) and not frame.working)
                panel = frame.book.GetPage(0)
                check('Main has named native Previous and Next result controls',
                    isinstance(panel.previous_result_page, wx.Button)
                    and isinstance(panel.next_result_page, wx.Button)
                    and panel.previous_result_page.GetName() == 'Previous result page'
                    and panel.next_result_page.GetName() == 'Next result page')
                check('navigation is unavailable before a completed search',
                    not panel.previous_result_page.IsEnabled() and not panel.next_result_page.IsEnabled())
                panel.query.SetValue('needle')
                panel.retrieval.SetSelection(0)
                panel.result_mode.SetSelection(0)
                panel.on_options_changed()
                click(panel.go)
                pump(lambda: not frame.working)
                first = tuple(panel.model.hits)
                check('real Main search publishes the first 100 original results',
                    len(first) == 100 and panel.next_result_page.IsEnabled()
                    and not panel.previous_result_page.IsEnabled())
                key_space(panel.next_result_page, panel)
                last = tuple(panel.model.hits)
                check('Space on Next reaches the previously hidden 101st result',
                    len(last) == 1 and panel.previous_result_page.IsEnabled()
                    and not panel.next_result_page.IsEnabled())
                check('page status distinguishes source window and actual exhaustion',
                    'Source editions ' in panel.status.GetValue()
                    and 'No further matching result page' in panel.status.GetValue())
                try:
                    library.copy(first[0].workspace, first[0].ref, first[0].revision, 'quote')
                except ValueError:
                    rejected = True
                else:
                    rejected = False
                check('successful navigation withdraws the preceding source handles', rejected)
                panel.copy('quote')
                pump(lambda: not frame.working)
                assert wx.TheClipboard.Open()
                try:
                    copied = wx.TextDataObject()
                    check('native copy handler still verifies the accepted page',
                        wx.TheClipboard.GetData(copied) and copied.GetText() == last[0].quote)
                finally:
                    wx.TheClipboard.Close()
                panel.save_selected(None)
                pump(lambda: not frame.working)
                check('accepted-page source can still be saved to the bound topic',
                    len(library.evidence(workspace, topic)) == 1
                    and last[0].quote in library.export_topic(workspace, topic))
                key_space(panel.previous_result_page, panel)
                previous = tuple(panel.model.hits)
                check('Space on Previous rereads the prior page with fresh handles',
                    {row.quote for row in previous} == {row.quote for row in first}
                    and not ({row.ref for row in previous} & {row.ref for row in first})
                    and panel.next_result_page.IsEnabled()
                    and not panel.previous_result_page.IsEnabled())
                panel.query.SetValue('different')
                check('editing the query clears the old sequence and action selection',
                    not panel.model.hits and not panel.results
                    and not panel.previous_result_page.IsEnabled()
                    and not panel.next_result_page.IsEnabled())
                check('navigation, copy and save preserve original source bytes',
                    original.read_bytes() == original_bytes)
            finally:
                if frame is not None:
                    frame._closing = True
                    frame.cancel_event.set()
                    frame.executor.shutdown(wait=True, cancel_futures=True)
                    frame.Destroy()
                    application.Yield()
                    frame = None
        print(json.dumps({'status': 'PASS', 'checks': len(checks),
                          'native_keyboard_navigation': 'PASS', 'actual_source_services': 'PASS',
                          'semantic_model_quality': 'NOT_RUN', 'nvda': 'NOT_RUN'}))
        return 0
    except BaseException as exc:
        print(json.dumps({'status': 'FAIL', 'checks_passed': len(checks),
                          'error': str(exc), 'nvda': 'NOT_RUN'}))
        return 1
    finally:
        watchdog.Stop()
        del activation
        application.Destroy()


if __name__ == '__main__':
    raise SystemExit(main())

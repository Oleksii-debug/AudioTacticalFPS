"""Real installed desktop/Search Center continuation, keyboard and source copy.

Only synthetic local sources are created. Native controls, worker completion,
SQLite, retained-source checks and clipboard are real. No NVDA result is claimed.
Run: <installed-python> -I -B -X utf8 <this-file> --native
"""
from argparse import ArgumentParser
from collections import Counter
from hashlib import sha256
import json
import os
from pathlib import Path
import platform
import sys
from tempfile import TemporaryDirectory
import time
import traceback


EXPECTED_CHECKS = (
    'desktop-navigation-accessible-and-initially-disabled',
    'desktop-first-page-bounded-100',
    'desktop-first-page-accessible-counts',
    'desktop-next-native-key-reaches-final-21',
    'desktop-forward-results-have-no-overlap',
    'desktop-old-page-source-handle-withdrawn',
    'desktop-previous-native-key-reruns-first-page',
    'desktop-previous-allocates-fresh-source-handles',
    'desktop-native-copy-rechecks-exact-source',
    'center-native-space-selects-explicit-second-library',
    'center-navigation-accessible',
    'center-first-page-bounded-10',
    'center-accessible-counts-and-batch-order',
    'center-next-native-key-reaches-page-two',
    'center-next-results-have-no-overlap',
    'center-previous-native-key-reruns-first-page',
    'center-old-page-source-handle-withdrawn',
    'center-native-copy-rechecks-exact-source',
    'center-query-change-withdraws-results-and-navigation',
    'center-escape-preserves-host-library',
    'synthetic-original-bytes-unchanged',
)


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--native', action='store_true')
    args = parser.parse_args()
    if not args.native:
        parser.error('Choose --native to run the actual controls and keyboard.')
    if sys.platform not in ('win32', 'darwin') and not (
            os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')):
        print(json.dumps({'result': 'NOT_RUN', 'reason': 'No native display.', 'nvda': 'NOT_RUN'}))
        return 77
    try:
        import wx
        from carr_library.application import LibraryApp
        from carr_library.ui.desktop_app import DesktopFrame
        from carr_library.ui.federated_search import NativeSearchCenter
        import carr_library.application as application
        import carr_library.search.pagination as pagination
        import carr_library.ui.search_paging as search_paging
        import carr_library.ui.federated_search as federated_search
    except (ImportError, OSError) as exc:
        print(json.dumps({'result': 'NOT_RUN', 'reason': str(exc), 'nvda': 'NOT_RUN'}))
        return 77

    checks, failures, timers = [], [], []
    frame = form = None
    deadline = time.monotonic() + 150.0

    def abort():
        if frame is not None:
            frame.cancel_event.set()
        if form is not None and form._window_alive and form.window.IsModal():
            form.window.EndModal(wx.ID_CANCEL)
        gui.ExitMainLoop()

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            failures.append(traceback.format_exc())
            abort()
            return False

    try:
        gui = ProbeApp(False)
    except (Exception, SystemExit) as exc:
        print(json.dumps({'result': 'NOT_RUN', 'reason': str(exc), 'nvda': 'NOT_RUN'}))
        return 77
    simulator = wx.UIActionSimulator()

    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        checks.append(label)
        print('PASS:', label, flush=True)

    def guarded(callback):
        if failures:
            return
        try:
            if time.monotonic() >= deadline:
                raise TimeoutError('Native continuation probe exceeded its 150-second deadline.')
            callback()
        except BaseException:
            failures.append(traceback.format_exc())
            abort()

    def later(callback, delay=40):
        timers.append(wx.CallLater(delay, lambda: guarded(callback)))

    def await_page(predicate, callback):
        if predicate():
            callback()
        else:
            later(lambda: await_page(predicate, callback))

    def press(control, key=wx.WXK_SPACE):
        assert control.IsShownOnScreen() and control.IsEnabled(), control.GetName()
        control.SetFocus()
        assert wx.Window.FindFocus() is control
        assert simulator.KeyDown(key)
        assert simulator.KeyUp(key)

    def clipboard():
        value = wx.TextDataObject()
        assert wx.TheClipboard.Open()
        try:
            assert wx.TheClipboard.GetData(value)
        finally:
            wx.TheClipboard.Close()
        return value.GetText()

    def expect_stale(action):
        try:
            action()
        except ValueError:
            return True
        raise AssertionError('An old result handle retained source authority.')

    with TemporaryDirectory(prefix='carr-search-continuation-native-') as temporary:
        library = LibraryApp(Path(temporary) / 'private')
        workspace = library.create_workspace('1 Synthetic continuation library')
        other = library.create_workspace('2 Explicit second library')
        source = '\f'.join(f'Needle synthetic page {number:03d}.' for number in range(1, 122))
        imported = library.add_text(workspace, 'Synthetic 121-page source', source,
                                    authors=('Synthetic researcher',))
        second = library.add_text(other, 'Second synthetic source', 'Needle in the second library.')
        originals = {identity: sha256(library.assets.resolve(identity).read_bytes()).hexdigest()
                     for identity in (imported['asset'], second['asset'])}
        frame = DesktopFrame(library)
        frame._open_workspace(workspace)
        frame.SetSize((1150, 1000))
        frame.Show()
        frame.Raise()
        panel = frame.book.GetPage(0)
        snapshots = {}

        def begin():
            check(EXPECTED_CHECKS[0],
                  panel.previous_page_button.GetName() == 'Previous results page'
                  and panel.next_page_button.GetName() == 'Next results page'
                  and panel.previous_page_button.AcceptsFocusFromKeyboard()
                  and panel.next_page_button.AcceptsFocusFromKeyboard()
                  and not panel.previous_page_button.IsEnabled()
                  and not panel.next_page_button.IsEnabled())
            panel.query.SetValue('Needle')
            panel.retrieval.SetSelection(0)
            panel.result_mode.SetSelection(0)
            press(panel.go)
            later(lambda: await_page(
                lambda: not frame.working and panel.paging.page_number == 1, desktop_first))

        def desktop_first():
            rows = tuple(panel.results.values())
            snapshots['desktop_first'] = rows
            check(EXPECTED_CHECKS[1], len(rows) == 100 and all(row.verified for row in rows))
            check(EXPECTED_CHECKS[2], 'Page 1: 100 current results' in panel.status.GetValue()
                  and '100 of 121' in panel.status.GetValue()
                  and panel.next_page_button.IsEnabled()
                  and not panel.previous_page_button.IsEnabled())
            press(panel.next_page_button)
            later(lambda: await_page(
                lambda: not frame.working and panel.paging.page_number == 2, desktop_second))

        def desktop_second():
            rows = tuple(panel.results.values())
            check(EXPECTED_CHECKS[3], len(rows) == 21
                  and '121 of 121' in panel.status.GetValue()
                  and panel.previous_page_button.IsEnabled()
                  and not panel.next_page_button.IsEnabled())
            check(EXPECTED_CHECKS[4],
                  not {row.page for row in rows} & {row.page for row in snapshots['desktop_first']})
            old = snapshots['desktop_first'][0]
            check(EXPECTED_CHECKS[5], expect_stale(
                lambda: library.copy(workspace, old.ref, old.revision, 'quote')))
            press(panel.previous_page_button)
            later(lambda: await_page(
                lambda: not frame.working and panel.paging.page_number == 1, desktop_back))

        def desktop_back():
            rows = tuple(panel.results.values())
            previous = snapshots['desktop_first']
            check(EXPECTED_CHECKS[6],
                  [(row.edition_id, row.page, row.quote) for row in rows] ==
                  [(row.edition_id, row.page, row.quote) for row in previous])
            check(EXPECTED_CHECKS[7], not {row.ref for row in rows} & {row.ref for row in previous})
            button, = [child for child in panel.GetChildren()
                       if isinstance(child, wx.Button) and child.GetLabel() == 'Copy &quote']
            press(button)
            later(desktop_copied)

        def desktop_copied():
            check(EXPECTED_CHECKS[8], clipboard() == panel.selected().quote)
            later(open_center)

        def open_center():
            nonlocal form
            form = NativeSearchCenter(frame, library.workspaces())
            form.query.SetValue('Needle')
            form.limit.SetSelection(0)
            index = next(i for i, row in enumerate(form.libraries) if row.id == other)
            form.libraries_control.SetSelection(index)
            later(lambda: press(form.libraries_control), 100)
            later(center_selected, 220)
            form.show()
            if not failures:
                check(EXPECTED_CHECKS[19], frame.active == (workspace, None) and not frame.working)
                check(EXPECTED_CHECKS[20], all(
                    sha256(library.assets.resolve(identity).read_bytes()).hexdigest() == digest
                    for identity, digest in originals.items()))
                gui.ExitMainLoop()

        def center_selected():
            selected = tuple(row.id for i, row in enumerate(form.libraries)
                             if form.libraries_control.IsChecked(i))
            check(EXPECTED_CHECKS[9], selected == (workspace, other))
            check(EXPECTED_CHECKS[10],
                  form.previous_button.GetName() == 'Previous results page'
                  and form.next_button.GetName() == 'Next results page'
                  and form.previous_button.AcceptsFocusFromKeyboard()
                  and form.next_button.AcceptsFocusFromKeyboard())
            press(form.search_button)
            later(lambda: await_page(
                lambda: not form.controller.running and form.controller.paging.page_number == 1,
                center_first))

        def center_first():
            rows = form.controller.rows
            snapshots['center_first'] = rows
            check(EXPECTED_CHECKS[11], len(rows) == 10 and all(
                hit.workspace == workspace and hit.result.verified for hit in rows))
            check(EXPECTED_CHECKS[12], 'Page 1: 10 current results' in form.status.GetValue()
                  and 'never a global score' in form.status.GetValue()
                  and '0 of 2' in form.status.GetValue())
            press(form.next_button)
            later(lambda: await_page(
                lambda: not form.controller.running and form.controller.paging.page_number == 2,
                center_second))

        def center_second():
            rows = form.controller.rows
            snapshots['center_second'] = rows
            check(EXPECTED_CHECKS[13], len(rows) == 10 and form.previous_button.IsEnabled())
            check(EXPECTED_CHECKS[14], not {hit.result.page for hit in rows} &
                  {hit.result.page for hit in snapshots['center_first']})
            press(form.previous_button)
            later(lambda: await_page(
                lambda: not form.controller.running and form.controller.paging.page_number == 1,
                center_back))

        def center_back():
            check(EXPECTED_CHECKS[15],
                  [(hit.result.page, hit.result.quote) for hit in form.controller.rows] ==
                  [(hit.result.page, hit.result.quote) for hit in snapshots['center_first']])
            check(EXPECTED_CHECKS[16], expect_stale(
                lambda: form.controller.service.copy(snapshots['center_second'][0], 'quote')))
            press(form.quote_button)
            later(lambda: await_page(
                lambda: not form.controller.running and 'copied' in form.status.GetValue().lower(),
                center_copied))

        def center_copied():
            check(EXPECTED_CHECKS[17], clipboard() == form.controller.rows[0].result.quote
                  and 'copied' in form.status.GetValue().lower())
            form.query.SetValue('absent')
            later(center_changed)

        def center_changed():
            check(EXPECTED_CHECKS[18], not form.controller.rows
                  and not form.previous_button.IsEnabled() and not form.next_button.IsEnabled()
                  and not form.quote_button.IsEnabled())
            press(form.query, wx.WXK_ESCAPE)

        watchdog = wx.CallLater(150000, lambda: guarded(
            lambda: (_ for _ in ()).throw(TimeoutError('Native continuation deadline.'))))
        timers.append(watchdog)
        try:
            later(begin, 120)
            gui.MainLoop()
        finally:
            for timer in timers:
                try:
                    timer.Stop()
                except (RuntimeError, AttributeError):
                    pass
            if frame is not None:
                frame.cancel_event.set()
                try:
                    frame.executor.shutdown(wait=True, cancel_futures=True)
                except BaseException:
                    failures.append('Owned worker cleanup: ' + traceback.format_exc())
                for window in ((form.window,) if form is not None and form._window_alive else ()) + (frame,):
                    try:
                        if window:
                            window.Destroy()
                    except BaseException:
                        failures.append('Owned window cleanup: ' + traceback.format_exc())

    if failures:
        print(json.dumps({'result': 'FAIL', 'checks': checks, 'failures': failures, 'nvda': 'NOT_RUN'}))
        return 1
    if Counter(checks) != Counter(EXPECTED_CHECKS):
        print(json.dumps({'result': 'FAIL', 'checks': checks, 'reason': 'Native oracle incomplete.', 'nvda': 'NOT_RUN'}))
        return 1
    origins = {}
    for module in (application, pagination, search_paging, federated_search):
        path = Path(module.__file__).resolve()
        origins[module.__name__] = {'path': str(path), 'sha256': sha256(path.read_bytes()).hexdigest()}
    print(json.dumps({'result': 'PASS', 'checks': checks,
                      'actual_native_keys': True, 'actual_source_copy': True,
                      'synthetic_corpus': True, 'nvda': 'NOT_RUN',
                      'real_corpus_benchmark': 'NOT_RUN',
                      'platform': platform.platform(), 'python': platform.python_version(),
                      'module_origins': origins}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

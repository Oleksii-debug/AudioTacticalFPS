"""Actual Windows result-page controls over the current library and worker.

Uses purpose-written public source text. It does not claim scale benchmarks,
semantic paging, screen-reader acceptance or model quality.
"""
import argparse
import faulthandler
from hashlib import sha256
import importlib.metadata
import importlib.util
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import time
import traceback


def evidence_helpers():
    path = Path(__file__).resolve(strict=True).with_name('windows_clipboard_reader_smoke.py')
    spec = importlib.util.spec_from_file_location('_search_native_evidence', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def source_origin(options):
    if os.name != 'nt' or not sys.flags.isolated or not sys.dont_write_bytecode:
        raise RuntimeError('Run this actual Windows search probe with Python -I -B.')
    checkout = Path(__file__).resolve(strict=True).parents[1]
    if options.source is not None:
        if options.expect_installed or options.source.resolve(strict=True) != checkout:
            raise ValueError('Select this exact source checkout or the installed package.')
        sys.path.insert(0, str(checkout / 'src'))
    elif not options.expect_installed:
        raise ValueError('Choose an explicit source or installed qualification.')
    return checkout


def run(options, report, evidence, checkout):
    import wx
    import carr_library
    from carr_library import application
    from carr_library.search import literal, pagination, presentation
    from carr_library.ui import desktop_app, search_model
    package = Path(carr_library.__file__).resolve(strict=True).parent
    distribution = None
    if options.expect_installed:
        owners = []
        for candidate in importlib.metadata.distributions(name='universal-research-library'):
            for member in candidate.files or ():
                if str(member).replace('\\', '/') == 'carr_library/__init__.py':
                    try:
                        actual = Path(candidate.locate_file(member)).resolve(strict=True)
                    except FileNotFoundError:
                        continue
                    if actual == Path(carr_library.__file__).resolve(strict=True):
                        owners.append(candidate)
        if len(owners) != 1:
            raise RuntimeError('Exactly one installed RECORD must own the search package.')
        distribution = owners[0]
    report['modules'] = evidence._module_evidence(checkout, package, (
        ('application.py', application), ('search/literal.py', literal),
        ('search/pagination.py', pagination), ('search/presentation.py', presentation),
        ('ui/desktop_app.py', desktop_app), ('ui/search_model.py', search_model)),
        source_mode=bool(options.source), distribution=distribution)
    report['wx'] = wx.version()
    if wx.Platform != '__WXMSW__':
        raise RuntimeError('The actual wxMSW controls are required.')
    failures = []
    class App(wx.App):
        def OnExceptionInMainLoop(self):
            failures.append(traceback.format_exc())
            return False
    gui = App(False)
    gui.SetExitOnFrameDelete(False)
    loop = wx.GUIEventLoop()
    activation = wx.EventLoopActivator(loop)
    simulator = wx.UIActionSimulator()

    def check(name, condition):
        report['phase'] = name
        evidence._checkpoint(options.report, report)
        if not condition:
            raise AssertionError(name)
        report['checks'].append(name)
        evidence._checkpoint(options.report, report)
        print('PASS: ' + name, flush=True)

    def pump(condition, title, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            gui.Yield()
            if failures:
                raise RuntimeError(failures[0])
            if condition():
                return
            wx.MilliSleep(10)
        raise TimeoutError(title)

    def press(control):
        check('native result control is enabled: ' + control.GetName(), control.IsEnabled())
        wx.GetTopLevelParent(control).Raise()
        control.SetFocus()
        pump(lambda: wx.Window.FindFocus() == control, 'The result control did not receive focus.', timeout=3)
        if not simulator.Char(wx.WXK_SPACE):
            raise RuntimeError('Actual native Space dispatch failed.')
        gui.Yield()

    with TemporaryDirectory(prefix='carr-search-pagination-') as temporary:
        library = application.LibraryApp(Path(temporary) / 'library')
        workspace = library.create_workspace('Native result pagination')
        raw = '\f'.join(f'Original section {index}: needle evidence in Київ.' for index in range(205))
        imported = library.add_text(workspace, 'Purpose-written source', raw)
        source = library.assets.resolve(imported['asset'])
        before = sha256(source.read_bytes()).hexdigest()
        other = library.create_workspace('Excluded private workspace')
        library.add_text(other, 'Foreign source', 'needle outside the selected library')
        frame = desktop_app.DesktopFrame(library)
        if frame.active != (workspace, None):
            frame._open_workspace(workspace)
        frame.SetSize((1100, 950))
        frame.Show()
        frame.Raise()
        panel = frame.book.GetPage(0)
        try:
            check('actual result paging buttons have accessible names',
                panel.next_page.GetName() == 'Next search results page'
                and panel.previous_page.GetName() == 'Previous search results page'
                and isinstance(panel.next_page, wx.Button)
                and isinstance(panel.previous_page, wx.Button))
            panel.query.SetValue('needle')
            panel.retrieval.SetSelection(0)
            panel.result_mode.SetSelection(0)
            press(panel.go)
            pump(lambda: not frame.working, 'The first native result page did not complete.')
            first = tuple((hit.page, hit.quote) for hit in panel.model.hits)
            check('first native page has one hundred exact authorized excerpts',
                len(first) == 100 and panel._page_number == 1
                and all(hit.workspace == workspace for hit in panel.model.hits)
                and panel.next_page.IsEnabled() and not panel.previous_page.IsEnabled())
            press(panel.next_page)
            pump(lambda: not frame.working, 'The second native result page did not complete.')
            second = tuple((hit.page, hit.quote) for hit in panel.model.hits)
            check('actual Next opens different results beyond the first hundred',
                len(second) == 100 and panel._page_number == 2
                and not set(first).intersection(second)
                and panel.previous_page.IsEnabled() and panel.next_page.IsEnabled())
            check('both enabled paging controls accept keyboard focus',
                panel.previous_page.AcceptsFocusFromKeyboard()
                and panel.next_page.AcceptsFocusFromKeyboard())
            press(panel.next_page)
            pump(lambda: not frame.working, 'The last native result page did not complete.')
            check('last native page contains all remaining results and disables Next',
                panel.model.row_count() == 5 and panel._page_number == 3
                and not panel.next_page.IsEnabled() and panel.previous_page.IsEnabled())
            press(panel.previous_page)
            pump(lambda: not frame.working, 'The native previous page did not complete.')
            check('actual Previous reloads the exact preceding page',
                tuple((hit.page, hit.quote) for hit in panel.model.hits) == second
                and panel._page_number == 2 and panel.next_page.IsEnabled())
            selected = panel.model.hits[0]
            check('paged result retains current exact quotation authority',
                library.copy(workspace, selected.ref, selected.revision, 'quote') == selected.quote)
            before_cursor, before_hits = panel._next_cursor, tuple(panel.model.hits)
            library.add_text(workspace, 'Added source', 'New source does not contain this search term.')
            press(panel.next_page)
            pump(lambda: not frame.working, 'The changed-scope result page did not return.')
            check('changed source scope rejects continuation and preserves prior visible results',
                tuple(panel.model.hits) == before_hits and panel._next_cursor == before_cursor
                and 'changed' in panel.status.GetValue()
                and panel.next_page.IsEnabled())
            panel.query.SetValue('evidence')
            gui.Yield()
            check('editing the query revokes both paging directions and stale hits',
                panel.model.row_count() == 0 and panel._next_cursor is None
                and not panel.previous_page.IsEnabled() and not panel.next_page.IsEnabled())
            press(panel.go)
            pump(lambda: not frame.working, 'The fresh native query did not complete.')
            check('fresh query starts at page one using the same worker',
                panel._page_number == 1 and panel.model.row_count() == 100)
            check('original retained source bytes remain unchanged',
                sha256(source.read_bytes()).hexdigest() == before)
            check('worker controls return after all result page operations',
                not frame.working and not frame.busy and frame.choice.IsEnabled()
                and frame.book.IsEnabled() and not frame.cancel_button.IsEnabled())
        finally:
            frame.cancel_event.set()
            pump(lambda: not frame.working, 'The native search worker did not drain before closure.')
            frame.Close()
            gui.Yield()
            frame.executor.shutdown(wait=True, cancel_futures=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--expect-installed', action='store_true')
    parser.add_argument('--report', type=Path, required=True)
    options = parser.parse_args()
    evidence = evidence_helpers()
    report = dict(status='FAIL', phase='starting', checks=[],
        source_commit=os.environ.get('GITHUB_SHA'), installed=options.expect_installed,
        scope='Actual native exact-search pagination, source quotation and worker controls',
        nvda='NOT_RUN', scale_benchmark='NOT_RUN')
    evidence._checkpoint(options.report, report)
    faulthandler.enable()
    faulthandler.dump_traceback_later(150, exit=True)
    runtime = None
    try:
        checkout = source_origin(options)
        from carr_library.native_runtime import activate_bundled_runtime
        runtime = activate_bundled_runtime()
        run(options, report, evidence, checkout)
        report.update(status='PASS', phase='complete')
        evidence._checkpoint(options.report, report)
        return 0
    except BaseException as exc:
        report.update(status='FAIL', phase='failed',
            failure={'type': type(exc).__name__, 'message': str(exc)[:2000]})
        evidence._checkpoint(options.report, report)
        raise
    finally:
        try:
            if runtime is not None:
                runtime.close()
        finally:
            faulthandler.cancel_dump_traceback_later()


if __name__ == '__main__':
    raise SystemExit(main())

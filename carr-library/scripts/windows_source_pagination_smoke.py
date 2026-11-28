"""Actual native bounded source windows and selected-library continuation.

Uses purpose-written retained text and the current shared desktop worker.
No semantic-model quality, scale benchmark, screen-reader or NVDA claim.
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
    import threading
    from contextlib import closing
    from uuid import uuid4
    import wx
    import carr_library
    from carr_library import application
    from carr_library.catalog import read_views
    from carr_library.catalog.membership import Catalog
    from carr_library.catalog.registration import Registration, register_batch
    from carr_library.citations.text_attestation import CONFIG
    from carr_library.storage.revisions import CURRENT_SCHEMA_VERSION, Page, Storage
    from carr_library.search import literal, pagination, source_windows, federated, federated_pages
    from carr_library.ui import desktop_app, federated_search
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
        ('application.py', application), ('catalog/read_views.py', read_views),
        ('search/literal.py', literal), ('search/pagination.py', pagination),
        ('search/source_windows.py', source_windows), ('search/federated.py', federated),
        ('search/federated_pages.py', federated_pages), ('ui/desktop_app.py', desktop_app),
        ('ui/federated_search.py', federated_search)),
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

    def focus(control):
        if not control.IsEnabled():
            raise AssertionError('The exact native search control is disabled.')
        wx.GetTopLevelParent(control).Raise()
        control.SetFocus()
        pump(lambda: wx.Window.FindFocus() is control, 'Native search focus did not settle.', timeout=3)

    def key(code, modifiers=0):
        if not simulator.Char(code, modifiers):
            raise RuntimeError('Actual native search keyboard dispatch failed.')
        gui.Yield()

    def press(control):
        focus(control)
        key(wx.WXK_SPACE)

    def text(control, value):
        focus(control)
        key(ord('A'), wx.MOD_CONTROL)
        if not simulator.Text(value.encode('ascii')):
            raise RuntimeError('Actual native search text entry failed.')
        gui.Yield()
        pump(lambda: control.GetValue() == value, 'The native search query did not match.')

    def choose(control, index):
        focus(control)
        key(wx.WXK_HOME)
        for _ in range(index):
            key(wx.WXK_DOWN)
        pump(lambda: control.GetSelection() == index, 'The native search choice did not settle.')

    with TemporaryDirectory(prefix='carr-source-window-native-') as temporary:
        library = application.LibraryApp(Path(temporary) / 'library')
        originals = []
        def source(workspace, title, raw):
            data = raw.encode('utf-8')
            digest = sha256(data).hexdigest()
            library.assets.retain(data, 'text/plain', digest)
            edition, revision, work = uuid4().hex, uuid4().hex, uuid4().hex
            with closing(Storage(library.database, schema_version=CURRENT_SCHEMA_VERSION)) as storage:
                with storage.transaction():
                    register_batch(Catalog(storage.db, workspace), (Registration(work, edition,
                        dict(sha256=digest, size=len(data), mime='text/plain'), title=title,
                        authors=('Purpose-written native fixture',), language='en'),))
                    storage.stage_revision(workspace, edition, revision, digest, CONFIG,
                                           (Page(part) for part in raw.split('\f')))
                    storage.db.execute('INSERT INTO source_metadata '
                        '(workspace,edition,revision,author,title,volume,edition_label) VALUES(?,?,?,?,?,?,?)',
                        (workspace, edition, revision, 'Purpose-written native fixture', title, None, None))
            originals.append((library.assets.resolve(digest), digest))

        workspace = library.create_workspace('00 Native ranked windows')
        source(workspace, 'Ranked source', '\f'.join(
            ['Unrelated source segment.'] * 200 +
            [f'Original needle evidence {index}.' for index in range(5)]))
        selected = []
        for number in range(25):
            chosen = library.create_workspace(f'Federated source {number:02d}')
            source(chosen, f'Purpose-written book {number}', f'Railway source {number:02d}.')
            selected.append(chosen)
        frame = desktop_app.DesktopFrame(library)
        frame.SetSize((1100, 950))
        frame.Show()
        frame.Raise()
        native = None
        try:
            pump(lambda: not frame.working and not frame.busy,
                 'The current desktop initialization did not finish.')
            if frame.active != (workspace, None):
                frame._open_workspace(workspace)
            panel = frame.book.GetPage(0)
            def grid_page(control, message):
                epoch = panel._context_epoch
                press(control)
                pump(lambda: panel._context_epoch > epoch and not frame.working and not frame.busy, message)
            text(panel.query, 'needle')
            choose(panel.retrieval, panel.MODES.index('thematic'))
            grid_page(panel.go, 'The first actual ranked source window did not finish.')
            check('empty ranked window retains an explicit native Next action',
                  panel.model.row_count() == 0 and panel.next_page.IsEnabled()
                  and 'More source windows remain' in panel.status.GetValue()
                  and 'local to each window' in panel.status.GetValue())
            grid_page(panel.next_page, 'The second actual source window did not finish.')
            check('second empty window is reachable without changing source scope',
                  panel._page_number == 2 and panel.model.row_count() == 0
                  and panel.next_page.IsEnabled() and panel.previous_page.IsEnabled())
            grid_page(panel.next_page, 'The final ranked source window did not finish.')
            check('later exact source matches remain reachable through native Next',
                  panel._page_number == 3 and panel.model.row_count() == 5
                  and not panel.next_page.IsEnabled()
                  and all(hit.workspace == workspace for hit in panel.model.hits))
            last = tuple((hit.page, hit.quote) for hit in panel.model.hits)
            grid_page(panel.previous_page, 'The actual previous source window did not finish.')
            check('native Previous returns to its empty window',
                  panel._page_number == 2 and panel.model.row_count() == 0 and panel.next_page.IsEnabled())
            grid_page(panel.next_page, 'The actual next source window could not be revisited.')
            check('revisited window retains exact source excerpts',
                  tuple((hit.page, hit.quote) for hit in panel.model.hits) == last)

            active, book = frame.active, frame.book
            original_search_page = application.LibraryApp.search_page
            threads = []
            def observed(candidate, *args, **kwargs):
                threads.append(threading.get_ident())
                return original_search_page(candidate, *args, **kwargs)
            application.LibraryApp.search_page = observed
            native = federated_search.NativeSearchCenter(frame, library.workspaces())
            controller = native.controller
            finished = []
            def scenario():
                try:
                    check('new Search Center starts without continuation',
                          controller.next_cursor is None and not native.next_button.IsEnabled())
                    focus(native.libraries_control)
                    key(wx.WXK_HOME)
                    expected = tuple(row.id for row in native.libraries if row.id in selected)
                    for index, row in enumerate(native.libraries):
                        if index:
                            key(wx.WXK_DOWN)
                        pump(lambda: native.libraries_control.GetSelection() == index,
                             'The native library-list cursor did not settle.', timeout=3)
                        desired = row.id in selected
                        if native.libraries_control.IsChecked(index) != desired:
                            key(wx.WXK_SPACE)
                        pump(lambda: native.libraries_control.IsChecked(index) == desired,
                             'The actual native checked scope did not match.', timeout=3)
                    check('native checklist explicitly selects twenty-five libraries',
                          len(expected) == 25 and sum(native.libraries_control.IsChecked(i)
                                                     for i in range(len(native.libraries))) == 25)
                    text(native.query, 'Railway')
                    epoch = controller.epoch
                    press(native.search_button)
                    pump(lambda: controller.epoch > epoch and not controller.running and not frame.working,
                         'The actual first federated page did not finish.')
                    first = controller.rows
                    check('first native page visits at most twenty selected libraries',
                          len(first) == 20 and controller.service.current.workspaces == expected[:20]
                          and native.next_button.IsEnabled())
                    check('source search executes on the existing background worker',
                          threads and all(identity != threading.get_ident() for identity in threads))
                    epoch = controller.epoch
                    press(native.next_button)
                    pump(lambda: controller.epoch > epoch and not controller.running and not frame.working,
                         'The actual next federated page did not finish.')
                    check('native Next reaches only the five remaining libraries',
                          len(controller.rows) == 5 and controller.service.current.workspaces == expected[20:]
                          and not native.next_button.IsEnabled())
                    try:
                        controller.service.copy(first[0], 'quote')
                    except ValueError:
                        revoked = True
                    else:
                        revoked = False
                    check('previous page source handles are revoked', revoked)
                    press(native.quote_button)
                    pump(lambda: not controller.running and not frame.working
                         and 'copied from' in native.status.GetValue().lower(),
                         'The actual current-page quotation verification did not finish.')
                    if not wx.TheClipboard.Open():
                        raise RuntimeError('The native clipboard could not be inspected after this fixture copy.')
                    try:
                        copied = wx.TextDataObject()
                        accepted = wx.TheClipboard.GetData(copied)
                    finally:
                        wx.TheClipboard.Close()
                    check('current-page quotation uses the native verified clipboard action',
                          'copied from' in native.status.GetValue().lower()
                          and accepted and copied.GetText() == controller.rows[0].result.quote)
                    check('searching another library never replaces the active notebook',
                          frame.active == active and frame.book is book)
                    finished.append(True)
                    press(native.close_button)
                except BaseException:
                    failures.append(traceback.format_exc())
                    controller.cancel()
                    if native._window_alive and native.window.IsModal():
                        native.window.EndModal(wx.ID_CANCEL)
            try:
                wx.CallLater(40, scenario)
                native.show()
            finally:
                application.LibraryApp.search_page = original_search_page
            if failures:
                raise RuntimeError(failures[0])
            check('native selected-library dialog closes after its shared worker completes',
                  finished == [True] and not controller.alive and not frame.working)
            check('every retained original remains byte-identical',
                  all(sha256(path.read_bytes()).hexdigest() == digest for path, digest in originals))
            check('main scope and worker controls are retained after both paging workflows',
                  frame.active == active and frame.book is book and not frame.busy
                  and frame.choice.IsEnabled() and frame.book.IsEnabled())
        finally:
            frame.cancel_event.set()
            if native is not None:
                native.controller.dispose()
            # Always join the same worker, even after a recorded native failure.
            # The queued completion then sees disposed reader controls.
            frame.executor.shutdown(wait=True, cancel_futures=True)
            gui.Yield()
            if native is not None and native._window_alive:
                if native.window.IsModal():
                    native.window.EndModal(wx.ID_CANCEL)
                native.window.Destroy()
            frame.Close()
            gui.Yield()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--expect-installed', action='store_true')
    parser.add_argument('--report', type=Path, required=True)
    options = parser.parse_args()
    evidence = evidence_helpers()
    report = dict(status='FAIL', phase='starting', checks=[],
        source_commit=os.environ.get('GITHUB_SHA'), installed=options.expect_installed,
        scope='Actual native ranked source windows and 25-library bounded continuation',
        nvda='NOT_RUN', scale_benchmark='NOT_RUN')
    evidence._checkpoint(options.report, report)
    faulthandler.enable()
    faulthandler.dump_traceback_later(180, exit=True)
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

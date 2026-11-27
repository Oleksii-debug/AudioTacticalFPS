"""Actual Windows Word-review controls with an explicitly synthetic COM backend.

The native host, CSL formatter, shared worker, broker, fixed protocol and Windows
Job execute. Microsoft Word itself is NOT_RUN here. The separate Office probe
requires an already-open disposable document and explicit operator consent.
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
from hashlib import sha256
import importlib
import json
import os
from pathlib import Path
import sys
import sysconfig
from tempfile import TemporaryDirectory
import threading
import time
import traceback

ORIGINS = (
    'carr_library.academic_integration.word_bridge', 'carr_library.academic_integration.word_worker',
    'carr_library.academic_integration.word_native', 'carr_library.academic_integration.word_records',
    'carr_library.academic_integration.formatting', 'carr_library.citations.bibliography_workbench',
    'carr_library.citations.csl', 'carr_library.ui.word_citations_dialog',
    'carr_library.ui.citation_styles_dialog', 'carr_library.ui.academic_library_dialog',
    'carr_library.ui.desktop_app', 'carr_library.ui.worker_lifetime',
    'carr_library.audio.process', 'carr_library.audio.windows_job', 'carr_library.audio.cleanup',
    'carr_library.operations.broker', 'carr_library.operations.models', 'carr_library.application',
    'carr_library.durable_state', 'carr_library.native_runtime',
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root', type=Path, required=True)
    parser.add_argument('--require-installed', action='store_true')
    parser.add_argument('--synthetic-transport', action='store_true',
                        help='Required: acknowledge that the Word COM backend is a protocol fixture.')
    parser.add_argument('--output', type=Path)
    options = parser.parse_args()
    if os.name != 'nt' or not options.synthetic_transport:
        raise ValueError('Require actual Windows and explicit --synthetic-transport; this is not an Office qualification.')
    import wx
    import carr_library
    from carr_library.application import LibraryApp
    from carr_library.ui.desktop_app import DesktopFrame
    from carr_library.ui.academic_library_dialog import AcademicController
    from carr_library.ui.citation_styles_dialog import create_citation_styles_dialog
    from carr_library.academic_integration import word_bridge, word_worker
    from carr_library.audio import windows_job
    from carr_library.operations import ResourceBroker

    source_root = options.source_root.resolve(strict=True)
    installed = Path(os.environ.get('CARR_PROGRAM_DIR') or sysconfig.get_path('purelib')).resolve(strict=True)
    checks, errors, children, stages, observations, threads = [], [], [], [], [], []
    gui_thread = threading.get_ident()
    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        if name not in checks:
            checks.append(name)
            print('PASS: ' + name, flush=True)
    def origins():
        result = {}
        for name in ORIGINS:
            module = importlib.import_module(name)
            actual = Path(module.__file__).resolve(strict=True)
            expected = source_root.joinpath(*name.split('.')).with_suffix('.py')
            digest = sha256(actual.read_bytes()).hexdigest()
            if digest != sha256(expected.read_bytes()).hexdigest():
                raise AssertionError('Module bytes differ: ' + name)
            if options.require_installed and (not actual.is_relative_to(installed) or actual.is_relative_to(source_root)):
                raise AssertionError('The Word probe imported a source module: ' + name)
            result[name] = {'path': str(actual), 'sha256': digest, 'installed': actual.is_relative_to(installed)}
        return result
    before = origins()
    check('all twenty Word host and protocol module bytes match their requested origins', len(before) == 20)
    fixture = source_root.parent / 'tests' / 'academic_integration' / 'word_fixtures.py'
    if not fixture.is_file():
        raise ValueError('The exact checked-out synthetic protocol companion is required.')
    fixture_hash = sha256(fixture.read_bytes()).hexdigest()
    package_root = str(Path(carr_library.__file__).resolve().parent.parent)

    class App(wx.App):
        def OnExceptionInMainLoop(self):
            errors.append(traceback.format_exc())
            unwind()
            self.ExitMainLoop()
            return False
    app = App(False)
    app.SetAssertMode(wx.APP_ASSERT_EXCEPTION)
    app.SetExitOnFrameDelete(False)
    simulator = wx.UIActionSimulator()
    user = ctypes.WinDLL('user32', use_last_error=True)
    user.IsWindow.argtypes, user.IsWindow.restype = [wintypes.HWND], wintypes.BOOL
    user.SetForegroundWindow.argtypes, user.SetForegroundWindow.restype = [wintypes.HWND], wintypes.BOOL
    user.GetForegroundWindow.argtypes, user.GetForegroundWindow.restype = [], wintypes.HWND
    user.FindWindowW.argtypes, user.FindWindowW.restype = [wintypes.LPCWSTR, wintypes.LPCWSTR], wintypes.HWND
    user.GetFocus.argtypes, user.GetFocus.restype = [], wintypes.HWND
    user.GetDlgCtrlID.argtypes, user.GetDlgCtrlID.restype = [wintypes.HWND], ctypes.c_int
    def unwind():
        for window in reversed(wx.GetTopLevelWindows()):
            if isinstance(window, wx.Dialog) and window.IsModal():
                window.EndModal(wx.ID_CANCEL)
    def pump(predicate, message, timeout=35, *, cleanup=False):
        deadline = time.monotonic() + timeout
        while not predicate():
            if errors and not cleanup:
                raise RuntimeError('\n'.join(errors))
            if time.monotonic() >= deadline:
                raise TimeoutError(message)
            app.Yield()
            time.sleep(.01)
    def settle():
        until = time.monotonic() + .04
        pump(lambda: time.monotonic() >= until, 'Native events did not settle.')
    def press(control):
        check('every invoked Word action is enabled in its actual native state', control.IsEnabled())
        top = wx.GetTopLevelParent(control)
        top.Show(); top.Raise()
        user.SetForegroundWindow(top.GetHandle()); control.SetFocus()
        pump(lambda: user.GetForegroundWindow() == top.GetHandle() and wx.Window.FindFocus() is control,
             'Native Word control did not receive focus.')
        if not simulator.Char(wx.WXK_SPACE):
            raise AssertionError('Native Space input failed.')
        settle()
    def answer(yes):
        try:
            title = 'Apply reviewed Word change'
            pump(lambda: bool(user.FindWindowW(None, title)), 'The native Word confirmation did not open.')
            handle = user.FindWindowW(None, title)
            user.SetForegroundWindow(handle)
            check('native Word application confirmation starts on No', user.GetDlgCtrlID(user.GetFocus()) == 7)
            if yes:
                if not simulator.Char(wx.WXK_LEFT):
                    raise AssertionError('Native Yes selection failed.')
            if not simulator.Char(wx.WXK_RETURN):
                raise AssertionError('Native confirmation key failed.')
        except Exception:
            errors.append(traceback.format_exc())
            unwind()

    original_run, original_child = word_bridge.run_process, windows_job.JobProcess
    class Child(original_child):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            if args and str(fixture) in args[0]:
                children.append(self)
    windows_job.JobProcess = Child
    frame = host_executor = None
    try:
        with TemporaryDirectory(prefix='resonance-word-native-') as temporary:
            root = Path(temporary)
            library = LibraryApp(root / 'library')
            workspace = library.create_workspace('Word protocol qualification')
            controller = AcademicController(library, workspace)
            reference = controller.create(dict(title='Native citation source', authors=('Smith, Jane',),
                year='2020', notes='PRIVATE_NOTE_MUST_NOT_ENTER_WORD'))
            original_source = controller.export(reference.id, format='csl-json', include_notes=False)
            frame = DesktopFrame(library)
            frame.active = workspace, None
            host_executor, handle = frame.executor, frame.GetHandle()
            check('actual current DesktopFrame owns the selected library and one worker',
                  frame.library is library and bool(user.IsWindow(handle)) and wx.Platform == '__WXMSW__')

            def run(argv, **kwargs):
                threads.append(threading.get_ident())
                if argv[1:3] != ['-I', '-B'] or len(argv) != 5 or Path(argv[3]).resolve() != Path(word_worker.__file__).resolve():
                    raise AssertionError('The production fixed Word argv changed.')
                if kwargs.get('owned_windows_job') is not True:
                    raise AssertionError('The actual private Windows Job is required.')
                stage = Path(kwargs['cwd'])
                stages.append(stage)
                observed = root / ('observed-' + str(len(stages)))
                observed.mkdir()
                observations.append(observed)
                actual = [argv[0], '-I', '-B', str(fixture), package_root,
                          argv[3], argv[4], 'complete', str(observed)]
                return original_run(actual, **kwargs)
            word_bridge.run_process = run

            def all_reaped():
                return children and all(child.poll() is not None and child._job is None for child in children)

            def word_driver():
                try:
                    pump(lambda: any(isinstance(w, wx.Dialog) and w.IsModal()
                         and w.GetTitle() == 'Already-open Word document' for w in wx.GetTopLevelWindows()),
                         'The actual workbench Word button did not open its dialog.')
                    word = next(w for w in wx.GetTopLevelWindows()
                        if isinstance(w, wx.Dialog) and w.IsModal() and w.GetTitle() == 'Already-open Word document')
                    check('opening Word review launches no child or document mutation', not children)
                    check('Word operation and preview controls have distinct accessible names',
                          len({c.GetName() for c in (word.inspect, word.preview_button, word.apply_button,
                               word.references, word.output, word.references_output, word.status)}) == 7)
                    press(word.inspect)
                    pump(lambda: word.pending is None, 'Native read-only inspection did not complete.')
                    check('actual inspection uses one reaped protocol child without a mutation',
                          len(children) == 1 and all_reaped() and not (observations[0] / 'mutation.json').exists())
                    check('document references remain a read-only inert display',
                          bool(word.references_output.GetWindowStyleFlag() & wx.TE_READONLY))
                    word.references.SetSelection(0)
                    press(word.references)
                    check('native Space selects an explicit source reference', word.references.IsChecked(0))
                    press(word.preview_button)
                    pump(lambda: word.view is not None, 'The actual worker did not publish its finite CSL preview.')
                    view = word.view
                    check('actual canonical CSL preview is not a synthetic citation string',
                          'Smith' in view.explanation and 'PRIVATE_NOTE_MUST_NOT_ENTER_WORD' not in view.explanation + view.references_json)
                    check('the held preview retains its child staging and shared reservation',
                          len(children) == 2 and children[-1].poll() is None
                          and stages[-1].is_dir() and len(ResourceBroker.for_app(library.root).snapshot()['leases']) == 1)
                    check('preview alone does not write a mutation or final grant',
                          not (observations[-1] / 'mutation.json').exists() and not (stages[-1] / 'grant.json').exists())
                    wx.CallLater(60, answer, False)
                    press(word.apply_button)
                    check('native No leaves the held preview unapplied',
                          not word.decision.state(view.token) and not (observations[-1] / 'mutation.json').exists())
                    wx.CallLater(60, answer, True)
                    press(word.apply_button)
                    pump(lambda: word.pending is None, 'Explicit native Yes did not complete the actual protocol.')
                    check('explicit native Yes completes exactly the reviewed proposal',
                          word.bridge.last_outcome.status == 'COMPLETED'
                          and word.bridge.last_outcome.receipt['proposal_sha256'] == view.token
                          and (observations[1] / 'mutation.json').is_file())
                    check('confirmed completion returns accessible status and releases the private Job',
                          wx.Window.FindFocus() is word.status and all_reaped()
                          and all(not stage.exists() for stage in stages)
                          and ResourceBroker.for_app(library.root).snapshot()['leases'] == [])
                    press(word.copy_button)
                    if not wx.TheClipboard.Open():
                        raise AssertionError('The actual clipboard could not be read.')
                    try:
                        data = wx.TextDataObject()
                        if not wx.TheClipboard.GetData(data):
                            raise AssertionError('The copied CSL references were unavailable.')
                        copied = json.loads(data.GetText())
                    finally:
                        wx.TheClipboard.Close()
                    check('native copy exposes only the reviewed inert document references',
                          len(copied) == 1 and copied[0]['id'] == reference.id and 'note' not in copied[0])
                    press(word.preview_button)
                    pump(lambda: word.view is not None, 'The second held preview did not arrive.')
                    press(word.close_button)
                    pump(lambda: word.pending is None, 'Close did not settle the owned Word child.')
                    check('busy Close stops and reports before destroying the native review',
                          word.alive and all_reaped() and len(children) == 3
                          and not (observations[2] / 'mutation.json').exists()
                          and ResourceBroker.for_app(library.root).snapshot()['leases'] == []
                          and all(not stage.exists() for stage in stages))
                    press(word.close_button)
                    check('review closure never shuts down its borrowed desktop worker', not word.alive and not host_executor._shutdown)
                except Exception:
                    errors.append(traceback.format_exc())
                    unwind()

            def scenario():
                styles = None
                try:
                    styles = create_citation_styles_dialog(frame, controller,
                        selected_identity=reference.id, executor=host_executor)
                    def styles_driver():
                        try:
                            pump(lambda: styles.IsModal(), 'The CSL workbench did not open.')
                            press(styles.generate)
                            pump(lambda: styles.pending is None and styles.preview is not None,
                                 'The existing CSL preview did not complete.', timeout=50)
                            check('existing workbench formatting remains the Word entrypoint',
                                  styles.word_button.IsEnabled() and styles.executor is host_executor)
                            wx.CallLater(60, word_driver)
                            press(styles.word_button)
                            if errors:
                                raise RuntimeError('\n'.join(errors))
                            press(styles.close_button)
                        except Exception:
                            errors.append(traceback.format_exc())
                            unwind()
                    wx.CallLater(60, styles_driver)
                    styles.ShowModal()
                    if errors:
                        raise RuntimeError('\n'.join(errors))
                    check('all Word protocol work uses the existing single off-main worker',
                          len(threads) == 3 and len(set(threads)) == 1 and gui_thread not in threads)
                    check('bibliographic source bytes remain exact after every native action',
                          controller.export(reference.id, format='csl-json', include_notes=False) == original_source)
                    frame.Close()
                    pump(lambda: not user.IsWindow(handle), 'The actual DesktopFrame close path did not finish.')
                    host_executor.shutdown(wait=True, cancel_futures=True)
                    check('actual host finalizers settle before native completion is reported',
                          not user.IsWindow(handle) and not host_executor.cleanup_failures and all_reaped())
                except Exception:
                    errors.append(traceback.format_exc())
                finally:
                    if styles is not None:
                        styles.Destroy()
                    app.ExitMainLoop()
            try:
                frame.Show(); app.SetTopWindow(frame)
                wx.CallAfter(scenario)
                app.MainLoop()
                if errors:
                    raise RuntimeError('\n'.join(errors))
            finally:
                unwind()
                if user.IsWindow(handle):
                    frame.Close(force=True)
                    pump(lambda: not user.IsWindow(handle), 'Failure cleanup did not close the host.', cleanup=True)
                for child in children:
                    if child.poll() is None:
                        child.kill(); child.wait(timeout=5)
                    child.close()
                host_executor.shutdown(wait=True, cancel_futures=True)
    finally:
        word_bridge.run_process, windows_job.JobProcess = original_run, original_child
    check('application and synthetic companion bytes remain unchanged after native cleanup',
          origins() == before and sha256(fixture.read_bytes()).hexdigest() == fixture_hash)
    report = {'status': 'PASS', 'checks': checks, 'installed_modules': before,
        'actual_native_ui': 'PASS', 'actual_csl': 'PASS', 'synthetic_word_protocol': 'PASS',
        'owned_child_cleanup': 'PASS', 'owned_child_invocations': len(children),
        'actual_word': 'NOT_RUN', 'manual_nvda': 'NOT_RUN', 'office_document_quality': 'NOT_RUN',
        'synthetic_companion_sha256': fixture_hash}
    if options.output is not None:
        from carr_library.speech._io import new_output
        with new_output(options.output) as output:
            output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=True, indent=2), flush=True)
    return 0


if __name__ == '__main__':
    finished = threading.Event()
    def watchdog():
        if not finished.wait(180):
            os.write(2, b'Native Word protocol probe exceeded its work and cleanup deadline.\n')
            os._exit(86)
    threading.Thread(target=watchdog, daemon=True).start()
    native = None
    try:
        import faulthandler
        faulthandler.enable(all_threads=True)
        from carr_library.native_runtime import activate_bundled_runtime
        native = activate_bundled_runtime()
        raise SystemExit(main())
    finally:
        if native is not None:
            native.close()
        finished.set()

"""Native Windows import/EPUB continuation with real domain services and keys.

The UI fixture injects a one-file/one-book page budget into the current services.
The separate source regressions cross actual 5000/20000/500 boundaries. This
probe is keyboard/control qualification, not a corpus benchmark or NVDA claim.
"""
from argparse import ArgumentParser
from collections import Counter
from hashlib import sha256
import ctypes
from io import BytesIO
import json
from pathlib import Path
import platform
import sys
from tempfile import TemporaryDirectory
from threading import Event, get_ident
import time
import traceback
from zipfile import ZipFile, ZIP_DEFLATED, ZIP_STORED


EXPECTED_CHECKS = (
    'actual-desktop-frame-exists-before-mainloop',
    'import-continue-control-keyboard-addressable',
    'small-injected-import-page-is-durable',
    'import-page-status-announces-continuation',
    'native-continue-starts-existing-background-service',
    'native-cancel-reaches-live-scan-callback',
    'cancelled-page-keeps-previous-durable-batch',
    'cancelled-page-does-not-publish-next-batch',
    'native-continue-retries-the-same-next-identity',
    'continued-page-has-a-distinct-exact-selected-source',
    'continued-batches-reopen-without-rescanning',
    'native-import-close-destroys-owned-hwnd',
    'native-epub-command-opens-first-bounded-page',
    'epub-chooser-is-keyboard-addressable',
    'native-epub-next-opens-second-page',
    'epub-second-page-has-previous-and-next',
    'native-epub-previous-restores-first-book-choice',
    'native-epub-selection-opens-the-second-edition',
    'selected-epub-text-matches-retained-original',
    'epub-list-and-source-work-use-existing-worker-thread',
    'native-epub-back-destroys-owned-reader-hwnd',
    'native-epub-cancel-opens-no-source-section',
    'host-library-scope-is-preserved',
    'all-synthetic-original-bytes-are-unchanged',
    'native-close-destroys-owned-desktop-hwnd',
    'all-recorded-owned-native-windows-are-gone',
)


def write_epub(path, identity, content):
    members = {
        'mimetype': 'application/epub+zip',
        'META-INF/container.xml': (
            '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="OPS/package.opf" media-type="application/oebps-package+xml"/>'
            '</rootfiles></container>'),
        'OPS/package.opf': (
            '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">'
            '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
            '<dc:identifier id="id">' + identity + '</dc:identifier><dc:title>Synthetic native fixture</dc:title>'
            '<dc:language>en</dc:language><meta property="dcterms:modified">2026-11-27T00:00:00Z</meta>'
            '</metadata><manifest><item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/>'
            '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
            '</manifest><spine><itemref idref="chapter"/></spine></package>'),
        'OPS/chapter.xhtml': (
            '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>First chapter</title></head>'
            '<body><h1 id="first">First chapter</h1><p>' + content + '</p></body></html>'),
        'OPS/nav.xhtml': (
            '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">'
            '<head><title>Contents</title></head><body><nav epub:type="toc"><ol>'
            '<li><a href="chapter.xhtml#first">First chapter</a></li></ol></nav></body></html>'),
    }
    output = BytesIO()
    with ZipFile(output, 'w') as archive:
        for name, text in members.items():
            archive.writestr(name, text.encode('utf-8'),
                             compress_type=ZIP_STORED if name == 'mimetype' else ZIP_DEFLATED)
    path.write_bytes(output.getvalue())


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--native', action='store_true')
    args = parser.parse_args()
    if not args.native:
        parser.error('Choose --native for the actual Windows controls and keyboard.')
    if sys.platform != 'win32':
        print(json.dumps({'result': 'NOT_RUN', 'reason': 'Native Windows is required.', 'nvda': 'NOT_RUN'}))
        return 77
    try:
        import wx
        from carr_library.application import LibraryApp
        from carr_library.ui.desktop_app import DesktopFrame
        import carr_library.application as application
        import carr_library.ingest.batch_scan as scan
        import carr_library.ingest.batches as batches
        import carr_library.reader.ebook as ebook
        import carr_library.ui.ebook_reader as ebook_ui
        import carr_library.ui.import_center as import_ui
    except (ImportError, OSError) as error:
        print(json.dumps({'result': 'NOT_RUN', 'reason': str(error), 'nvda': 'NOT_RUN'}))
        return 77

    checks, failures, observed_windows = [], [], {}
    frame = None
    timer = None
    waiting = None
    complete = False
    deadline = time.monotonic() + 180
    entered, release, hold = Event(), Event(), Event()
    snapshot_calls, listing_threads, source_calls = [], [], []
    ui_thread = get_ident()
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.IsWindow.argtypes = [ctypes.c_void_p]
    user32.IsWindow.restype = ctypes.c_int
    user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    user32.GetWindowThreadProcessId.restype = ctypes.c_ulong

    def native_alive(handle):
        return bool(handle and user32.IsWindow(handle))

    def remember(window):
        import os
        handle = int(window.GetHandle())
        process = ctypes.c_ulong()
        if not native_alive(handle) or not user32.GetWindowThreadProcessId(handle, ctypes.byref(process)):
            raise AssertionError('The expected native window is unavailable.')
        if process.value != os.getpid():
            raise AssertionError('The probe cannot use another process window.')
        observed_windows[handle] = window
        return handle

    def descendants(window):
        for child in window.GetChildren():
            yield child
            yield from descendants(child)

    def modal(kind=None):
        for window in wx.GetTopLevelWindows():
            if window is frame or not isinstance(window, wx.Dialog):
                continue
            try:
                if not window.IsShown() or window.GetParent() is not frame:
                    continue
                if kind == 'import' and window.GetTitle() != 'Import Center':
                    continue
                if kind == 'choice' and not isinstance(window, wx.SingleChoiceDialog):
                    continue
                if kind == 'reader' and (isinstance(window, wx.SingleChoiceDialog)
                                         or window.GetTitle() != 'Read EPUB book'):
                    continue
                remember(window)
                return window
            except RuntimeError:
                continue
        return None

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)
        print('PASS:', name, flush=True)

    def abort():
        release.set()
        if frame is not None:
            frame.cancel_event.set()
        for window in tuple(wx.GetTopLevelWindows()):
            try:
                if isinstance(window, wx.Dialog) and window.GetParent() is frame and window.IsModal():
                    remember(window)
                    window.EndModal(wx.ID_CANCEL)
            except RuntimeError:
                pass
        gui.ExitMainLoop()

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            failures.append(traceback.format_exc())
            abort()
            return False

    try:
        gui = ProbeApp(False)
        gui.SetExitOnFrameDelete(False)
    except (Exception, SystemExit) as error:
        print(json.dumps({'result': 'NOT_RUN', 'reason': str(error), 'nvda': 'NOT_RUN'}))
        return 77
    simulator = wx.UIActionSimulator()

    def press(control, key=wx.WXK_SPACE):
        if not control.IsShownOnScreen() or not control.IsEnabled():
            raise AssertionError('The selected native control is unavailable.')
        control.SetFocus()
        if wx.Window.FindFocus() is not control:
            raise AssertionError('Keyboard focus did not reach the selected control.')
        if not simulator.KeyDown(key) or not simulator.KeyUp(key):
            raise AssertionError('Native keyboard injection failed.')

    def shortcut(control):
        control.SetFocus()
        if wx.Window.FindFocus() is not control:
            raise AssertionError('The reader command did not receive native focus.')
        for key in (wx.WXK_CONTROL, wx.WXK_SHIFT, ord('R')):
            if not simulator.KeyDown(key):
                raise AssertionError('Native EPUB shortcut key-down failed.')
        for key in (ord('R'), wx.WXK_SHIFT, wx.WXK_CONTROL):
            if not simulator.KeyUp(key):
                raise AssertionError('Native EPUB shortcut key-up failed.')

    def button(window, label):
        found = [child for child in descendants(window) if isinstance(child, wx.Button)
                 and child.GetLabel().replace('&', '') == label]
        if len(found) != 1:
            raise AssertionError('The exact native button is unavailable: ' + label)
        return found[0]

    def chooser(window):
        found = [child for child in descendants(window) if isinstance(child, wx.ListBox)]
        if len(found) != 1:
            raise AssertionError('The native EPUB list is unavailable.')
        return found[0]

    def condition(label, predicate):
        return label, predicate

    old_max = scan.MAX_FILES
    real_snapshot, real_listing, real_read = scan.planned_snapshot, ebook_ui.list_ebooks_page, ebook_ui.read_ebook_section

    def controlled_snapshot(path, **options):
        snapshot_calls.append((get_ident(), str(path)))
        if hold.is_set():
            entered.set()
            if not release.wait(20):
                raise TimeoutError('The explicit native cancellation fixture was not released.')
        return real_snapshot(path, **options)

    def small_listing(*args, **options):
        listing_threads.append(get_ident())
        return real_listing(*args, **options, limit=1)

    def observed_read(*args, **options):
        result = real_read(*args, **options)
        source_calls.append((get_ident(), args[2], args[3], result.raw_text))
        return result

    scan.MAX_FILES = 1
    scan.planned_snapshot = controlled_snapshot
    ebook_ui.list_ebooks_page = small_listing
    ebook_ui.read_ebook_section = observed_read

    with TemporaryDirectory(prefix='carr-import-epub-native-') as temporary:
        root = Path(temporary).resolve()
        library = LibraryApp(root / 'private')
        workspace = library.create_workspace('Native continuation fixture')
        sources = root / 'selected-files'
        sources.mkdir()
        original_files = []
        for index in range(3):
            path = sources / f'{index}.txt'
            path.write_text(f'Synthetic selected source {index}.', encoding='utf-8')
            original_files.append(path)
        published = {}
        for letter in 'ABC':
            path = root / f'source-{letter}.epub'
            write_epub(path, 'fixture-' + letter, f'Synthetic EPUB source {letter}.')
            result = library._import_file(workspace, path, title=f'Native EPUB {letter}',
                work_id='native-work-' + letter, edition_id='native-epub-' + letter)
            if result['state'] != 'complete':
                raise AssertionError('The actual synthetic EPUB import did not complete.')
            published[letter] = result
            original_files.append(path)
        originals = {str(path): sha256(path.read_bytes()).hexdigest() for path in original_files}
        records = {}

        def scenario():
            nonlocal frame
            frame = DesktopFrame(library)
            frame._open_workspace(workspace)
            frame.SetSize((1150, 950))
            frame.Show()
            frame.Raise()
            records['frame'] = remember(frame)
            check(EXPECTED_CHECKS[0], native_alive(records['frame']))
            # The generator is primed here before MainLoop is started.
            yield condition('desktop shown', lambda: frame.IsShownOnScreen())
            wx.CallAfter(frame.import_center)
            yield condition('Import Center opened', lambda: modal('import') is not None)
            form = modal('import')
            import_handle = remember(form)
            following = button(form, 'Continue scan')
            check(EXPECTED_CHECKS[1], following.AcceptsFocusFromKeyboard()
                  and following.GetLabel().replace('&', '') == 'Continue scan')
            form.plan((sources,))
            yield condition('first import page', lambda: not form.busy and not frame.working and form.batch is not None)
            first = form.batch
            records['first'] = first
            check(EXPECTED_CHECKS[2], first['version'] == 2 and len(first['rows']) == 1
                  and first['scan']['cursor'] is not None
                  and library.import_batch(workspace, first['batch_id']) == first)
            check(EXPECTED_CHECKS[3], 'More selected sources remain' in form.status.GetValue())
            hold.set()
            press(following)
            yield condition('continued source boundary', entered.is_set)
            check(EXPECTED_CHECKS[4], form.busy and frame.working and form.cancel.IsEnabled()
                  and snapshot_calls[-1][0] != ui_thread)
            press(form.cancel)
            yield condition('native cancellation', frame.cancel_event.is_set)
            check(EXPECTED_CHECKS[5], frame.cancel_event.is_set())
            hold.clear()
            release.set()
            yield condition('cancelled page returned', lambda: not form.busy and not frame.working)
            check(EXPECTED_CHECKS[6], form.batch['batch_id'] == first['batch_id']
                  and library.import_batch(workspace, first['batch_id']) == first
                  and 'cancel' in form.status.GetValue().lower())
            next_path = library.root / 'import-batches' / (first['scan']['next_batch_id'] + '.json')
            check(EXPECTED_CHECKS[7], not next_path.exists())
            press(following)
            yield condition('continued page saved', lambda: not form.busy and not frame.working
                            and form.batch is not None and form.batch['batch_id'] != first['batch_id'])
            second = form.batch
            check(EXPECTED_CHECKS[8], second['batch_id'] == first['scan']['next_batch_id'])
            check(EXPECTED_CHECKS[9], len(second['rows']) == 1
                  and second['rows'][0]['source'] != first['rows'][0]['source']
                  and second['rows'][0]['manifest']['sha256'] ==
                      originals[second['rows'][0]['source']])
            reopened = LibraryApp(library.root)
            reads_before_replay = len(snapshot_calls)
            check(EXPECTED_CHECKS[10], reopened.continue_import_scan(workspace, first['batch_id']) == second
                  and len(snapshot_calls) == reads_before_replay)
            press(button(form, 'Close'))
            yield condition('Import Center destroyed', lambda: not native_alive(import_handle))
            check(EXPECTED_CHECKS[11], not native_alive(import_handle))

            frame.Raise()
            shortcut(frame.status)
            yield condition('first EPUB chooser', lambda: modal('choice') is not None and not frame.working)
            first_choice = modal('choice')
            first_handle = remember(first_choice)
            first_list = chooser(first_choice)
            first_labels = tuple(first_list.GetStrings())
            check(EXPECTED_CHECKS[12], len(first_labels) == 2 and 'Native EPUB A' in first_labels[0]
                  and first_labels[-1] == 'Next page of EPUB books')
            check(EXPECTED_CHECKS[13], first_list.AcceptsFocusFromKeyboard()
                  and first_choice.GetTitle() == 'Read EPUB book')
            press(first_list, wx.WXK_END)
            yield condition('next selected by native key', lambda: first_list.GetSelection() == 1)
            press(first_list, wx.WXK_RETURN)
            yield condition('second EPUB chooser', lambda: not native_alive(first_handle)
                            and modal('choice') is not None and not frame.working)
            second_choice = modal('choice')
            second_handle = remember(second_choice)
            second_list = chooser(second_choice)
            labels = tuple(second_list.GetStrings())
            check(EXPECTED_CHECKS[14], len(labels) == 3 and 'Native EPUB B' in labels[0])
            check(EXPECTED_CHECKS[15], labels[1:] == ('Previous page of EPUB books', 'Next page of EPUB books'))
            press(second_list, wx.WXK_HOME)
            yield condition('second page first item selected', lambda: second_list.GetSelection() == 0)
            press(second_list, wx.WXK_DOWN)
            yield condition('previous selected by native key', lambda: second_list.GetSelection() == 1)
            press(second_list, wx.WXK_RETURN)
            yield condition('first EPUB page restored', lambda: not native_alive(second_handle)
                            and modal('choice') is not None and not frame.working)
            back_choice = modal('choice')
            back_handle = remember(back_choice)
            back_list = chooser(back_choice)
            check(EXPECTED_CHECKS[16], tuple(back_list.GetStrings()) == first_labels)
            press(back_list, wx.WXK_END)
            yield condition('next selected again', lambda: back_list.GetSelection() == 1)
            press(back_list, wx.WXK_RETURN)
            yield condition('second EPUB page revisited', lambda: not native_alive(back_handle)
                            and modal('choice') is not None and not frame.working)
            selected_choice = modal('choice')
            selected_handle = remember(selected_choice)
            selected_list = chooser(selected_choice)
            press(selected_list, wx.WXK_HOME)
            yield condition('second book selected', lambda: selected_list.GetSelection() == 0)
            press(selected_list, wx.WXK_RETURN)
            yield condition('real EPUB section', lambda: not native_alive(selected_handle)
                            and modal('reader') is not None and not frame.working)
            reader = modal('reader')
            reader_handle = remember(reader)
            texts = [child for child in descendants(reader) if isinstance(child, wx.TextCtrl)
                     and child.GetName() == 'Extracted section text — unverified']
            check(EXPECTED_CHECKS[17], len(source_calls) == 1
                  and source_calls[0][1:3] == ('native-epub-B', published['B']['id']))
            check(EXPECTED_CHECKS[18], len(texts) == 1
                  and texts[0].GetValue() == source_calls[0][3]
                  and 'Synthetic EPUB source B.' in texts[0].GetValue()
                  and 'Synthetic EPUB source A.' not in texts[0].GetValue())
            check(EXPECTED_CHECKS[19], bool(listing_threads)
                  and all(identity != ui_thread for identity in listing_threads)
                  and source_calls[0][0] != ui_thread)
            press(button(reader, 'Back'), wx.WXK_ESCAPE)
            yield condition('EPUB reader destroyed', lambda: not native_alive(reader_handle))
            check(EXPECTED_CHECKS[20], not native_alive(reader_handle))
            frame.Raise()
            shortcut(frame.status)
            yield condition('cancel chooser opened', lambda: modal('choice') is not None and not frame.working)
            cancelled_choice = modal('choice')
            cancelled_handle = remember(cancelled_choice)
            press(chooser(cancelled_choice), wx.WXK_ESCAPE)
            yield condition('cancel chooser destroyed', lambda: not native_alive(cancelled_handle))
            check(EXPECTED_CHECKS[21], len(source_calls) == 1 and not frame.working)
            check(EXPECTED_CHECKS[22], frame.library is library and frame.active == (workspace, None))
            check(EXPECTED_CHECKS[23], all(sha256(Path(path).read_bytes()).hexdigest() == digest
                                         for path, digest in originals.items()))
            frame.Raise()
            frame.status.SetFocus()
            if not simulator.KeyDown(wx.WXK_ALT) or not simulator.KeyDown(wx.WXK_F4):
                raise AssertionError('Native close shortcut failed.')
            if not simulator.KeyUp(wx.WXK_F4) or not simulator.KeyUp(wx.WXK_ALT):
                raise AssertionError('Native close shortcut key-up failed.')
            yield condition('desktop destroyed', lambda: not native_alive(records['frame']))
            check(EXPECTED_CHECKS[24], not native_alive(records['frame']))
            check(EXPECTED_CHECKS[25], all(not native_alive(handle) for handle in observed_windows))

        script = scenario()

        def pump():
            nonlocal timer, waiting, complete
            if failures or complete:
                return
            try:
                if time.monotonic() >= deadline:
                    raise TimeoutError('Native continuation deadline while waiting for ' + waiting[0])
                if waiting[1]():
                    waiting = next(script)
                timer = wx.CallLater(30, pump)
            except StopIteration:
                complete = True
                gui.ExitMainLoop()
            except BaseException:
                failures.append(traceback.format_exc())
                abort()

        try:
            waiting = next(script)
            timer = wx.CallLater(80, pump)
            gui.MainLoop()
        except BaseException:
            failures.append(traceback.format_exc())
        finally:
            release.set()
            if timer is not None:
                timer.Stop()
            if frame is not None:
                frame.cancel_event.set()
                frame._closing = True
                progress = getattr(frame, 'operation_progress', None) or getattr(frame, 'progress_view', None)
                if progress is not None:
                    try:
                        progress.close()
                    except BaseException:
                        failures.append('Progress cleanup: ' + traceback.format_exc())
                try:
                    frame.executor.shutdown(wait=True, cancel_futures=True)
                except BaseException:
                    failures.append('Worker cleanup: ' + traceback.format_exc())
            for handle, window in reversed(tuple(observed_windows.items())):
                if not native_alive(handle):
                    continue
                try:
                    if isinstance(window, wx.Dialog) and window.IsModal():
                        window.EndModal(wx.ID_CANCEL)
                    window.Destroy()
                except RuntimeError:
                    failures.append('An owned native window could not be closed.')
            cleanup_deadline = time.monotonic() + 3
            while any(native_alive(handle) for handle in observed_windows) and time.monotonic() < cleanup_deadline:
                gui.ProcessPendingEvents()
                wx.YieldIfNeeded()
                time.sleep(.01)
            if any(native_alive(handle) for handle in observed_windows):
                failures.append('An owned native window remained after cleanup.')
            scan.MAX_FILES = old_max
            scan.planned_snapshot = real_snapshot
            ebook_ui.list_ebooks_page = real_listing
            ebook_ui.read_ebook_section = real_read

    if not complete or Counter(checks) != Counter(EXPECTED_CHECKS):
        failures.append('The exact native continuation oracle is incomplete.')
    origins = {}
    for module in (application, scan, batches, ebook, ebook_ui, import_ui):
        path = Path(module.__file__).resolve()
        origins[module.__name__] = {'path': str(path), 'sha256': sha256(path.read_bytes()).hexdigest()}
    print(json.dumps({'result': 'FAIL' if failures else 'PASS', 'checks': checks,
        'check_count': len(checks), 'failures': failures, 'actual_native_keys': True,
        'actual_domain_services': True, 'synthetic_sources': True,
        'injected_test_page_limits': {'import_files': 1, 'epub_editions': 1},
        'real_corpus_benchmark': 'NOT_RUN', 'nvda': 'NOT_RUN',
        'platform': platform.platform(), 'python': platform.python_version(),
        'module_origins': origins}, ensure_ascii=False))
    return 1 if failures else 0


if __name__ == '__main__':
    raise SystemExit(main())

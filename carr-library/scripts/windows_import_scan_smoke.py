"""Native bounded scan continuation and actual isolated TXT imports.

Only the selected folder-picker result is supplied. The budget controls,
keyboard buttons, background work, page persistence and imports are real.
Installed modules are required unless --source-root is explicitly selected.
"""
import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
import sysconfig
import threading
from tempfile import TemporaryDirectory
from time import monotonic, sleep
import traceback


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root', type=Path, help='Explicit carr-library source tree; default is installed modules')
    args = parser.parse_args(argv)
    if os.name != 'nt':
        print(json.dumps({'result': 'NOT_RUN', 'reason': 'Native Windows required'}))
        return 77
    selected_source = args.source_root.resolve(strict=True) if args.source_root else None
    if selected_source is not None:
        sys.path[:0] = [str(selected_source / 'src'), str(selected_source)]
    from carr_library.native_runtime import activate_bundled_runtime
    native_handle = activate_bundled_runtime()
    import ctypes
    from ctypes import wintypes
    import wx
    from carr_library import application
    from carr_library.ingest import batch_scan, batch_pages, batches, worker, publication
    from carr_library.operations import ResourceBroker
    from carr_library.ui import import_center, desktop_app, worker_lifetime
    DesktopFrame = desktop_app.DesktopFrame

    checks, errors, origins = [], [], {}
    frame = None
    picker = wx.DirDialog

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)
        print('PASS:', name, flush=True)

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            errors.append(traceback.format_exc())
            self.ExitMainLoop()
            return False

    gui = ProbeApp(False)
    gui.SetExitOnFrameDelete(False)
    simulator = wx.UIActionSimulator()
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.GetForegroundWindow.argtypes = ()
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.IsWindow.argtypes = (wintypes.HWND,)
    user32.IsWindow.restype = wintypes.BOOL

    def pump(predicate, label, timeout=120):
        end = monotonic() + timeout
        while not predicate():
            if errors:
                raise RuntimeError('\n'.join(errors))
            if monotonic() >= end:
                raise TimeoutError(label)
            gui.Yield()
            sleep(.01)

    def find(parent, name):
        result = wx.Window.FindWindowByName(name, parent)
        check('named native control: ' + name, result is not None)
        return result

    def press(control):
        name = control.GetName()
        owner = wx.GetTopLevelParent(control)
        owner.Raise()
        user32.SetForegroundWindow(owner.GetHandle())
        control.SetFocus()
        pump(lambda: wx.Window.FindFocus() is control
             and user32.GetForegroundWindow() == owner.GetHandle(), 'Keyboard focus: ' + name, 10)
        check('native Space down: ' + name, simulator.KeyDown(wx.WXK_SPACE))
        try:
            gui.Yield()
            sleep(.03)
        finally:
            check('native Space up: ' + name, simulator.KeyUp(wx.WXK_SPACE))
        gui.Yield()

    def idle(dialog):
        pump(lambda: not dialog.busy and not frame.working, 'Import Center worker acknowledgement')

    try:
        check('actual wx Windows controls', wx.Platform == '__WXMSW__')
        checkout = Path(__file__).resolve().parents[1]
        installed_root = (Path(os.environ["CARR_PROGRAM_DIR"]).resolve(strict=True)
                          if os.environ.get("CARR_PROGRAM_DIR")
                          else Path(sysconfig.get_path("purelib")).resolve(strict=True))
        for module in (application, batch_scan, batch_pages, batches, worker, publication,
                       import_center, desktop_app, worker_lifetime):
            path = Path(module.__file__).resolve(strict=True)
            if selected_source is None:
                check('installed module: ' + module.__name__, path.is_relative_to(installed_root))
                expected = checkout / 'src' / Path(*module.__name__.split('.')).with_suffix('.py')
                check('exact candidate installed bytes: ' + module.__name__,
                      sha256(path.read_bytes()).hexdigest() == sha256(expected.read_bytes()).hexdigest())
            else:
                check('explicit source module: ' + module.__name__,
                      path.is_relative_to(selected_source / 'src'))
            origins[module.__name__] = {'path': str(path), 'sha256': sha256(path.read_bytes()).hexdigest()}
        with TemporaryDirectory(prefix='carr-native-scan-pages-') as temporary:
            root = Path(temporary)
            folder = root / 'Selected original sources'
            folder.mkdir()
            texts = {
                '01-one.txt': 'Synthetic first manuscript quotation.',
                '02-two.txt': 'Synthetic source deliberately excluded from this page.',
                '03-three.txt': 'Synthetic continuation manuscript quotation.',
            }
            for name, text in texts.items():
                (folder / name).write_text(text, encoding='utf-8')
            original = {name: (folder / name).read_bytes() for name in texts}
            library = application.LibraryApp(root / 'library')
            workspace = library.create_workspace('Native source continuation')
            collection = library.create_collection(workspace, 'Reviewed destination')
            frame = DesktopFrame(library)
            frame.Show()
            gui.SetTopWindow(frame)
            state = {}

            class SelectedFolder:
                def __init__(self, parent, title, **options):
                    check('existing-folder picker contract', options['style'] == wx.DD_DIR_MUST_EXIST)
                def ShowModal(self):
                    return wx.ID_OK
                def GetPath(self):
                    return str(folder)
                def Destroy(self):
                    pass

            wx.DirDialog = SelectedFolder

            def drive():
                try:
                    popup, = [item for item in wx.GetTopLevelWindows()
                              if item.IsShown() and item.GetTitle() == 'Import Center']
                    state['dialog'] = popup
                    check('native budget controls are actual SpinCtrls',
                          all(isinstance(find(popup, name), wx.SpinCtrl) for name in
                              ('Files per scan page', 'Traversal entries per scan page',
                               'Seconds per source scan page')))
                    popup.page_files.SetValue(2)
                    popup.page_entries.SetValue(30)
                    popup.page_seconds.SetValue(60)
                    popup.collection.SetSelection(popup.collection_ids.index(collection))
                    press(find(popup, 'Add folder...'))
                    idle(popup)
                    first = popup.batch
                    check('first native page retains two queued rows without importing',
                          first is not None and first['version'] == 2 and first['total'] == 2
                          and first['counts']['queued'] == 2 and library.browse(workspace) == ())
                    check('native page budget is saved exactly',
                          first['scan']['limits'] == {'max_files': 2, 'max_entries': 30,
                              'timeout': 60.0, 'max_bytes': 4 * 1024**2})
                    check('first page explicitly reports remaining source entries',
                          not first['scan']['cursor']['complete']
                          and 'Continue source scan' in popup.status.GetValue())
                    first_id = first['batch_id']
                    stable_requests = [row['request_id'] for row in first['rows']]
                    popup.rows.SetSelection(1)
                    press(find(popup, 'Exclude or include selected file'))
                    check('native exclusion affects only the selected row',
                          popup.batch['counts']['excluded'] == 1
                          and popup.batch['rows'][0]['state'] == 'queued')
                    press(find(popup, 'Start queued files'))
                    idle(popup)
                    check('explicit first-page run uses actual verified isolated child',
                          popup.batch['counts']['complete'] == 1
                          and popup.batch['counts']['excluded'] == 1
                          and popup.batch['rows'][0]['result']['isolated']
                          and popup.batch['rows'][0]['result']['verified'])
                    check('first-page exact quotation is retained',
                          any(library.copy(workspace, hit.ref, hit.revision, 'quote') == 'first manuscript quotation'
                              for hit in library.search(workspace, 'first manuscript quotation')))
                    # Current new-plan choices do not retarget an existing scan.
                    popup.collection.SetSelection(0)
                    popup.languages.SetSelection(1)
                    press(find(popup, 'Continue source scan'))
                    idle(popup)
                    second = popup.batch
                    check('native continuation creates the exact next saved page without auto import',
                          second['batch_id'] == first['scan']['next']
                          and second['scan']['previous'] == first_id
                          and second['counts']['queued'] == 1
                          and len(library.browse(workspace)) == 1)
                    check('continuation preserves reviewed scope and OCR settings',
                          second['collections'] == first['collections'] == [collection]
                          and second['config'] == first['config'])
                    check('last native source page proves complete traversal and exact count',
                          second['scan']['cursor']['complete']
                          and second['scan']['cursor']['planned'] == 3
                          and 'traversal is complete' in popup.status.GetValue())
                    second_id = second['batch_id']
                    press(find(popup, 'Start queued files'))
                    idle(popup)
                    check('second native page imports exactly its selected original',
                          popup.batch['counts']['complete'] == 1
                          and popup.batch['rows'][0]['file'] == '03-three.txt'
                          and popup.batch['rows'][0]['result']['isolated']
                          and popup.batch['rows'][0]['result']['verified']
                          and len(library.browse(workspace, collection=collection)) == 2)
                    reopened = application.LibraryApp(library.root)
                    saved_first = reopened.import_batch(workspace, first_id)
                    saved_second = reopened.import_batch(workspace, second_id)
                    check('reopen preserves previous imports exclusions and stable request identities',
                          saved_first['counts']['complete'] == 1 and saved_first['counts']['excluded'] == 1
                          and [row['request_id'] for row in saved_first['rows']] == stable_requests
                          and saved_second['counts']['complete'] == 1)
                    check('actual import and scanning release every resource lease',
                          ResourceBroker.for_app(library.root).snapshot()['leases'] == [])
                    check('all selected source bytes remain unchanged',
                          {name: (folder / name).read_bytes() for name in texts} == original)
                    popup.status.SetFocus()
                    pump(lambda: wx.Window.FindFocus() is popup.status, 'Readable native status focus', 10)
                    check('native completion focus is readable', wx.Window.FindFocus() is popup.status)
                    state['completed'] = True
                    press(find(popup, 'Close Import Center'))
                except Exception:
                    errors.append(traceback.format_exc())
                    frame.cancel_event.set()
                    popup = state.get('dialog')
                    if popup is not None and popup.IsModal():
                        popup.EndModal(wx.ID_CANCEL)

            def scenario():
                try:
                    wx.CallLater(30, drive)
                    import_center.show_import_center(frame, workspace)
                except Exception:
                    errors.append(traceback.format_exc())
                finally:
                    gui.ExitMainLoop()

            wx.CallAfter(scenario)
            gui.MainLoop()
            if errors:
                raise RuntimeError('\n'.join(errors))
            check('native continuation scenario completed', state.get('completed') is True)
            executor = frame.executor
            native_handle_value = frame.GetHandle()
            frame.Close()
            pump(lambda: not user32.IsWindow(native_handle_value), 'Original desktop host close acknowledgement', 15)
            executor.shutdown(wait=True, cancel_futures=True)
            check('native host shutdown settles all registered owners', not executor.cleanup_failures)
            check('original desktop HWND is destroyed', not user32.IsWindow(native_handle_value))
            frame = None
            gui.Yield()
    finally:
        wx.DirDialog = picker
        if frame is not None:
            frame.cancel_event.set()
            frame._closing = True
            frame.executor.shutdown(wait=True, cancel_futures=True)
            frame.Destroy()
            gui.Yield()
        if native_handle is not None:
            native_handle.close()
    print(json.dumps({'result': 'PASS', 'checks': checks, 'origins': origins,
        'source_commit': os.environ.get('GITHUB_SHA'), 'native_controls': 'PASS',
        'actual_isolated_imports': 'PASS', 'native_folder_picker': 'NOT_RUN',
        'nvda': 'NOT_RUN'}, ensure_ascii=False), flush=True)
    return 0


def _hard_deadline():
    # This daemon stays armed through cleanup and Python's non-daemon joins.
    # A normal process exit needs no cancellation; a retained owner is a failure.
    print(json.dumps({'result': 'FAIL', 'reason': 'Native scan probe exceeded its 180-second process deadline, including cleanup.'}), flush=True)
    os._exit(124)


if __name__ == '__main__':
    watchdog = threading.Timer(180, _hard_deadline)
    watchdog.daemon = True
    watchdog.start()
    raise SystemExit(main())

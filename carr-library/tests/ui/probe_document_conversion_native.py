"""Actual Windows menu/worker/result dialogs with explicit synthetic selectors.

Installed wheel: python -I tests/ui/probe_document_conversion_native.py --native
Checkout debugging: append --source /absolute/path/to/carr-library.

The production source/destination selector functions are explicitly replaced
with fixed synthetic paths. Native file/directory chooser interaction is NOT
tested. wx menus, shared command dispatch, background job, conversion process
and read-only result dialogs remain real. No NVDA acceptance is claimed.
"""
from argparse import ArgumentParser
from hashlib import sha256
import json
from pathlib import Path
import platform
import sys
from tempfile import TemporaryDirectory
from time import monotonic
import traceback
from unittest.mock import patch


def not_run(reason):
    print(json.dumps({'status': 'NOT_RUN', 'reason': reason,
                      'native_file_choosers': 'NOT_RUN', 'nvda': 'NOT_RUN'}), flush=True)
    return 77


def run_native(source=None, timeout=90):
    if sys.platform != 'win32':
        return not_run('This probe requires actual Windows wx controls.')
    try:
        import wx
    except (ImportError, OSError):
        return not_run('Native wxPython is unavailable.')
    if source is not None:
        sys.path[:0] = [str(source / 'src'), str(source)]
    import carr_library.conversion.service as conversion_module
    import carr_library.ui.document_conversion as ui
    import carr_library.ui.desktop_app as desktop_module
    from carr_library.application import LibraryApp

    failures, checks, shown = [], [], {}
    frame = None
    deadline = monotonic() + timeout
    observer = [None]

    def abort(reason):
        failures.append(str(reason))
        if frame is not None:
            frame.cancel_event.set()
        for window in wx.GetTopLevelWindows():
            if isinstance(window, wx.Dialog) and window.IsModal():
                window.EndModal(wx.ID_CANCEL)

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            abort(traceback.format_exc())
            return False

    try:
        application = ProbeApp(False)
    except (Exception, SystemExit):
        return not_run('A native wx application could not open.')
    native_loop = wx.GUIEventLoop()
    activation = wx.EventLoopActivator(native_loop)
    watchdog = wx.CallLater(int(timeout * 1000), abort, 'Native conversion probe exceeded its deadline.')

    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        checks.append(label)
        print('PASS:', label, flush=True)

    def pump(predicate):
        while monotonic() < deadline:
            application.Yield()
            if failures:
                raise AssertionError('; '.join(failures))
            if predicate():
                return
            wx.MilliSleep(10)
        raise TimeoutError('Native conversion probe exceeded its deadline.')

    def observe_results():
        try:
            for window in wx.GetTopLevelWindows():
                if (not isinstance(window, wx.Dialog) or not window.IsModal()
                        or window.GetTitle() not in ('Converted reading documents', 'Document conversion formats')):
                    continue
                title = window.GetTitle()
                readers = [child for child in window.GetChildren() if isinstance(child, wx.TextCtrl)]
                check(title + ' has one named read-only native text control',
                      len(readers) == 1 and not readers[0].IsEditable() and readers[0].GetName() == title)
                shown[title] = readers[0].GetValue()
                window.EndModal(wx.ID_CANCEL)
        except BaseException:
            abort(traceback.format_exc())
        if not failures and monotonic() < deadline:
            observer[0] = wx.CallLater(25, observe_results)

    origins = {module.__name__: str(Path(module.__file__).resolve())
               for module in (ui, desktop_module, conversion_module)}
    checkout = Path(__file__).resolve().parents[2]
    try:
        if source is None:
            check('conversion and desktop load from the installed distribution',
                  all(not Path(value).is_relative_to(checkout) for value in origins.values()))
        with TemporaryDirectory(prefix='carr-native-conversion-', ignore_cleanup_errors=True) as temporary:
            root = Path(temporary)
            library = LibraryApp(root / 'private-library')
            workspace = library.create_workspace('Synthetic converter library')
            imported = library.add_text(workspace, 'Protected source', 'Protected original source stays exact.')
            protected = library.assets.resolve(imported['asset'])
            original_asset = protected.read_bytes()
            source_path = root / 'Оригінал Ľudská.txt'
            source_bytes = 'Synthetic native conversion.\r\nУкраїна — Slovensko.'.encode('utf-8')
            source_path.write_bytes(source_bytes)
            destination = root / 'Читання export'
            destination.mkdir()
            frame = desktop_module.DesktopFrame(library)
            application.SetTopWindow(frame)
            frame.Show()
            frame.Raise()
            items = ui.install_conversion_actions(frame)
            menu_count = frame.menu_bar.GetMenuCount()
            check('conversion installer is idempotent in the real desktop',
                  ui.install_conversion_actions(frame) is items and frame.menu_bar.GetMenuCount() == menu_count)
            manager = frame.keyboard_commands
            check('both stable commands are registered with native menu bindings',
                  all(manager.registry.get(identity).context == 'main' and manager.available(identity)
                      for identity in ('conversion.documents', 'conversion.formats')))
            observer[0] = wx.CallLater(25, observe_results)

            # Explicit deterministic selectors only. No fake wx dialogs, service,
            # producer, worker, progress job or result-view implementation.
            with patch.object(ui, '_select_sources', lambda parent, binding: (source_path,)), \
                    patch.object(ui, '_select_destination', lambda parent, binding: destination):
                check('shared command dispatch reaches the production conversion menu handler',
                      manager.invoke('conversion.documents'))
                pump(lambda: 'Converted reading documents' in shown and not frame.working)
            text, = destination.glob('*.txt')
            receipt = Path(str(text) + '.provenance.json')
            metadata = json.loads(receipt.read_bytes())
            check('actual isolated conversion preserves exact Unicode and line-ending bytes',
                  text.read_bytes() == source_bytes and source_path.read_bytes() == source_bytes)
            check('actual receipt binds both hashes and refuses quotation authority',
                  metadata['source_sha256'] == metadata['text_sha256'] == sha256(source_bytes).hexdigest()
                  and metadata['verified_for_quotation'] is False and metadata['original_modified'] is False)
            check('native completion view contains paths and the source-review boundary',
                  str(text) in shown['Converted reading documents']
                  and str(receipt) in shown['Converted reading documents']
                  and 'quotation approval still requires source review' in shown['Converted reading documents'])

            check('shared command dispatch reaches the production format-inventory handler',
                  manager.invoke('conversion.formats'))
            pump(lambda: 'Document conversion formats' in shown and not frame.working)
            check('native format inventory shows implemented and explicitly unavailable adapters',
                  '.txt: available' in shown['Document conversion formats']
                  and '.azw: unavailable' in shown['Document conversion formats'])
            check('catalog original and library identity remain unchanged',
                  frame.library is library and protected.read_bytes() == original_asset
                  and len(library.browse(workspace)) == 1)
            frame.Close()
            pump(lambda: not frame)
            frame = None
        print(json.dumps({'status': 'PASS', 'checks': checks, 'platform': platform.platform(),
            'module_origins': origins, 'selection': 'explicit injected synthetic path selectors',
            'native_menu_dispatch': 'shared registry and native EVT_MENU',
            'native_result_views': 'actual read-only wx dialogs',
            'native_file_choosers': 'NOT_RUN', 'nvda': 'NOT_RUN'}, ensure_ascii=False), flush=True)
        return 0
    except BaseException:
        print(json.dumps({'status': 'FAIL', 'error': traceback.format_exc(),
                          'native_file_choosers': 'NOT_RUN', 'nvda': 'NOT_RUN'}), flush=True)
        return 1
    finally:
        watchdog.Stop()
        if observer[0] is not None:
            observer[0].Stop()
        if frame is not None:
            frame.cancel_event.set()
            frame._closing = True
            for window in wx.GetTopLevelWindows():
                if isinstance(window, wx.Dialog) and window.IsModal():
                    window.EndModal(wx.ID_CANCEL)
            frame.executor.shutdown(wait=True, cancel_futures=True)
            frame.Destroy()
            application.Yield()
        del activation


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--native', action='store_true', help='Run the actual Windows native probe')
    parser.add_argument('--source', type=Path, help='Explicit trusted source checkout; omit for installed-wheel verification')
    args = parser.parse_args()
    return run_native(args.source.resolve() if args.source else None) if args.native else not_run('Pass --native explicitly.')


if __name__ == '__main__':
    raise SystemExit(main())

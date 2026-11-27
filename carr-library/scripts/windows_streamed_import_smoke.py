"""Native Import Center controls + actual owned streamed-import completion.

The synthetic input is supplied to the real handler after native controls are
shown. This qualifies neither the OS file picker nor manual NVDA operation.
No import service, worker, progress observer or source reader is replaced.
"""
import argparse
import faulthandler
from hashlib import sha256
import importlib.metadata
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import traceback


def run_probe(wx, phase):
    from carr_library.application import LibraryApp
    from carr_library.ui.desktop_app import DesktopFrame
    from carr_library.ui.import_center import show_import_center
    from carr_library.ingest.worker import load_request

    failures, report, timers = [], {}, []
    state = dict(dialog=None, returned=False, destroyed=False, started=False,
                 frame_destroyed=False)
    frame = gui = cancel = executor = None

    def fail(message):
        failures.append(message)
        if cancel is not None:
            cancel.set()
        for window in reversed(wx.GetTopLevelWindows()):
            if isinstance(window, wx.Dialog) and window.IsModal():
                window.EndModal(wx.ID_CANCEL)
                break
        loop = None if gui is None else gui.GetMainLoop()
        if loop is not None:
            loop.ScheduleExit(1)

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            fail(traceback.format_exc())
            return False

        def OnEventLoopExit(self, loop):
            if failures and not loop.IsMain():
                wx.CallAfter(fail, 'A nested native dialog prevented streamed-import completion.')
            super().OnEventLoopExit(loop)

    gui = ProbeApp(False)
    with TemporaryDirectory(prefix='streamed-native-') as directory:
        root = Path(directory)
        library = LibraryApp(root / 'library Україна')
        workspace = library.create_workspace('Native streamed source')
        source = root / 'Original.txt'
        original = ('Exact native source\r\n' + 'x' * 65536 + '\fДругий фрагмент').encode('utf-8')
        source.write_bytes(original)
        frame = DesktopFrame(library)
        cancel, executor = frame.cancel_event, frame.executor
        frame_id = frame.GetId()
        def frame_destroyed(event):
            if event.GetId() == frame_id:
                state['frame_destroyed'] = True
            event.Skip()
        frame.Bind(wx.EVT_WINDOW_DESTROY, frame_destroyed)
        if frame.active != (workspace, None):
            frame._open_workspace(workspace)
        frame.SetSize((1250, 1000))
        frame.Show()

        def guarded(callback):
            if failures:
                return
            try:
                callback()
            except BaseException:
                fail(traceback.format_exc())

        def later(callback, delay=35):
            timers.append(wx.CallLater(delay, lambda: guarded(callback)))

        def open_center():
            phase('open-native-import-center')
            show_import_center(frame, workspace)
            state['returned'] = True

        def ready():
            choices = [window for window in wx.GetTopLevelWindows()
                       if isinstance(window, wx.Dialog) and window.GetTitle() == 'Import Center'
                       and window.GetParent() is frame and window.IsShownOnScreen() and window.IsModal()]
            if not choices:
                later(ready)
                return
            assert len(choices) == 1
            dialog = choices[0]
            phase('native-profile-focus-and-tab')
            state['dialog'] = dialog
            identifier = dialog.GetId()
            def destroyed(event):
                if event.GetId() == identifier:
                    state['destroyed'] = True
                event.Skip()
            dialog.Bind(wx.EVT_WINDOW_DESTROY, destroyed)
            dialog.SetSize((1100, 1050))
            assert dialog.stream_mib.GetName() == 'Maximum bytes for one streamed UTF-8 text, in MiB'
            dialog.stream_mib.SetValue(70)
            dialog.stream_mib.SetFocus()
            later(focused)

        def focused():
            dialog = state['dialog']
            focus = wx.Window.FindFocus()
            assert focus is dialog.stream_mib or dialog.stream_mib.IsDescendant(focus)
            assert dialog.stream_mib.GetValue() == 70
            simulator = wx.UIActionSimulator()
            simulator.Char(wx.WXK_TAB)
            later(begin)

        def begin():
            dialog = state['dialog']
            focus = wx.Window.FindFocus()
            assert focus.GetName() == 'Import selected UTF-8 text in bounded logical fragments'
            assert focus.IsShownOnScreen() and focus.IsEnabled()
            phase('actual-owned-import-and-disabled-controls')
            dialog.import_streamed_text(source)
            state['started'] = True
            assert dialog.busy and frame.working and dialog.cancel.IsEnabled()
            assert not dialog.stream_mib.IsEnabled()
            later(settled)

        def settled():
            dialog = state['dialog']
            if dialog.busy or frame.working:
                later(settled)
                return
            phase('verify-real-receipt-original-and-quotation')
            assert '3 exact logical fragments' in dialog.status.GetValue(), dialog.status.GetValue()
            assert dialog.stream_mib.IsEnabled() and not dialog.cancel.IsEnabled()
            requests = library.import_requests(workspace)
            assert len(requests) == 1 and requests[0]['state'] == 'complete'
            record = load_request(library.root, requests[0]['request_id'])
            assert record['version'] == 2
            assert record['source_profile']['maximum_bytes'] == 70 * 1024**2
            result = record['result']
            hit = library.search(workspace, 'Exact native source')[0]
            assert hit.verified and library.copy(workspace, hit.ref, hit.revision, 'quote') == 'Exact native source'
            assert sha256(source.read_bytes()).hexdigest() == result['asset']
            assert source.read_bytes() == original
            button = wx.Window.FindWindowById(wx.ID_CANCEL, dialog)
            assert button is not None and button.GetParent() is dialog
            phase('close-native-import-center')
            event = wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId())
            event.SetEventObject(button)
            assert button.GetEventHandler().ProcessEvent(event)
            later(closed)

        def closed():
            if not state['returned'] or not state['destroyed']:
                later(closed)
                return
            report.update(status='PASS', exact_fragments=3,
                checks=['native named numeric profile control', 'native focus and Tab to import action',
                        'real existing frame worker and owned import child', 'controls disabled until settlement',
                        'captured byte allowance', 'canonical request and completed result',
                        'original-byte verified quotation', 'unchanged original', 'owned native close and destruction'],
                file_picker='NOT_RUN', manual_nvda='NOT_RUN')
            phase('close-native-frame-after-worker-settlement')
            frame.Close()

        watchdog = wx.CallLater(60000, lambda: fail('Native streamed-import probe exceeded 60 seconds.'))
        later(open_center, 80)
        later(ready, 100)
        try:
            gui.MainLoop()
        finally:
            watchdog.Stop()
            for timer in timers:
                timer.Stop()
            cancel.set()
            executor.shutdown(wait=True, cancel_futures=True)
            if not state['frame_destroyed']:
                frame.Destroy()
            gui.ProcessPendingEvents()
        assert state['frame_destroyed'], 'The original native frame did not finish destruction.'
    if failures or report.get('status') != 'PASS':
        raise AssertionError('\n'.join(failures) or 'Native streamed-import probe did not finish.')
    return report


def _load_proof_helpers():
    """Reuse the existing bounded checkout/RECORD proof, without importing the app."""
    path = Path(__file__).resolve().with_name('windows_clipboard_reader_smoke.py')
    with path.open('rb') as source:
        raw = source.read(128 * 1024 + 1)
    if len(raw) > 128 * 1024:
        raise RuntimeError('The shared native provenance helper exceeds its bound.')
    scope = {'__name__': '_streamed_import_probe_proof', '__file__': str(path)}
    exec(compile(raw.decode('utf-8'), str(path), 'exec'), scope)
    return scope['_checkpoint'], scope['_module_evidence']


def _installed_owner(package_file, checkout):
    """Select the unique wheel RECORD which owns the package actually imported."""
    actual = Path(package_file).resolve(strict=True)
    if actual.is_relative_to(checkout):
        raise RuntimeError('Installed acceptance imported the checkout package.')
    matches = []
    for distribution in importlib.metadata.distributions(name='universal-research-library'):
        for member in distribution.files or ():
            if str(member).replace('\\', '/') != 'carr_library/__init__.py':
                continue
            try:
                declared = Path(distribution.locate_file(member)).resolve(strict=True)
            except FileNotFoundError:
                continue
            if declared == actual:
                matches.append(distribution)
    if len(matches) != 1:
        raise RuntimeError('Exactly one installed wheel RECORD must own the imported package.')
    return matches[0]


def main():
    parser = argparse.ArgumentParser()
    origin = parser.add_mutually_exclusive_group(required=True)
    origin.add_argument('--source', type=Path)
    origin.add_argument('--expect-installed', action='store_true')
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    checkpoint, module_evidence = _load_proof_helpers()
    report = dict(status='FAIL', phase='starting', checks=[],
        scope='actual Windows Import Center controls and owned streamed-import child',
        file_picker='NOT_RUN', manual_nvda='NOT_RUN', large_file_capacity='SEPARATE_TESTS',
        installed=args.expect_installed, source_commit=os.environ.get('GITHUB_SHA'),
        python=sys.version)
    checkpoint(args.report, report)

    def phase(name):
        report['phase'] = name
        checkpoint(args.report, report)

    handle = None
    faulthandler.enable()
    # Independent of the wx loop, and retained through worker/native cleanup.
    faulthandler.dump_traceback_later(180, exit=True)
    try:
        if os.name != 'nt':
            raise RuntimeError('This acceptance probe requires actual Windows controls.')
        checkout = Path(__file__).resolve().parents[1]
        if args.source is not None:
            selected = args.source.resolve(strict=True)
            if selected != checkout:
                raise RuntimeError('The source override must identify this exact checkout.')
            sys.path.insert(0, str(selected / 'src'))
        elif not sys.flags.isolated:
            raise RuntimeError('Installed acceptance requires Python -I.')

        phase('verify-imported-module-origin')
        import carr_library
        from carr_library import application, native_file_metadata, pending_backup
        from carr_library.citations import streamed_text_attestation
        from carr_library.ingest import (catalog_asset_copy, retained_assets, streamed_ingest,
                                         streamed_text, worker)
        from carr_library.reader import source_file
        from carr_library import native_runtime
        package_root = Path(carr_library.__file__).resolve(strict=True).parent
        distribution = None if args.source is not None else _installed_owner(
            carr_library.__file__, checkout)
        # Activate only the existing native CRT dependency for wx, not audio.
        handle = native_runtime.activate_bundled_runtime()
        import wx
        from carr_library.ui import desktop_app, import_center, source_launch
        report['wx_version'] = wx.version()
        report['modules'] = module_evidence(checkout, package_root,
            (('application.py', application), ('native_file_metadata.py', native_file_metadata),
             ('pending_backup.py', pending_backup),
             ('citations/streamed_text_attestation.py', streamed_text_attestation),
             ('ingest/catalog_asset_copy.py', catalog_asset_copy),
             ('ingest/retained_assets.py', retained_assets),
             ('ingest/streamed_ingest.py', streamed_ingest),
             ('ingest/streamed_text.py', streamed_text), ('ingest/worker.py', worker),
             ('reader/source_file.py', source_file), ('ui/desktop_app.py', desktop_app),
             ('ui/import_center.py', import_center), ('ui/source_launch.py', source_launch)),
            source_mode=args.source is not None, distribution=distribution)
        phase('native-controls-and-owned-import')
        result = run_probe(wx, phase)
        report.update(result)
        phase('completed')
        return 0
    except BaseException as exc:
        report['status'] = 'FAIL'
        report['failure'] = {'type': type(exc).__name__, 'message': str(exc)[:2000]}
        report['traceback'] = traceback.format_exc()[-8000:]
        phase('failed')
        raise
    finally:
        try:
            if handle is not None:
                handle.close()
        finally:
            faulthandler.cancel_dump_traceback_later()


if __name__ == '__main__':
    raise SystemExit(main())

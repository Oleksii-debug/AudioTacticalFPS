"""Installed native conversion menu, real supervised readers, and Cancel probe.

Only local file/folder chooser selections are substituted. The desktop frame,
menu binding, worker executor, ConversionService, isolated reader processes,
progress controls and result dialog are the installed implementation. Native
command events drive the controls; physical keyboard, pickers and NVDA require
separate manual acceptance. No private book, model or network service is used.

Run on Windows with the installed bundle interpreter:
  .venv/Scripts/python.exe scripts/windows_conversion_smoke.py --bundle ROOT --output REPORT
"""
from hashlib import sha256
import json
import os
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory
from threading import Timer
from time import monotonic


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if sys.platform != 'win32':
        print(json.dumps({'status': 'NOT_RUN', 'windows_execution': 'NOT_RUN'}))
        return 77
    if not __debug__:
        raise ValueError('Run this acceptance probe without Python optimization.')
    if os.path.lexists(args.output):
        raise FileExistsError('Choose a new acceptance report path.')

    bundle = args.bundle.resolve(strict=True)
    import carr_library
    installed_root = Path(carr_library.__file__).resolve().parent
    if not installed_root.is_relative_to(bundle / '.venv'):
        raise ValueError('The probe must use this bundle\'s actual installed application.')
    manifest_path = bundle / 'bundle-manifest.json'
    manifest_raw = manifest_path.read_bytes()
    manifest = json.loads(manifest_raw)
    from carr_library.native_runtime import activate_bundled_runtime
    native_handle = activate_bundled_runtime(bundle_root=bundle)
    if native_handle is None:
        raise ValueError('The installed native runtime was not activated.')
    import wx
    from carr_library.application import LibraryApp
    from carr_library.progress import ProgressTracker
    from carr_library.ui.desktop_app import DesktopFrame

    source_root = Path(__file__).resolve().parents[1] / 'src/carr_library'
    modules = (
        'audio/jobs.py', 'research/export_file.py', 'conversion/__init__.py',
        'conversion/models.py', 'conversion/capabilities.py', 'conversion/service.py',
        'conversion/worker.py', 'extract/document_formats.py', 'ingest/worker.py',
        'ui/document_conversion.py', 'ui/ocr_options.py', 'ui/desktop_app.py',
        'progress.py', 'ui/progress_view.py',
    )
    hashes = {}
    for name in modules:
        raw = (source_root / name).read_bytes()
        if (installed_root / name).read_bytes() != raw:
            raise ValueError('Installed conversion source differs from the acceptance checkout: ' + name)
        hashes[name] = sha256(raw).hexdigest()

    failures, network_attempts = [], []
    def offline(*args, **kwargs):
        network_attempts.append(True)
        raise AssertionError('Network is disabled in the native conversion acceptance process.')
    socket.getaddrinfo = socket.create_connection = offline
    socket.socket.connect = socket.socket.connect_ex = offline

    def force_failure():
        try:
            print(json.dumps({
                'schema': 'carr-native-conversion-smoke/1', 'status': 'FAIL',
                'windows_execution': 'FAIL', 'forced_process_exit': True,
                'error': 'Native conversion acceptance exceeded its bounded cleanup deadline.',
                'failures': failures, 'nvda': 'NOT_RUN',
            }, ensure_ascii=False), flush=True)
        finally:
            os._exit(1)

    watchdog = Timer(70, force_failure)
    watchdog.daemon = True
    watchdog.start()
    final = {}
    try:
        app = wx.App(False)
        with TemporaryDirectory(prefix='carr-native-conversion-') as temporary:
            root = Path(temporary)
            inputs, outputs, cancelled_output = (root / name for name in ('inputs', 'reading', 'cancelled'))
            for directory in (inputs, outputs, cancelled_output):
                directory.mkdir()
            text_source, html_source, cancel_source = (
                inputs / 'Київ-Žilina.txt', inputs / 'static.html', inputs / 'cancel-source.txt')
            original = {
                text_source: b'\xef\xbb\xbf' + 'Перший\r\n  Žilina  \f東京\n'.encode('utf-8'),
                html_source: '<h1>Київ</h1><p>Žilina &amp; 東京.</p><script>do_not_run()</script>'.encode('utf-8'),
                cancel_source: b'Public synthetic cancellation source.\r\n' * 60_000,
            }
            for path, raw in original.items():
                path.write_bytes(raw)
            expected = {
                '0001-Київ-Žilina-reading.txt': (text_source, original[text_source][3:]),
                '0002-static-reading.txt': (html_source, 'Київ\n\nŽilina & 東京.'.encode('utf-8')),
            }
            library = LibraryApp(root / 'library')
            frame = DesktopFrame(library)
            frame.Show()
            database = library.root / 'library.sqlite'
            catalog_before = sha256(database.read_bytes()).hexdigest()
            native_file_dialog, native_dir_dialog = wx.FileDialog, wx.DirDialog
            choices, destroyed = [], []
            phase, cancel_delivered, cancel_scheduled = ['ready'], [False], [False]
            completion_dialog_seen = [False]
            launches, cleanup_deadline = [], [None]
            deadline = monotonic() + 50
            timer = wx.Timer(frame)

            class Selection:
                def __init__(self, parent, message, *, kind, **options):
                    assert parent is frame
                    assert kind in ('files', 'folder')
                    expected_title = ('Choose documents for new reading text' if kind == 'files' else
                                      'Choose the folder for new reading files and provenance reports')
                    assert message == expected_title
                    if kind == 'files':
                        assert options['style'] & wx.FD_MULTIPLE
                        assert options['style'] & wx.FD_FILE_MUST_EXIST
                    self.kind, self.phase = kind, phase[0]
                    assert self.phase in ('success', 'cancel')
                    choices.append((self.phase, kind))
                def ShowModal(self):
                    return wx.ID_OK
                def GetPaths(self):
                    assert self.kind == 'files'
                    return [str(path) for path in (
                        (text_source, html_source) if self.phase == 'success' else (cancel_source,))]
                def GetPath(self):
                    assert self.kind == 'folder'
                    return str(outputs if self.phase == 'success' else cancelled_output)
                def Destroy(self):
                    destroyed.append((self.phase, self.kind))

            wx.FileDialog = lambda parent, message, **options: Selection(parent, message, kind='files', **options)
            wx.DirDialog = lambda parent, message, **options: Selection(parent, message, kind='folder', **options)

            def press(control):
                assert isinstance(control, wx.Button) and control.IsEnabled()
                event = wx.CommandEvent(wx.EVT_BUTTON.typeId, control.GetId())
                event.SetEventObject(control)
                control.GetEventHandler().ProcessEvent(event)

            def completion_dialogs():
                return [window for window in wx.GetTopLevelWindows()
                        if isinstance(window, wx.Dialog) and window.GetParent() is frame
                        and window.GetTitle() == 'Document conversion complete']

            def invoke_menu():
                matches = []
                bar = frame.GetMenuBar()
                for index in range(bar.GetMenuCount()):
                    for item in bar.GetMenu(index).GetMenuItems():
                        if item.GetItemLabelText() == 'Convert documents to reading text...':
                            matches.append(item)
                assert len(matches) == 1 and matches[0].IsEnabled()
                event = wx.CommandEvent(wx.EVT_MENU.typeId, matches[0].GetId())
                event.SetEventObject(frame)
                frame.GetEventHandler().ProcessEvent(event)
                assert frame.working and isinstance(frame.progress_view.tracker, ProgressTracker)
                assert frame.progress_view.tracker.snapshot().percent != 100

            def begin_failure(exc):
                if phase[0] != 'cleanup':
                    failures.append(type(exc).__name__ + ': ' + str(exc)[:2000])
                    cleanup_deadline[0] = monotonic() + 10
                    phase[0] = 'cleanup'
                frame.cancel_event.set()

            def cancel_from_worker_launch():
                if phase[0] != 'cancel':
                    return
                try:
                    assert frame.working and not cancel_delivered[0]
                    press(frame.cancel_button)
                    assert frame.cancel_event.is_set()
                    cancel_delivered[0] = True
                except BaseException as exc:
                    begin_failure(exc)

            def audit(event, arguments):
                # CPython reports a command-line string on Windows. This hook
                # observes Popen; it never substitutes the supervisor or child.
                if event != 'subprocess.Popen':
                    return
                command = arguments[1]
                if not isinstance(command, str):
                    command = ' '.join(str(part) for part in command)
                if ('carr_library.ingest.worker' not in command or
                        'runpy.run_module(' not in command):
                    return
                launches.append(phase[0])
                if phase[0] == 'cancel' and not cancel_scheduled[0]:
                    cancel_scheduled[0] = True
                    wx.CallAfter(cancel_from_worker_launch)

            sys.addaudithook(audit)

            def verify_outputs():
                delivered = {}
                for name, (source, raw) in expected.items():
                    text_path = outputs / name
                    report_path = outputs / (name + '.provenance.json')
                    assert text_path.read_bytes() == raw
                    report_raw = report_path.read_bytes()
                    report = json.loads(report_raw)
                    assert report['schema'] == 'carr-reading-derivative/1'
                    assert report['source_name'] == source.name
                    assert report['source_sha256'] == sha256(original[source]).hexdigest()
                    assert report['source_size'] == len(original[source])
                    assert report['text_sha256'] == sha256(raw).hexdigest()
                    assert report['text_size'] == len(raw)
                    assert report['verified_for_quotation'] is False
                    assert report['original_modified'] is False
                    assert report['printed_pages_reconstructed'] is False
                    text = raw.decode('utf-8')
                    assert report['segments']
                    for index, segment in enumerate(report['segments'], 1):
                        start, end = segment['output_characters']
                        assert segment['ordinal'] == index
                        assert 0 <= start <= end <= len(text)
                        assert segment['text_sha256'] == sha256(text[start:end].encode('utf-8')).hexdigest()
                        assert segment['printed_label'] is None
                        assert segment['printed_label_verified'] is False
                    delivered[name] = sha256(raw).hexdigest()
                    delivered[report_path.name] = sha256(report_raw).hexdigest()
                assert {path.name for path in outputs.iterdir()} == set(delivered)
                return delivered

            def close_result_dialogs():
                for dialog in completion_dialogs():
                    press(wx.FindWindowById(wx.ID_CANCEL, dialog))

            def drive(event):
                try:
                    if phase[0] == 'cleanup':
                        close_result_dialogs()
                        if not frame.working and not completion_dialogs():
                            timer.Stop()
                            app.ExitMainLoop()
                        elif monotonic() >= cleanup_deadline[0]:
                            force_failure()
                        return
                    if monotonic() > deadline:
                        raise TimeoutError('Native conversion flow exceeded its bounded wait.')
                    if phase[0] == 'ready':
                        phase[0] = 'success'
                        invoke_menu()
                    elif phase[0] == 'success':
                        dialogs = completion_dialogs()
                        if dialogs:
                            assert len(dialogs) == 1 and not completion_dialog_seen[0]
                            dialog = dialogs[0]
                            reader = wx.FindWindowByName('Document conversion complete', dialog)
                            assert isinstance(reader, wx.TextCtrl) and not reader.IsEditable()
                            shown = reader.GetValue()
                            assert 'Created 2 reading file(s).' in shown
                            assert 'unverified for quotation' in shown
                            assert 'Original documents were preserved' in shown
                            assert all(str(outputs / name) in shown for name in expected)
                            completion_dialog_seen[0] = True
                            press(wx.FindWindowById(wx.ID_CANCEL, dialog))
                            return
                        if not frame.working:
                            assert completion_dialog_seen[0]
                            measured = frame.progress_view.tracker.snapshot()
                            assert (measured.state, measured.percent, measured.completed, measured.total) == (
                                'completed', 100, 2, 2)
                            assert frame.progress_view.gauge.GetValue() == 100
                            assert 'Complete' in frame.progress_view.status.GetValue()
                            assert launches.count('success') == 2
                            final['derivative_hashes'] = verify_outputs()
                            phase[0] = 'cancel'
                            invoke_menu()
                    elif phase[0] == 'cancel' and not frame.working:
                        assert cancel_scheduled[0] and cancel_delivered[0]
                        assert frame.cancel_event.is_set()
                        assert launches.count('cancel') == 1
                        measured = frame.progress_view.tracker.snapshot()
                        assert measured.state == 'cancelled' and measured.percent != 100
                        assert frame.progress_view.gauge.GetValue() != 100
                        assert 'Cancelled' in frame.progress_view.status.GetValue()
                        assert not list(cancelled_output.iterdir())
                        assert not completion_dialogs()
                        assert verify_outputs() == final['derivative_hashes']
                        assert all(path.read_bytes() == raw for path, raw in original.items())
                        assert sha256(database.read_bytes()).hexdigest() == catalog_before
                        assert choices == destroyed == [
                            ('success', 'files'), ('success', 'folder'),
                            ('cancel', 'files'), ('cancel', 'folder')]
                        assert not network_attempts
                        phase[0] = 'complete'
                        timer.Stop()
                        app.ExitMainLoop()
                except BaseException as exc:
                    begin_failure(exc)

            frame.Bind(wx.EVT_TIMER, drive, timer)
            timer.Start(25)
            try:
                app.MainLoop()
            finally:
                timer.Stop()
                frame.cancel_event.set()
                frame._closing = True
                try:
                    # The watchdog remains active through real supervisor and
                    # executor shutdown. No user process or environment is killed.
                    frame.executor.shutdown(wait=True, cancel_futures=True)
                    frame.progress_view.close()
                finally:
                    wx.FileDialog, wx.DirDialog = native_file_dialog, native_dir_dialog
                    frame.Destroy()
            if failures or phase[0] != 'complete':
                raise RuntimeError('Native conversion probe failed: ' + '; '.join(failures or ['incomplete flow']))
            final.update(
                schema='carr-native-conversion-smoke/1', status='PASS', windows_execution='PASS',
                source_commit=manifest['source_commit'], source_hashes=hashes,
                installed_package=str(installed_root),
                desktop_worker='actual DesktopFrame.run_job and ConversionService',
                real_supervised_reader_launches={'completed': 2, 'cancelled': 1},
                native_controls=True, menu_dispatch='native wx command event',
                cancellation='native Cancel button after real supervisor launch; no completed pair',
                progress='actual shared tracker and native gauge; complete=100, cancelled<100',
                successful_formats=['.txt', '.html'], fixture_kind='public synthetic local documents',
                sources_unchanged=True, library_catalog_unchanged=True,
                quotation_authority=False, file_folder_selections='controlled synthetic choices',
                network='parent connections denied; existing worker denies connections',
                manual_file_folder_dialogs='NOT_RUN', manual_keyboard_navigation='NOT_RUN',
                nvda='NOT_RUN',
            )
        for name, digest in hashes.items():
            if (sha256((source_root / name).read_bytes()).hexdigest() != digest or
                    sha256((installed_root / name).read_bytes()).hexdigest() != digest):
                raise ValueError('Conversion acceptance source changed during native execution.')
        if manifest_path.read_bytes() != manifest_raw:
            raise ValueError('The sealed bundle manifest changed during acceptance.')
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open('x', encoding='utf-8') as report:
            report.write(json.dumps(final, ensure_ascii=False, indent=2) + '\n')
        print(json.dumps(final, ensure_ascii=False), flush=True)
        return 0
    finally:
        watchdog.cancel()


if __name__ == '__main__':
    raise SystemExit(main())

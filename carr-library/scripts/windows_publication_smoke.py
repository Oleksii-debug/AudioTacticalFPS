"""Installed native Audio menu, current renderer, EPUB/DAISY export and Cancel.

Chooser answers alone are controlled. DesktopFrame, menu command dispatch,
PublicationService, FFmpeg/ffprobe, executor, receipts and ProgressTracker are
the actual installed implementation. Public synthetic PCM is not a speech,
microphone, physical playback, keyboard, reader or NVDA qualification.

Run with the installed interpreter, independent of the current directory:
  .venv/Scripts/python.exe -I -B scripts/windows_publication_smoke.py --bundle ROOT --output NEW_REPORT
"""
from hashlib import sha256
import importlib.util
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if sys.platform != 'win32':
        print(json.dumps({'status': 'NOT_RUN', 'windows_execution': 'NOT_RUN'}))
        return 77
    if not __debug__:
        raise ValueError('Run this acceptance without Python optimization.')
    if os.path.lexists(args.output):
        raise FileExistsError('Choose a new acceptance report path.')
    bundle = args.bundle.resolve(strict=True)
    configured = os.environ.get('CARR_PROGRAM_DIR')
    if configured and Path(configured).resolve(strict=True) != bundle:
        raise ValueError('The selected bundle differs from the active program directory.')
    os.environ['CARR_PROGRAM_DIR'] = str(bundle)
    import carr_library
    installed_root = Path(carr_library.__file__).resolve().parent
    if not installed_root.is_relative_to(bundle / '.venv'):
        raise ValueError('Use this selected bundle\'s actual installed interpreter.')
    manifest_path = bundle / 'bundle-manifest.json'
    manifest_raw = manifest_path.read_bytes()
    manifest = json.loads(manifest_raw)
    from carr_library.native_runtime import activate_bundled_runtime
    native_handle = activate_bundled_runtime(bundle_root=bundle)
    if native_handle is None:
        raise ValueError('The selected installed native runtime was not activated.')

    import wx
    from dataclasses import replace
    from carr_library.application import LibraryApp
    from carr_library.audio.production import plan_narration, render_project
    from carr_library.audio.publishing import PublicationService, inspect_epub
    from carr_library.audio.daisy import inspect_daisy
    from carr_library.progress import ProgressTracker
    from carr_library.ui.desktop_app import DesktopFrame

    specification = importlib.util.spec_from_file_location('native_publication_fixture',
        Path(__file__).with_name('publication_acceptance_support.py'))
    fixture = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(fixture)
    ffmpeg, runtime_evidence, unchanged_runtime = fixture.runtime(require_installed=True)
    source_root = Path(__file__).resolve().parents[1] / 'src' / 'carr_library'
    modules = ('audio/models.py', 'audio/jobs.py', 'audio/production.py', 'audio/ffmpeg.py',
        'audio/runtime.py', 'audio/publication_source.py', 'audio/publication_io.py',
        'audio/publishing.py', 'audio/daisy.py', 'research/export_file.py',
        'operations/process.py', 'operations/broker.py', 'progress.py',
        'ui/progress_view.py', 'ui/audio_publication.py', 'ui/audio_studio.py', 'ui/desktop_app.py')
    hashes = {}
    for name in modules:
        raw = (source_root / name).read_bytes()
        if (installed_root / name).read_bytes() != raw:
            raise ValueError('Installed publication source differs from this acceptance checkout: ' + name)
        hashes[name] = sha256(raw).hexdigest()

    failures, network_attempts, final = [], [], {}
    def offline(*args, **kwargs):
        network_attempts.append(True)
        raise AssertionError('Network is disabled in native publication acceptance.')
    socket.getaddrinfo = socket.create_connection = offline
    socket.socket.connect = socket.socket.connect_ex = offline

    def force_failure():
        try:
            print(json.dumps({'schema': 'carr-native-publication-smoke/1', 'status': 'FAIL',
                'windows_execution': 'FAIL', 'forced_process_exit': True,
                'error': 'Publication acceptance exceeded its owned process cleanup deadline.',
                'failures': failures, 'nvda': 'NOT_RUN'}, ensure_ascii=False), flush=True)
        finally:
            os._exit(1)

    watchdog = Timer(120, force_failure)
    watchdog.daemon = True
    watchdog.start()
    try:
        app = wx.App(False)
        with TemporaryDirectory(prefix='carr-native-publication-') as temporary:
            root = Path(temporary).resolve(strict=True)
            project, output_root = root / 'audio-project', root / 'exports'
            output_root.mkdir()
            library = LibraryApp(root / 'library')
            original = '  [pause 2]\r\nExact literal <source> & words.\fУкраїнський другий сегмент.\r\n'
            plan = replace(plan_narration(original, title='Public native publication fixture'),
                           sample_rate=24000, export_stems=False)
            rendered = render_project(plan, project, engine=fixture.SyntheticTones(), ffmpeg=ffmpeg)
            assert rendered['status'] == 'complete'
            before = {path.relative_to(project).as_posix(): fixture.digest(path)
                      for path in project.rglob('*') if path.is_file() and not path.name.endswith('.lock')}
            service = PublicationService(project, private_root=library.root, ffmpeg=ffmpeg)
            frame = DesktopFrame(library)
            frame.Show()
            database = library.root / 'library.sqlite'
            catalog_before = sha256(database.read_bytes()).hexdigest()
            native_dialogs = wx.DirDialog, wx.SingleChoiceDialog, wx.TextEntryDialog, wx.FileDialog
            phase, cleanup_deadline = ['ready'], [None]
            choices, destroyed, launches = [], [], []
            cancel_scheduled, cancel_delivered = [False], [False]
            records, publication_hashes = {}, {}
            outputs = {'epub': output_root / 'Київ.epub', 'daisy': output_root / 'Київ.zip',
                       'cancel': output_root / 'cancelled.epub'}
            deadline = monotonic() + 80
            timer = wx.Timer(frame)

            class Selection:
                def __init__(self, parent, message, *positional, kind, **options):
                    assert parent is frame
                    assert phase[0] in ('epub', 'daisy', 'verify-epub', 'verify-daisy', 'cancel')
                    self.kind, self.phase, self.message = kind, phase[0], message
                    self.positional, self.options = positional, options
                    self.selection, self.name = 0, None
                    if kind == 'folder':
                        assert message == 'Choose an Audio Studio output folder containing completed renders'
                        assert options['style'] & wx.DD_DIR_MUST_EXIST
                    elif kind == 'choice':
                        title, rows = positional
                        assert rows
                        if title == 'Completed production':
                            assert len(rows) == 1 and rendered['run_key'] in rows[0]
                        elif title == 'Publication format':
                            assert len(rows) == 2 and 'EPUB 3' in rows[0] and 'DAISY 2.02' in rows[1]
                            self.selection = 1 if self.phase == 'daisy' else 0
                        elif title == 'Saved publication receipt':
                            expected = records[self.phase.removeprefix('verify-')]['id']
                            self.selection = next(i for i, row in enumerate(rows) if expected in row)
                        else:
                            raise AssertionError('Unexpected publication choice: ' + title)
                    elif kind == 'text':
                        assert positional[0] in ('Publication language', 'Publication creator', 'DAISY publisher')
                    else:
                        assert kind == 'file'
                        if self.phase.startswith('verify-'):
                            assert options['style'] & wx.FD_OPEN and options['style'] & wx.FD_FILE_MUST_EXIST
                        else:
                            assert options['style'] & wx.FD_SAVE
                    choices.append((self.phase, self.kind))
                def SetName(self, value):
                    assert isinstance(value, str) and value
                    self.name = value
                def ShowModal(self):
                    assert self.name
                    return wx.ID_OK
                def GetPath(self):
                    if self.kind == 'folder':
                        return str(project)
                    assert self.kind == 'file'
                    return str(outputs[self.phase.removeprefix('verify-')])
                def GetSelection(self):
                    assert self.kind == 'choice'
                    return self.selection
                def GetValue(self):
                    assert self.kind == 'text'
                    return 'uk' if self.positional[0] == 'Publication language' else 'Public synthetic fixture'
                def Destroy(self):
                    destroyed.append((self.phase, self.kind))

            wx.DirDialog = lambda parent, message, **options: Selection(parent, message, kind='folder', **options)
            wx.SingleChoiceDialog = lambda parent, message, *pos, **options: Selection(parent, message, *pos, kind='choice', **options)
            wx.TextEntryDialog = lambda parent, message, *pos, **options: Selection(parent, message, *pos, kind='text', **options)
            wx.FileDialog = lambda parent, message, **options: Selection(parent, message, kind='file', **options)

            def press(control):
                assert isinstance(control, wx.Button) and control.IsEnabled()
                event = wx.CommandEvent(wx.EVT_BUTTON.typeId, control.GetId())
                event.SetEventObject(control)
                control.GetEventHandler().ProcessEvent(event)

            def menu(verification=False):
                expected = ('Verify a saved audio publication...' if verification else
                            'Publish completed audio as EPUB or DAISY...')
                items = []
                bar = frame.GetMenuBar()
                for i in range(bar.GetMenuCount()):
                    items.extend(item for item in bar.GetMenu(i).GetMenuItems()
                                 if item.GetItemLabelText() == expected)
                assert len(items) == 1 and items[0].IsEnabled()
                event = wx.CommandEvent(wx.EVT_MENU.typeId, items[0].GetId())
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

            def cancel_after_native_launch():
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
                if event != 'subprocess.Popen':
                    return
                command = arguments[1]
                if not isinstance(command, str):
                    command = ' '.join(str(part) for part in command)
                for name in ('ffmpeg', 'ffprobe'):
                    if str(runtime_evidence['audio_binaries'][name]['path']).casefold() in command.casefold():
                        launches.append({'phase': phase[0], 'tool': name})
                        if phase[0] == 'cancel' and not cancel_scheduled[0]:
                            cancel_scheduled[0] = True
                            wx.CallAfter(cancel_after_native_launch)
                        break

            sys.addaudithook(audit)

            def verify_book(kind):
                selected = outputs[kind]
                record, = [row for row in service.publications() if Path(row['output']) == selected]
                assert record['status'] == 'complete'
                proof = (inspect_epub if kind == 'epub' else inspect_daisy)(selected)
                assert proof['original_script'] == original
                assert proof['source_display_encoding'] == 'json-string'
                assert proof['source']['approved_library_evidence'] is False
                assert proof['source']['frame_intervals'] == record['source']['frame_intervals']
                assert proof['audio']['codec'] == 'mp3' and not proof['audio']['reused']
                assert fixture.digest(selected) == record['artifact']['sha256']
                assert service.verify(record['id']) == record
                records[kind] = record
                publication_hashes[kind] = record['artifact']['sha256']

            def completed():
                measured = frame.progress_view.tracker.snapshot()
                assert measured.state == 'completed' and measured.percent == 100
                assert frame.progress_view.gauge.GetValue() == 100
                assert 'Complete' in frame.progress_view.status.GetValue()

            def drive(event):
                try:
                    if phase[0] == 'cleanup':
                        if not frame.working:
                            timer.Stop()
                            app.ExitMainLoop()
                        elif monotonic() >= cleanup_deadline[0]:
                            force_failure()
                        return
                    if monotonic() > deadline:
                        raise TimeoutError('The native publication sequence exceeded its bounded wait.')
                    if phase[0] == 'ready':
                        phase[0] = 'epub'
                        menu()
                    elif phase[0] in ('epub', 'daisy'):
                        kind = phase[0]
                        if not frame.working and outputs[kind].exists() and frame.status.GetValue().startswith('Publication saved:'):
                            completed()
                            verify_book(kind)
                            assert any(item['phase'] == kind and item['tool'] == 'ffmpeg' for item in launches)
                            assert any(item['phase'] == kind and item['tool'] == 'ffprobe' for item in launches)
                            phase[0] = 'daisy' if kind == 'epub' else 'verify-epub'
                            menu(phase[0].startswith('verify-'))
                    elif phase[0].startswith('verify-'):
                        kind = phase[0].removeprefix('verify-')
                        if not frame.working and frame.status.GetValue().startswith('Publication bytes and source provenance verified:'):
                            completed()
                            assert fixture.digest(outputs[kind]) == publication_hashes[kind]
                            phase[0] = 'verify-daisy' if kind == 'epub' else 'cancel'
                            menu(phase[0].startswith('verify-'))
                    elif phase[0] == 'cancel' and cancel_delivered[0] and not frame.working:
                        measured = frame.progress_view.tracker.snapshot()
                        assert cancel_scheduled[0] and frame.cancel_event.is_set()
                        assert measured.state == 'cancelled' and measured.percent != 100
                        assert frame.progress_view.gauge.GetValue() != 100
                        assert 'Cancelled' in frame.progress_view.status.GetValue()
                        assert not outputs['cancel'].exists()
                        assert len(service.publications()) == 2
                        for kind in ('epub', 'daisy'):
                            assert fixture.digest(outputs[kind]) == publication_hashes[kind]
                        assert not list(output_root.glob('.pending-*'))
                        assert all(fixture.digest(project / name) == identity for name, identity in before.items())
                        assert sha256(database.read_bytes()).hexdigest() == catalog_before
                        assert choices == destroyed and choices
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
                    frame.executor.shutdown(wait=True, cancel_futures=True)
                    frame.progress_view.close()
                finally:
                    wx.DirDialog, wx.SingleChoiceDialog, wx.TextEntryDialog, wx.FileDialog = native_dialogs
                    frame.Destroy()
            if failures or phase[0] != 'complete':
                raise RuntimeError('Native publication probe failed: ' + '; '.join(failures or ['incomplete flow']))
            unchanged_runtime()
            final.update(schema='carr-native-publication-smoke/1', status='PASS', windows_execution='PASS',
                source_commit=manifest['source_commit'], source_hashes=hashes, **runtime_evidence,
                installed_package=str(installed_root), native_controls=True,
                menu_dispatch='actual native wx command events for export and verification',
                desktop_worker='actual DesktopFrame.run_job, ProductionPlan renderer and PublicationService',
                actual_native_process_launches=launches, publication_sha256=publication_hashes,
                cancellation='actual Cancel button scheduled after an observed native publication launch',
                progress='actual shared tracker and gauge: completed=100, cancelled<100',
                source_unchanged=True, library_catalog_unchanged=True, quotation_authority=False,
                chooser_answers='controlled public synthetic selections',
                real_network='connections denied', external_validators='SEPARATE_GATE',
                physical_microphone='NOT_RUN', physical_playback='NOT_RUN',
                manual_dialogs='NOT_RUN', manual_keyboard='NOT_RUN',
                reader_interoperability='NOT_RUN', speech_quality='NOT_RUN', nvda='NOT_RUN')
        for name, identity in hashes.items():
            assert sha256((source_root / name).read_bytes()).hexdigest() == identity
            assert sha256((installed_root / name).read_bytes()).hexdigest() == identity
        assert manifest_path.read_bytes() == manifest_raw
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open('x', encoding='utf-8') as stream:
            stream.write(json.dumps(final, ensure_ascii=False, indent=2) + '\n')
        print(json.dumps(final, ensure_ascii=False), flush=True)
        return 0
    finally:
        watchdog.cancel()
        native_handle.close()


if __name__ == '__main__':
    raise SystemExit(main())

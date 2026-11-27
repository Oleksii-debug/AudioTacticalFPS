"""Real wx controls and the existing isolated worker; synthetic ASR protocol only."""
import importlib.util
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import traceback
from unittest.mock import patch
import venv

import wx

from carr_library.application import LibraryApp
from carr_library.durable_state import read_json
from carr_library.ui.desktop_app import DesktopFrame
from carr_library.ui.transcription_dialog import show_transcription

source_root = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('transcription_protocol_fixture',
    source_root / 'tests' / 'transcription' / 'test_supplied_worker_bridge.py')
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
v5_spec = importlib.util.spec_from_file_location('transcription_v5_protocol_fixture',
    source_root / 'tests' / 'transcription' / 'test_v5_worker_profile.py')
v5_fixture = importlib.util.module_from_spec(v5_spec)
v5_spec.loader.exec_module(v5_fixture)
desktop = wx.App(False)
# Keep the real outer event loop alive until the observed frame is destroyed.
desktop.SetExitOnFrameDelete(False)
with TemporaryDirectory(prefix='transcription-native-') as temporary:
    root = Path(temporary).resolve()
    library = LibraryApp(root / 'library')
    workspace = library.create_workspace('Native transcription controls')
    service = library.transcription(workspace)
    models = root / 'models'
    models.mkdir()
    # Both admitted peers use only the standard library. Keep wx/application
    # imports in this installed host; select a real private peer interpreter
    # through the same visible runtime-inspection and trust controls below.
    environment = root / 'selected-python'
    venv.EnvBuilder(with_pip=False, system_site_packages=False,
                    symlinks=os.name != 'nt').create(environment)
    python = environment / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    worker = root / 'synthetic-worker.py'
    worker.write_text(fixture.SYNTHETIC_WORKER.replace('__MODE__', repr('complete')), encoding='utf-8')
    v5_runtime = root / 'synthetic-v5-runtime'
    v5_runtime.mkdir()
    v5_worker = v5_runtime / 'worker.py'
    v5_worker.write_text(v5_fixture.SYNTHETIC_V5_WORKER, encoding='utf-8')
    for name in ('local_models.py', 'transcript_features.py'):
        (v5_runtime / name).write_text('# Synthetic V5 protocol fixture only.\n', encoding='utf-8')
    recordings = root / 'recordings'
    (recordings / 'nested').mkdir(parents=True)
    original = recordings / 'nested' / 'Запис.wav'
    original.write_bytes(b'Synthetic protocol audio placeholder; no acoustic inference.')
    sidecar = recordings / 'notes.txt'
    sidecar.write_text('Excluded synthetic nonmedia sidecar.', encoding='utf-8')
    export_parent = root / 'exports'
    export_parent.mkdir()

    class FolderAnswer:
        """Control only the picker answer; the native button and service still run."""
        directory = recordings
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def ShowModal(self): return wx.ID_OK
        def GetPath(self): return str(self.directory)

    class ExportAnswer(FolderAnswer):
        directory = export_parent

    close_observations = []

    def record_close(stage, window, *, inspect_dirty=True, **details):
        # Read-only, bounded diagnostics: never alter veto, busy state or lifetime.
        row = {'stage': stage, 'working': getattr(window, 'working', None),
               'busy': getattr(window, 'busy', None),
               '_closing': getattr(window, '_closing', None)}
        try:
            row['wrapper_live'] = bool(window)
            if inspect_dirty and row['wrapper_live']:
                row['dirty'] = window._dirty()
        except Exception as exc:
            row['inspection_error'] = type(exc).__name__ + ': ' + str(exc)
        row.update(details)
        close_observations.append(row)
        del close_observations[:-32]

    class ObservedDesktopFrame(DesktopFrame):
        def on_close(self, event):
            record_close('handler-enter', self, event_id=event.GetId(),
                event_object_is_frame=event.GetEventObject() is self,
                can_veto=event.CanVeto(), vetoed=event.GetVeto())
            try:
                return super().on_close(event)
            finally:
                record_close('handler-exit', self, can_veto=event.CanVeto(),
                    vetoed=event.GetVeto(), skipped=event.GetSkipped())

    frame = ObservedDesktopFrame(library)
    frame.Show()
    captured_frame_id = frame.GetId()
    destroyed = []
    def observe_destroy(event):
        record_close('destroy-event', frame, inspect_dirty=False,
            captured_frame_id=captured_frame_id, event_id=event.GetId(),
            event_object_is_frame=event.GetEventObject() is frame)
        if event.GetEventObject() is frame:
            destroyed.append(True)
            wx.CallAfter(desktop.ExitMainLoop)
        event.Skip()
    frame.Bind(wx.EVT_WINDOW_DESTROY, observe_destroy)
    failures, state = [], {'phase': 0, 'finished': False, 'job': None}
    started = time.monotonic()

    def click(control):
        event = wx.CommandEvent(wx.EVT_BUTTON.typeId, control.GetId())
        event.SetEventObject(control)
        control.GetEventHandler().ProcessEvent(event)

    def drive():
        dialog = next((window for window in wx.GetTopLevelWindows()
            if isinstance(window, wx.Dialog) and window.GetTitle() == 'Local transcription workstation'), None)
        try:
            assert time.monotonic() - started < 120, 'Native transcription workflow timed out.'
            if dialog is None or dialog.pending or frame.working:
                wx.CallLater(50, drive)
                return

            def find(name):
                control = wx.FindWindowByName(name, dialog)
                assert control is not None, 'Missing named control: ' + name
                return control

            def choose_protocol(index, chosen_worker, name):
                dialog.pages.SetSelection(1)
                protocol = find('Exact reviewed worker protocol')
                assert isinstance(protocol, wx.Choice)
                protocol.SetSelection(index)
                expected = ('receipts-v1', 'whisper-v5-manifest-v1')[index]
                assert protocol.GetStringSelection() == expected
                event = wx.CommandEvent(wx.EVT_CHOICE.typeId, protocol.GetId())
                event.SetEventObject(protocol)
                protocol.GetEventHandler().ProcessEvent(event)
                assert dialog.inspection is None
                find('Runtime profile name').SetValue(name)
                find('Python executable').SetValue(str(python))
                find('Reviewed transcription worker').SetValue(str(chosen_worker))
                find('Existing local model directory').SetValue(str(models))
                assert not find('Explicit trust of inspected runtime, worker and model files').GetValue()
                click(find('Inspect files'))
                assert frame.working

            def trust_inspected(protocol):
                details = json.loads(find('Runtime inspection and fingerprint').GetValue())
                assert details['protocol'] == protocol and details['fingerprint']
                assert details['inference_qualified'] is False
                inventory = details['runtime_inventory']
                assert inventory['python_executable'] == str(python.absolute())
                assert inventory['python_configuration']
                assert all(row == {'present': False}
                           for row in inventory['runtime_distributions'].values())
                assert not list(library.root.rglob('job_status.json'))
                assert service.list() == ()
                # Registration cannot be admitted until the real trust control is checked.
                count = len(service.list_runtimes())
                click(find('Register trusted runtime'))
                assert not frame.working and len(service.list_runtimes()) == count
                find('Explicit trust of inspected runtime, worker and model files').SetValue(True)
                click(find('Register trusted runtime'))
                assert frame.working

            if state['phase'] == 0:
                assert frame.active[0] == workspace and service.list() == ()
                assert find('Saved transcription jobs').GetCount() == 0
                assert find('Transcription source result').GetCount() == 0
                assert find('Retain available word timestamps').GetValue()
                assert not find('Pause at checkpoint').IsEnabled()
                assert not find('Transcript reading copy').IsEnabled()
                assert not find('Transcription task').IsEnabled()
                assert 'unsupported' in find('Transcription task').GetStringSelection()
                choose_protocol(1, v5_worker, 'Explicit synthetic V5 profile')
                state['phase'] = 'inspect-v5'
            elif state['phase'] == 'inspect-v5':
                trust_inspected('whisper-v5-manifest-v1')
                state['phase'] = 'registered-v5'
            elif state['phase'] == 'registered-v5':
                profile = next(row for row in service.list_runtimes()
                               if row['configuration']['protocol'] == 'whisper-v5-manifest-v1')
                configured = profile['configuration']
                assert set(configured['wrapper_sources_sha256']) == {
                    'worker.py', 'local_models.py', 'transcript_features.py'}
                assert profile['trusted'] and profile['qualification'] == 'unqualified'
                assert 'whisper-v5-manifest-v1' in find('Registered transcription runtime').GetStringSelection()
                assert not find('Include text headers; unsupported by V5').IsEnabled()
                choose_protocol(0, worker, 'Explicit synthetic receipts profile')
                state['phase'] = 'inspect-receipts'
            elif state['phase'] == 'inspect-receipts':
                trust_inspected('receipts-v1')
                state['phase'] = 'registered-receipts'
            elif state['phase'] == 'registered-receipts':
                profile = dialog.profiles[find('Registered transcription runtime').GetSelection()]
                assert profile['configuration']['protocol'] == 'receipts-v1' and profile['trusted']
                assert len(service.list_runtimes()) == 2
                state['profile'] = profile['id']
                dialog.pages.SetSelection(0)
                find('Job title').SetValue('Native transcript workflow')
                find('Include subfolders of selected folders').SetValue(True)
                with patch.object(wx, 'DirDialog', FolderAnswer):
                    click(find('Add folder…'))
                assert not frame.working and service.list() == ()
                assert str(recordings) in find('Selected local audio or video').GetValue()
                click(find('Prepare saved job'))
                assert frame.working
                state['phase'] = 'retained'
            elif state['phase'] == 'retained':
                job, = service.list()
                assert job['status'] == 'ready' and len(job['sources']) == 1
                assert job['runtime_profile_id'] == state['profile']
                assert job['sources'][0]['original_name'] == 'nested/Запис.wav'
                assert job['artifacts'] == []
                state['job'] = job['id']
                assert find('Saved transcription jobs').GetCount() == 1
                assert find('Transcription source result').GetCount() == 1
                notice = find('Transcription progress and result').GetValue()
                assert 'Retained files: 1;' in notice and 'unsupported files excluded: 1.' in notice
                assert 'No inference has run.' in notice
                click(find('Pause at checkpoint'))
                assert frame.working
                state['phase'] = 'paused'
            elif state['phase'] == 'paused':
                assert service.get(state['job'])['status'] == 'paused'
                click(find('Cancel operation'))
                assert frame.working
                state['phase'] = 'cancelled'
            elif state['phase'] == 'cancelled':
                assert service.get(state['job'])['status'] == 'cancelled'
                click(find('Start / resume'))
                assert frame.working
                state['phase'] = 'recognized'
            elif state['phase'] == 'recognized':
                job = service.verify(state['job'])
                assert job['status'] == 'completed' and len(job['artifacts']) == 6
                assert not find('Start / resume').IsEnabled()
                assert not find('Pause at checkpoint').IsEnabled()
                dialog.pages.SetSelection(2)
                click(find('Load transcript'))
                assert frame.working
                state['phase'] = 'loaded'
            elif state['phase'] == 'loaded':
                editor = find('Transcript reading copy')
                assert editor.IsEnabled() and 'Український' in editor.GetValue()
                state['recognized'] = editor.GetValue()
                assert 'Unverified recognition derivative' in find('Transcript provenance').GetValue()
                editor.SetFocus()
                editor.SetValue('Виправлена окрема версія.')
                click(find('Undo reading edit'))
                assert editor.GetValue() == state['recognized']
                click(find('Redo reading edit'))
                assert editor.GetValue() == 'Виправлена окрема версія.'
                save = find('Save edits')
                assert save.IsEnabled()
                click(save)
                assert frame.working
                state['phase'] = 'saved'
            elif state['phase'] == 'saved':
                edit, = service.edits(state['job'])
                assert edit['text'] == 'Виправлена окрема версія.'
                original_text = service.text(state['job'], service.get(state['job'])['sources'][0]['filename'])
                assert original_text == state['recognized'] and original_text.startswith('Synthetic transcript.')
                assert original.read_bytes().startswith(b'Synthetic protocol')
                assert sidecar.read_text(encoding='utf-8') == 'Excluded synthetic nonmedia sidecar.'
                assert not find('Save edits').IsEnabled()
                worker.unlink()  # Verified completed results/export are independent of a live engine.
                find('Output SRT').SetValue(False)
                find('Output VTT').SetValue(False)
                with patch.object(wx, 'DirDialog', ExportAnswer):
                    click(find('Export results…'))
                assert frame.working
                state['phase'] = 'exported'
            elif state['phase'] == 'exported':
                destination, = export_parent.iterdir()
                manifest = read_json(destination / 'transcription-manifest.json')
                assert manifest['status'] == 'completed' and len(manifest['artifacts']) == 2
                assert {Path(row['filename']).suffix for row in manifest['artifacts']} == {'.txt', '.json'}
                assert manifest['edits'][0]['text'] == 'Виправлена окрема версія.'
                assert len(service.verify(state['job'])['artifacts']) == 6
                assert not list(destination.rglob('*.srt')) and not list(destination.rglob('*.vtt'))
                state['finished'] = True
                click(find('Close'))
                return
            wx.CallLater(50, drive)
        except BaseException:
            failures.append(traceback.format_exc())
            frame.cancel_event.set()
            if dialog is not None:
                dialog.EndModal(wx.ID_CANCEL)

    closing_watchdogs = []

    def open_dialog():
        try:
            assert desktop.IsMainLoopRunning(), 'Native transcription requires the real outer event loop.'
            show_transcription(frame)
        except BaseException:
            failures.append(traceback.format_exc())
        finally:
            try:
                record_close('cleanup-enter', frame)
                frame.cancel_event.set()
                frame.executor.shutdown(wait=True, cancel_futures=True)
                # Use the actual close hook: tray, trust polling, progress and shared
                # cleanup owners must stop before temporary library removal.
                frame.working = False
                record_close('before-Close', frame)
                close_result = frame.Close()
                record_close('after-Close', frame, returned=close_result)
            except BaseException:
                failures.append(traceback.format_exc())
            finally:
                # Top-level destruction is deferred to idle. Let the same outer
                # loop observe it, retaining the existing five-second limit.
                closing_watchdogs.append(wx.CallLater(5000, desktop.ExitMainLoop))

    wx.CallLater(50, drive)
    wx.CallAfter(open_dialog)
    try:
        desktop.MainLoop()
        if failures:
            raise AssertionError('\n'.join(failures))
        assert state['finished'], 'Native dialog exited before acceptance.'
    finally:
        for watchdog in closing_watchdogs:
            watchdog.Stop()
        record_close('loop-exit', frame, inspect_dirty=False)
        print('ASR_CLOSE_DIAGNOSTICS ' + json.dumps({
            'captured_frame_id': captured_frame_id, 'destroyed': bool(destroyed),
            'observations': close_observations}, ensure_ascii=False), flush=True)
        assert destroyed, 'The real desktop window did not complete destruction.'
print(json.dumps({'status': 'PASS', 'checks': [
    'actual named wx controls', 'shared background operation',
    'actual native protocol choices and explicit trust registration', 'V5 three-source binding',
    'two exact named profiles without global substitution',
    'actual named folder action', 'background folder intake before inference',
    'relative source and exclusion count', 'durable native pause/cancel/resume',
    'actual isolated child protocol', 'six verified formats',
    'separate edit revision and Undo/Redo', 'original unchanged',
    'subset export after engine removal', 'real desktop close and destruction'],
    'folder_picker_interaction': 'NOT_RUN', 'folder_picker_answer': 'controlled synthetic directory',
    'profile_picker_interaction': 'programmatic real wx.Choice selection; manual interaction NOT_RUN',
    'whisper_acoustic_inference': 'NOT_RUN', 'manual_NVDA': 'NOT_RUN'}, ensure_ascii=False))

"""Real wx controls and shared child-process flow; synthetic ASR protocol only."""
import importlib.util
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import time
import traceback
from unittest.mock import patch

import wx

from carr_library.application import LibraryApp
from carr_library.ui.desktop_app import DesktopFrame
from carr_library.ui.transcription_dialog import show_transcription

source_root = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('transcription_protocol_fixture',
    source_root / 'tests' / 'transcription' / 'test_supplied_worker_bridge.py')
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
desktop = wx.App(False)
with TemporaryDirectory(prefix='transcription-native-') as temporary:
    root = Path(temporary).resolve()
    library = LibraryApp(root / 'library')
    workspace = library.create_workspace('Native transcription controls')
    service = library.transcription(workspace)
    models = root / 'models'
    models.mkdir()
    worker = root / 'synthetic-worker.py'
    worker.write_text(fixture.SYNTHETIC_WORKER.replace('__MODE__', repr('complete')), encoding='utf-8')
    service.configure(Path(sys.executable).resolve(), worker, models)
    original = root / 'Запис.wav'
    original.write_bytes(b'Synthetic protocol audio placeholder; no acoustic inference.')
    original_bytes = original.read_bytes()
    export_parent = root / 'exports'
    export_parent.mkdir()
    export_name = 'Експорт перевірених результатів'
    export_destination = export_parent / export_name
    job_id = service.submit([original], title='Native transcript workflow')
    frame = DesktopFrame(library)
    frame.Show()
    failures, state = [], {'phase': 0, 'finished': False}
    started = time.monotonic()
    class AcceptedChooser:
        """Control only picker responses; dispatch and export remain real."""
        def __init__(self, value):
            self.value = value
        def __enter__(self):
            return self
        def __exit__(self, *unused):
            return False
        def ShowModal(self):
            return wx.ID_OK
        def GetPath(self):
            return self.value
        def GetValue(self):
            return self.value
    def click(control):
        event = wx.CommandEvent(wx.EVT_BUTTON.typeId, control.GetId())
        event.SetEventObject(control)
        control.GetEventHandler().ProcessEvent(event)
    def drive():
        dialog = next((window for window in wx.GetTopLevelWindows()
            if isinstance(window, wx.Dialog) and window.GetTitle() == 'Транскрипція — локальний Whisper'), None)
        try:
            assert time.monotonic() - started < 45, 'Native transcription workflow timed out.'
            if dialog is None:
                wx.CallLater(50, drive)
                return
            def find(name):
                control = wx.FindWindowByName(name, dialog)
                assert control is not None, 'Missing named control: ' + name
                return control
            if state['phase'] == 0:
                assert frame.active[0] == workspace
                assert find('Збережені завдання').GetCount() == 1
                assert find('Запис для редагування').GetCount() == 1
                assert find('Часові мітки кожного слова').GetValue()
                assert not find('Зупинити').IsEnabled()
                assert not find('Текст: редагування зберігається окремою версією').IsEnabled()
                click(find('Запустити / продовжити'))
                assert frame.working
                state['phase'] = 1
            elif state['phase'] == 1 and not frame.working:
                job = service.verify(job_id)
                assert job['status'] == 'completed' and len(job['artifacts']) == 6
                editor = find('Текст: редагування зберігається окремою версією')
                assert editor.IsEnabled() and 'Український' in editor.GetValue()
                editor.SetFocus()
                editor.SetValue('Виправлена окрема версія.')
                state['phase'] = 2
            elif state['phase'] == 2:
                save = find('Зберегти правки')
                assert save.IsEnabled()
                click(save)
                state['phase'] = 3
            elif state['phase'] == 3 and not frame.working:
                edit, = service.edits(job_id)
                assert edit['text'] == 'Виправлена окрема версія.'
                original_text = service.text(job_id, service.get(job_id)['sources'][0]['filename'])
                assert original_text.startswith('Synthetic transcript.')
                assert original.read_bytes().startswith(b'Synthetic protocol')
                assert not find('Зберегти правки').IsEnabled()
                job = service.verify(job_id)
                job_root = service._path(job_id)
                state['job'], state['edits'] = job, service.edits(job_id)
                retained_names = ['job.json', 'job_status.json', *job['receipts']]
                retained_names += ['input/' + row['filename'] for row in job['sources']]
                retained_names += ['results/' + row['path'] for row in job['artifacts']]
                retained_names += ['edits/' + row['id'] + '.json' for row in state['edits']]
                state['retained_bytes'] = {
                    name: (job_root / name).read_bytes() for name in retained_names}
                state['export_bytes'] = {
                    row['path']: (job_root / 'results' / row['path']).read_bytes()
                    for row in job['artifacts']}
                state['export_bytes'].update({
                    'edited/' + row['id'] + '.txt': row['text'].encode('utf-8')
                    for row in state['edits']})
                assert not export_destination.exists()
                export_button = find('Експорт')
                assert export_button.IsEnabled()
                export_button.SetFocus()
                with patch.object(wx, 'DirDialog',
                        return_value=AcceptedChooser(str(export_parent))) as directory_picker, \
                     patch.object(wx, 'TextEntryDialog',
                        return_value=AcceptedChooser(export_name)) as name_picker:
                    click(export_button)
                directory_picker.assert_called_once()
                name_picker.assert_called_once()
                assert directory_picker.call_args.args[0] is dialog
                assert name_picker.call_args.args[0] is dialog
                assert frame.working, 'The real Export handler did not schedule its operation.'
                state['phase'] = 4
            elif state['phase'] == 4 and not frame.working:
                manifest = json.loads(
                    (export_destination / 'transcription-manifest.json').read_text(encoding='utf-8'))
                job, edits = state['job'], state['edits']
                assert manifest['version'] == 1
                assert manifest['job'] == job_id and manifest['status'] == 'completed'
                assert manifest['title'] == job['title']
                assert manifest['sources'] == job['sources'] and manifest['settings'] == job['settings']
                assert manifest['edits'] == list(edits)
                assert manifest['artifacts'] == [
                    {'filename': row['path'], 'size_bytes': row['size_bytes'], 'sha256': row['sha256']}
                    for row in job['artifacts']]
                exported_names = {
                    item.relative_to(export_destination).as_posix()
                    for item in export_destination.rglob('*') if item.is_file()}
                assert exported_names == set(state['export_bytes']) | {'transcription-manifest.json'}
                for name, expected in state['export_bytes'].items():
                    assert (export_destination / name).read_bytes() == expected, name
                job_root = service._path(job_id)
                for name, expected in state['retained_bytes'].items():
                    assert (job_root / name).read_bytes() == expected, name
                assert original.read_bytes() == original_bytes
                assert service.verify(job_id) == job and service.edits(job_id) == edits
                assert not list(export_parent.glob('.pending-*'))
                status = find('Стан транскрипції')
                assert status.GetValue() == 'Експорт завершено: ' + str(export_destination)
                assert status.IsEnabled() and find('Експорт').IsEnabled()
                status.SetFocus()
                state['phase'] = 5
            elif state['phase'] == 5:
                assert wx.Window.FindFocus() == find('Стан транскрипції'), \
                    'The completed export message must remain reachable by native focus.'
                state['finished'] = True
                click(find('Закрити'))
                return
            wx.CallLater(50, drive)
        except BaseException:
            failures.append(traceback.format_exc())
            frame.cancel_event.set()
            if dialog is not None:
                dialog.EndModal(wx.ID_CANCEL)
    wx.CallLater(50, drive)
    try:
        show_transcription(frame)
        if failures:
            raise AssertionError('\n'.join(failures))
        assert state['finished'], 'Native dialog exited before acceptance.'
    finally:
        frame.cancel_event.set()
        frame.executor.shutdown(wait=True, cancel_futures=True)
        frame.Destroy()
        desktop.Yield()
print(json.dumps({'status':'PASS', 'checks':['actual named wx controls','shared background operation',
    'actual isolated child protocol','six verified formats','separate edit revision','original unchanged',
    'actual export dispatch preserves verified bytes'],
    'whisper_acoustic_inference':'NOT_RUN', 'manual_NVDA':'NOT_RUN'}, ensure_ascii=False))

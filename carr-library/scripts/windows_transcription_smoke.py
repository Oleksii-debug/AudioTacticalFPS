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
    fixture.synthetic_models(models)
    worker = root / 'synthetic-worker.py'
    worker.write_text(fixture.SYNTHETIC_WORKER.replace('__MODE__', repr('complete')), encoding='utf-8')
    service.configure(Path(sys.executable).resolve(), worker, models)
    recordings = root / 'recordings'
    (recordings / 'nested').mkdir(parents=True)
    original = recordings / 'nested' / 'Запис.wav'
    original.write_bytes(b'Synthetic protocol audio placeholder; no acoustic inference.')
    sidecar = recordings / 'notes.txt'
    sidecar.write_text('Excluded synthetic nonmedia sidecar.', encoding='utf-8')
    class FolderAnswer:
        """Control the picker answer; the actual folder button/service still run."""
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def ShowModal(self): return wx.ID_OK
        def GetPath(self): return str(recordings)
    frame = DesktopFrame(library)
    frame.Show()
    failures, state = [], {'phase': 0, 'finished': False, 'job': None}
    started = time.monotonic()
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
                assert service.list() == ()
                assert find('Збережені завдання').GetCount() == 0
                assert find('Запис для редагування').GetCount() == 0
                assert find('Часові мітки кожного слова').GetValue()
                assert not find('Зупинити').IsEnabled()
                assert not find('Текст: редагування зберігається окремою версією').IsEnabled()
                folder_button = find('Додати папку з підпапками')
                assert folder_button.IsEnabled()
                find('Назва нового завдання').SetValue('Native transcript workflow')
                with patch.object(wx, 'DirDialog', FolderAnswer):
                    click(folder_button)
                assert frame.working
                state['phase'] = 'retaining'
            elif state['phase'] == 'retaining' and not frame.working:
                job, = service.list()
                assert job['status'] == 'ready' and len(job['sources']) == 1
                assert job['sources'][0]['original_name'] == 'nested/Запис.wav'
                state['job'] = job['id']
                assert find('Збережені завдання').GetCount() == 1
                assert find('Запис для редагування').GetCount() == 1
                notice = find('Стан транскрипції').GetValue()
                assert 'Збережено записів: 1.' in notice
                assert 'Непідтримуваних файлів пропущено: 1.' in notice
                click(find('Запустити / продовжити'))
                assert frame.working
                state['phase'] = 1
            elif state['phase'] == 1 and not frame.working:
                job = service.verify(state['job'])
                diagnostic = {key: job.get(key) for key in (
                    'status', 'done', 'failed', 'last_error', 'worker_run_id')}
                diagnostic['artifact_count'] = len(job['artifacts'])
                diagnostic['ui_status'] = find('Стан транскрипції').GetValue()[:3000]
                diagnostic['configured_python'] = str(sys.executable)
                assert job['status'] == 'completed' and len(job['artifacts']) == 6, (
                    'Synthetic native ASR protocol did not complete: ' +
                    json.dumps(diagnostic, ensure_ascii=False, sort_keys=True))
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
                job_id = state['job']
                edit, = service.edits(job_id)
                assert edit['text'] == 'Виправлена окрема версія.'
                original_text = service.text(job_id, service.get(job_id)['sources'][0]['filename'])
                assert original_text.startswith('Synthetic transcript.')
                assert original.read_bytes().startswith(b'Synthetic protocol')
                assert sidecar.read_text(encoding='utf-8') == 'Excluded synthetic nonmedia sidecar.'
                assert not find('Зберегти правки').IsEnabled()
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
    'actual named folder action','background folder intake','relative source and exclusion count',
    'actual isolated child protocol','six verified formats','separate edit revision','original unchanged'],
    'folder_picker_interaction':'NOT_RUN', 'folder_picker_answer':'controlled synthetic directory',
    'whisper_acoustic_inference':'NOT_RUN', 'manual_NVDA':'NOT_RUN'}, ensure_ascii=False))


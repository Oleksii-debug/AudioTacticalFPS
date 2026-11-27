"""Real wx controls and shared child-process flow; synthetic ASR protocol only."""
import importlib.util
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import time
import traceback

import wx

from carr_library.application import LibraryApp
from carr_library.ui.desktop_app import DesktopFrame
from carr_library.ui.transcription_dialog import show_transcription
from carr_library.ui.save_all import coordinator_for

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
    job_id = service.submit([original], title='Native transcript workflow')
    frame = DesktopFrame(library)
    frame.Show()
    coordinator = coordinator_for(frame)
    assert coordinator is not None, 'The composed desktop must install its shared Save All coordinator.'
    library_index = frame.menu_bar.FindMenu('Library')
    assert library_index != wx.NOT_FOUND
    assert any(item.GetItemLabelText() == 'Transcription: local audio and video...'
               for item in frame.menu_bar.GetMenu(library_index).GetMenuItems())
    file_index = frame.menu_bar.FindMenu('File')
    assert file_index != wx.NOT_FOUND
    assert all(item.GetItemLabelText() != 'Transcription: local audio and video...'
               for item in frame.menu_bar.GetMenu(file_index).GetMenuItems())
    failures, state = [], {'phase': 0, 'finished': False}
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
                assert find('Збережені завдання').GetCount() == 1
                assert find('Запис для редагування').GetCount() == 1
                assert find('Часові мітки кожного слова').GetValue()
                assert not find('Зупинити').IsEnabled()
                assert not find('Текст: редагування зберігається окремою версією').IsEnabled()
                save_all = find('Зберегти всі відкриті правки')
                assert save_all.IsEnabled()
                assert dialog.GetClientRect().Contains(save_all.GetRect())
                save_all.SetFocus()
                assert wx.Window.FindFocus() is save_all
                assert any(key.startswith('transcription.') for key in coordinator.participants)
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
                editor = find('Текст: редагування зберігається окремою версією')
                editor.SetFocus()
                editor.SetValue('Друга версія через спільне збереження.')
                state['phase'] = 4
            elif state['phase'] == 4:
                click(find('Зберегти всі відкриті правки'))
                assert coordinator.busy and frame.working
                state['phase'] = 5
            elif state['phase'] == 5 and not frame.working:
                edits = service.edits(job_id)
                assert len(edits) == 2
                assert {edit['text'] for edit in edits} == {
                    'Виправлена окрема версія.', 'Друга версія через спільне збереження.'}
                source = service.get(job_id)['sources'][0]['filename']
                assert service.text(job_id, source).startswith('Synthetic transcript.')
                assert original.read_bytes().startswith(b'Synthetic protocol')
                assert not coordinator.busy and not find('Зберегти правки').IsEnabled()
                assert find('Стан транскрипції').GetValue() == 'All 1 open drafts saved.'
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
        assert not coordinator.busy
        assert not any(key.startswith('transcription.') for key in coordinator.participants)
    finally:
        frame.cancel_event.set()
        frame.executor.shutdown(wait=True, cancel_futures=True)
        frame.Destroy()
        desktop.Yield()
print(json.dumps({'status':'PASS', 'checks':['actual named wx controls','shared background operation',
    'actual isolated child protocol','six verified formats','manual edit revision','original unchanged',
    'shared completion-aware Save All','second separate edit revision','native Save All focus and bounds',
    'semantic Library menu','closed modal participant removed'],
    'whisper_acoustic_inference':'NOT_RUN', 'manual_NVDA':'NOT_RUN'}, ensure_ascii=False))

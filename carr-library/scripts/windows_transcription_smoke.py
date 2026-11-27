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
    ready_deadline = time.monotonic() + 30
    while frame.working and time.monotonic() < ready_deadline:
        desktop.Yield()
        time.sleep(.01)
    assert not frame.working, 'Desktop initialization did not become ready.'
    library_index = next(index for index in range(frame.menu_bar.GetMenuCount())
        if frame.menu_bar.GetMenuLabel(index).replace('&', '') == 'Library')
    transcription_item = next(item for item in frame.menu_bar.GetMenu(library_index).GetMenuItems()
        if item.GetItemLabelText() == 'Transcription: local audio and video...')
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
                assert dialog.GetEscapeId() == find('Закрити').GetId()
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
        event = wx.CommandEvent(wx.EVT_MENU.typeId, transcription_item.GetId())
        event.SetEventObject(frame)
        assert frame.GetEventHandler().ProcessEvent(event), 'Actual transcription menu was not dispatched.'
        if failures:
            raise AssertionError('\n'.join(failures))
        assert state['finished'], 'Native dialog exited before acceptance.'
    finally:
        frame.cancel_event.set()
        frame.executor.shutdown(wait=True, cancel_futures=True)
        frame.Destroy()
        desktop.Yield()
print(json.dumps({'status':'PASS', 'checks':['actual named wx controls','shared background operation',
    'actual isolated child protocol','six verified formats','separate edit revision','original unchanged'],
    'whisper_acoustic_inference':'NOT_RUN', 'manual_NVDA':'NOT_RUN'}, ensure_ascii=False))


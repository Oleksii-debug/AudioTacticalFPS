"""Real wx controls and shared child-process flow; synthetic ASR protocol only."""
import importlib.util
from hashlib import sha256
import json
from pathlib import Path
import sys
import sysconfig
from tempfile import TemporaryDirectory
import time
import traceback
from unittest.mock import patch

import wx

from carr_library.application import LibraryApp
from carr_library.ui.desktop_app import DesktopFrame
from carr_library.ui.transcription_dialog import show_transcription

source_root = Path(__file__).resolve().parents[1]
assert sys.flags.isolated and sys.dont_write_bytecode, 'Run this installed probe with -I -B.'
purelib = Path(sysconfig.get_path('purelib')).resolve()
installed_modules = {}
for name, relative in {
    'carr_library.application': 'application.py',
    'carr_library.transcription': 'transcription.py',
    'carr_library.ui.desktop_app': 'ui/desktop_app.py',
    'carr_library.ui.transcription_dialog': 'ui/transcription_dialog.py',
}.items():
    module = importlib.import_module(name)
    actual = Path(module.__file__).resolve()
    assert actual.is_relative_to(purelib), (name, str(actual), str(purelib))
    expected = source_root / 'src' / 'carr_library' / relative
    digest = sha256(actual.read_bytes()).hexdigest()
    assert digest == sha256(expected.read_bytes()).hexdigest(), name + ' differs from the checked-out source'
    installed_modules[name] = {'relative_path': relative, 'sha256': digest}
spec = importlib.util.spec_from_file_location('transcription_protocol_fixture',
    source_root / 'tests' / 'transcription' / 'test_supplied_worker_bridge.py')
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
v5_spec = importlib.util.spec_from_file_location('transcription_v5_protocol_fixture',
    source_root / 'tests' / 'transcription' / 'test_v5_worker_profile.py')
v5_fixture = importlib.util.module_from_spec(v5_spec)
v5_spec.loader.exec_module(v5_fixture)
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
    class FolderAnswer:
        """Control the picker answer; the actual folder button/service still run."""
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def ShowModal(self): return wx.ID_OK
        def GetPath(self): return str(recordings)
    frame = DesktopFrame(library)
    frame.Show()
    progress_calls = []
    shared_run_job = frame.run_job
    def observed_run_job(label, operation, *args, **kwargs):
        if label == 'Локальна транскрипція Whisper':
            assert kwargs.get('with_progress') is True, 'Transcription must use the shared bound reporter.'
            def measured(progress):
                assert callable(progress)
                def record(done, total, phase):
                    progress(done, total, phase)
                    progress_calls.append((done, total, phase))
                return operation(record)
            return shared_run_job(label, measured, *args, **kwargs)
        return shared_run_job(label, operation, *args, **kwargs)
    frame.run_job = observed_run_job
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
            def configure_profile(index, chosen_worker):
                # Construct the actual native choice dialog, controlling only
                # its answer. Runtime/model pickers also use synthetic answers.
                class ProfileAnswer(wx.SingleChoiceDialog):
                    def ShowModal(self):
                        self.SetSelection(index)
                        button = self.FindWindow(wx.ID_OK)
                        assert button is not None, 'Native profile chooser has no OK button.'
                        wx.CallAfter(click, button)
                        timeout = wx.CallLater(5000, self.EndModal, wx.ID_CANCEL)
                        try:
                            result = super().ShowModal()
                        finally:
                            timeout.Stop()
                        assert result == wx.ID_OK, 'Native profile chooser did not accept the selection.'
                        assert self.GetSelection() == index
                        expected = ('receipts-v1', 'whisper-v5-manifest-v1')[index]
                        assert self.GetStringSelection().startswith(expected)
                        return result
                answers = iter((str(Path(sys.executable).resolve()), str(chosen_worker)))
                class RuntimeAnswer(FolderAnswer):
                    def __init__(self, *args, **kwargs):
                        self.path = next(answers)
                    def GetPath(self): return self.path
                class ModelsAnswer(FolderAnswer):
                    def GetPath(self): return str(models)
                with patch.object(wx, 'SingleChoiceDialog', ProfileAnswer), \
                        patch.object(wx, 'FileDialog', RuntimeAnswer), \
                        patch.object(wx, 'DirDialog', ModelsAnswer):
                    click(find('Підключити Whisper'))
                assert frame.working
            if state['phase'] == 0:
                assert frame.active[0] == workspace
                assert service.list() == ()
                assert find('Збережені завдання').GetCount() == 0
                assert find('Запис для редагування').GetCount() == 0
                assert find('Часові мітки кожного слова').GetValue()
                assert not find('Зупинити').IsEnabled()
                assert not find('Текст: редагування зберігається окремою версією').IsEnabled()
                configure_profile(1, v5_worker)
                state['phase'] = 'configured-v5'
            elif state['phase'] == 'configured-v5' and not frame.working:
                configured = service.configuration(verify=True)
                assert configured['protocol'] == 'whisper-v5-manifest-v1'
                assert set(configured['wrapper_sources_sha256']) == {
                    'worker.py', 'local_models.py', 'transcript_features.py'}
                assert 'whisper-v5-manifest-v1' in find('Стан транскрипції').GetValue()
                configure_profile(0, worker)
                state['phase'] = 'configured-legacy'
            elif state['phase'] == 'configured-legacy' and not frame.working:
                assert service.configuration(verify=True)['protocol'] == 'receipts-v1'
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
                assert job['status'] == 'completed' and len(job['artifacts']) == 6
                assert progress_calls and progress_calls[-1] == (1, 1, 'completed')
                measured = [done for done, _, _ in progress_calls if done is not None]
                assert measured == sorted(measured)
                assert all((done is None) == (total is None) for done, total, _ in progress_calls)
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
print(json.dumps({'status':'PASS', 'installed_package':True, 'installed_modules':installed_modules,
    'checks':['installed purelib origin and exact source hashes','actual named wx controls','shared background operation',
    'token-bound measured progress callback',
    'actual native profile choice and configure action','V5 three-source binding','explicit legacy reconfiguration',
    'actual named folder action','background folder intake','relative source and exclusion count',
    'actual isolated child protocol','six verified formats','separate edit revision','original unchanged'],
    'folder_picker_interaction':'NOT_RUN', 'folder_picker_answer':'controlled synthetic directory',
    'profile_picker_interaction':'NOT_RUN', 'profile_picker_answer':'controlled selections on an actual wx choice dialog',
    'whisper_acoustic_inference':'NOT_RUN', 'manual_NVDA':'NOT_RUN'}, ensure_ascii=False))

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
    class FolderAnswer(wx.Dialog):
        """Control the picker answer; the actual folder button/service still run."""
        def __init__(self, parent, *args, **kwargs):
            super().__init__(parent, title='Controlled synthetic picker answer')
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
            def configure_profile(index, chosen_worker):
                # Construct the actual native choice dialog, controlling only
                # its answer. Runtime/model pickers also use synthetic answers.
                class ProfileAnswer(wx.SingleChoiceDialog):
                    def ShowModal(self):
                        def accept():
                            self.SetSelection(index)
                            button = self.FindWindow(wx.ID_OK)
                            assert button is not None, 'The native profile dialog has no OK action.'
                            click(button)
                        wx.CallAfter(accept)
                        result = super().ShowModal()
                        expected = ('receipts-v1', 'whisper-v5-manifest-v1')[index]
                        assert result == wx.ID_OK and self.GetSelection() == index
                        assert self.GetStringSelection().startswith(expected), self.GetStringSelection()
                        return result
                answers = iter((str(Path(sys.executable).resolve()), str(chosen_worker)))
                class RuntimeAnswer(FolderAnswer):
                    def __init__(self, *args, **kwargs):
                        super().__init__(*args, **kwargs)
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

    # Retain the completed result while exercising the same real service during
    # direct native modal/parent destruction. No model or network is involved.
    completed_id = state['job']
    completed_root = service._path(completed_id)
    def completed_bytes():
        return {path.relative_to(completed_root).as_posix(): path.read_bytes()
                for path in completed_root.rglob('*')
                if path.is_file() and not path.name.endswith('.lock')}
    completed_before = completed_bytes()
    original_before = original.read_bytes()
    pause_worker = root / 'synthetic-pause-worker.py'
    pause_worker.write_text(fixture.SYNTHETIC_WORKER.replace('__MODE__', repr('pause-once')),
                            encoding='utf-8')
    service.configure(Path(sys.executable).resolve(), pause_worker, models)
    lifetime_results = []
    NativeDialog = wx.Dialog
    for target in ('dialog', 'parent'):
        identity = service.submit([original], title='Native lifetime ' + target)
        job_root = service._path(identity)
        owner = DesktopFrame(library)
        owner.Show()
        owned_executor, owned_cancel = owner.executor, owner.cancel_event
        errors, peers, submitted = [], [], []
        real_submit = owned_executor.submit
        def tracked_submit(*args, **kwargs):
            future = real_submit(*args, **kwargs)
            submitted.append(future)
            return future
        observation = {'started': False, 'destroy_requested': False, 'destroy_calls': 0}
        begin_at = time.monotonic()
        class TrackedDialog(NativeDialog):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                if self.GetTitle() == 'Транскрипція — локальний Whisper':
                    peers.append(self)
            def Destroy(self):
                observation['destroy_calls'] += 1
                return super().Destroy()
        def destroy_during_child():
            try:
                assert time.monotonic() - begin_at < 35, 'Native destruction case timed out.'
                if not peers:
                    wx.CallLater(25, destroy_during_child)
                    return
                peer = peers[0]
                if not observation['started']:
                    run = wx.FindWindowByName('Запустити / продовжити', peer)
                    assert run is not None and run.IsEnabled()
                    click(run)
                    assert owner.working
                    observation['started'] = True
                # Metadata existence only: Windows never opens mutable status
                # while the actual supplied-protocol child is still running.
                if not (job_root / 'job_status.json').is_file():
                    wx.CallLater(25, destroy_during_child)
                    return
                assert not (job_root / 'continued').exists()
                observation['destroy_requested'] = True
                if target == 'dialog':
                    peer.Destroy()
                else:
                    owner.Destroy()
            except BaseException:
                errors.append(traceback.format_exc())
                owned_cancel.set()
                if peers and peers[0] and not peers[0].IsBeingDeleted():
                    peers[0].EndModal(wx.ID_CANCEL)
        recorded = None
        with patch.object(wx, 'Dialog', TrackedDialog), \
                patch.object(owned_executor, 'submit', tracked_submit):
            wx.CallLater(25, destroy_during_child)
            try:
                show_transcription(owner, job_id=identity)
                assert len(submitted) == 1, 'Expected the actual running transcription future.'
                # Observe product-owned cancellation before fixture cleanup can
                # signal it. Parent Destroy may already close the executor, so
                # wait on its captured real future rather than submit new work.
                submitted[0].result(timeout=10)
                stopped_before_cleanup = service.get(identity)
                recorded = {**observation, 'errors': tuple(errors),
                    'status_before_cleanup': stopped_before_cleanup['status'],
                    'continued_before_cleanup': (job_root / 'continued').is_file()}
            finally:
                owned_cancel.set()
                owned_executor.shutdown(wait=True, cancel_futures=True)
                # Dispose only surviving peers. Keep the earlier observation
                # copied so cleanup cannot turn a failed case into evidence.
                if peers and peers[0] and not peers[0].IsBeingDeleted():
                    NativeDialog.Destroy(peers[0])
                if owner and not owner.IsBeingDeleted():
                    owner.Destroy()
                desktop.Yield()
        assert recorded is not None and not recorded['errors'], recorded
        assert recorded['started'] and recorded['destroy_requested'], recorded
        assert recorded['destroy_calls'] == (1 if target == 'dialog' else 0), recorded
        assert recorded['status_before_cleanup'] == 'stopped', recorded
        assert recorded['continued_before_cleanup'], recorded
        stopped = service.get(identity)
        assert stopped['status'] == 'stopped', stopped
        assert not stopped['receipts'] and not stopped['artifacts']
        assert (job_root / 'continued').is_file(), 'The actual child did not observe its stop flag.'
        assert (job_root / 'input' / stopped['sources'][0]['filename']).read_bytes() == original_before
        # A later explicit resume uses the same retained job; destruction never
        # starts another inference or marks stopped files as accepted results.
        resumed = service.run(identity, timeout=30)
        assert resumed['status'] == 'completed', resumed
        assert service.verify(identity)['id'] == identity
        assert completed_bytes() == completed_before and original.read_bytes() == original_before
        lifetime_results.append({'owner': target, 'python_destroy_calls': recorded['destroy_calls'],
                                 'child_stopped': True, 'explicit_same_job_resume': True})

print(json.dumps({'status':'PASS', 'checks':['actual named wx controls','shared background operation',
    'actual native profile choice and configure action','V5 three-source binding','explicit legacy reconfiguration',
    'actual named folder action','background folder intake','relative source and exclusion count',
    'actual isolated child protocol','six verified formats','separate edit revision','original unchanged',
    'direct modal and parent destruction with an actual pending child',
    'owned stop and explicit same-job resume without changing historical receipts'],
    'native_lifetime': lifetime_results,
    'folder_picker_interaction':'NOT_RUN', 'folder_picker_answer':'controlled synthetic directory',
    'profile_picker_interaction':'programmatic native selection and OK; manual interaction NOT_RUN',
    'profile_picker_answer':'controlled selections on an actual wx choice dialog',
    'whisper_acoustic_inference':'NOT_RUN', 'manual_NVDA':'NOT_RUN'}, ensure_ascii=False))

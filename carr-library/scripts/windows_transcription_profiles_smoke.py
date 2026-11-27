"""Native controls for the shared ASR runtime/profile adapter.

Synthetic file protocol and model identities only. No private worker, real
model, microphone, acoustic quality or manual NVDA claim is made here.
"""
from hashlib import sha256
import importlib
import importlib.util
import json
import os
from pathlib import Path
import time
import traceback
from unittest.mock import patch


def verify_runtime_profiles(frame, temporary, source_root):
    import wx
    from carr_library.ui.transcription_runtime_dialog import build_transcription_dialog

    source_root, temporary = Path(source_root), Path(temporary)
    origins = {}
    installed = os.environ.get('CARR_PROGRAM_DIR')
    for name in (
        'carr_library.transcription', 'carr_library.backup_transcription',
        'carr_library.integrations.transcription_values',
        'carr_library.integrations.transcription_runtime',
        'carr_library.integrations.transcription_scope',
        'carr_library.integrations.transcription_backup',
        'carr_library.integrations.transcription_protocol',
        'carr_library.integrations.transcription_exports',
        'carr_library.ui.transcription_dialog',
        'carr_library.ui.transcription_runtime_dialog',
    ):
        module = importlib.import_module(name)
        path = Path(module.__file__).resolve(strict=True)
        expected = source_root / 'src' / Path(*name.split('.')).with_suffix('.py')
        assert path.read_bytes() == expected.read_bytes(), (name, path)
        if installed:
            assert path.is_relative_to(Path(installed).resolve(strict=True)), (name, path)
        origins[name] = {'path': str(path), 'sha256': sha256(path.read_bytes()).hexdigest()}

    fixture_path = source_root / 'tests/transcription/fixture_runtime.py'
    spec = importlib.util.spec_from_file_location('native_runtime_inventory_fixture', fixture_path)
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    runtime = fixture.make_runtime(temporary / 'inventoried-synthetic-runtime',
                                   options={'hold_after_checkpoint': 3.0})
    sources = [temporary / 'Перший синтетичний.wav', temporary / 'Другий синтетичний.wav']
    for number, source in enumerate(sources):
        source.write_bytes(b'Synthetic retained source, no acoustic inference: ' + str(number).encode())
    originals = {source: source.read_bytes() for source in sources}
    service = frame.library.transcription(frame.active[0])
    old_legacy = [row['id'] for row in service.list()]
    dialog = build_transcription_dialog(frame)
    state, failures = {'phase': 'initial', 'done': False}, []
    started = time.monotonic()

    def find(name):
        control = wx.FindWindowByName(name, dialog)
        assert control is not None, 'Missing actual named ASR control: ' + name
        return control

    def click(control):
        assert control.IsEnabled(), control.GetName()
        event = wx.CommandEvent(wx.EVT_BUTTON.typeId, control.GetId())
        event.SetEventObject(control)
        control.GetEventHandler().ProcessEvent(event)

    class FilesAnswer:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def ShowModal(self): return wx.ID_OK
        def GetPaths(self): return [str(source) for source in sources]

    class DirectoryAnswer(FilesAnswer):
        def GetPath(self): return str(temporary)

    def drive():
        if state['done']:
            return
        try:
            assert time.monotonic() - started < 150, ('Native ASR profile watchdog expired', state, dialog.status.GetValue())
            phase = state['phase']
            idle = not dialog.pending and not frame.working
            if phase == 'initial' and idle:
                dialog.pages.SetSelection(1)
                dialog.runtime_name.SetValue('Synthetic fully inventoried runtime')
                dialog.python_path.SetValue(str(runtime.python))
                dialog.worker_path.SetValue(str(runtime.worker))
                dialog.model_path.SetValue(str(runtime.model))
                assert not dialog.trust.GetValue()
                click(find('Inspect files'))
                assert dialog.pending and frame.working
                state['phase'] = 'inspected'
            elif phase == 'inspected' and idle:
                assert dialog.inspection is not None, dialog.status.GetValue()
                inspected = dialog.inspection[1]
                assert inspected['protocol'] == 'local-file-job/1'
                assert len(inspected['runtime_distributions']) == 7
                assert set(inspected['model_files']) == {'model.bin', 'config.json', 'tokenizer.json'}
                assert inspected['fingerprint'] in dialog.runtime_details.GetValue()
                assert not runtime.worker.with_name('fixture-starts.log').exists()
                dialog.trust.SetValue(True)
                click(find('Register trusted runtime'))
                state['phase'] = 'registered'
            elif phase == 'registered' and idle:
                assert len(dialog.profiles) == 1 and dialog.profiles[0]['trusted'] is True
                assert dialog.profiles[0]['qualification'] == 'unqualified'
                dialog.pages.SetSelection(0)
                dialog.language.SetValue('uk')
                for control in dialog.formats.values():
                    control.SetValue(True)
                with patch.object(wx, 'FileDialog', FilesAnswer):
                    click(find('Add files…'))
                assert tuple(dialog.sources) == tuple(sources)
                assert all(str(source) in dialog.source_paths.GetValue() for source in sources)
                click(dialog.start_button)
                state['phase'] = 'running'
            elif phase == 'running':
                rows = service.list_jobs()
                if rows and rows[0]['done'] >= 1 and frame.working:
                    state['job'] = rows[0]['id']
                    click(dialog.pause_button)
                    state['phase'] = 'paused'
                elif idle:
                    raise AssertionError('The controlled checkpoint was not paused by the actual native button.')
            elif phase == 'paused' and idle:
                job = service.get_job(state['job'])
                assert job['status'] == 'paused' and 1 <= job['done'] < job['total'], job
                assert dialog.current_job['id'] == job['id'] and dialog.start_button.IsEnabled()
                state['first_receipt'] = dict(job['results'][0])
                runtime.options.write_text('{}', encoding='utf-8')
                click(dialog.start_button)
                state['phase'] = 'completed'
            elif phase == 'completed' and idle:
                job = service.verify(state['job'])
                assert job['status'] == 'complete' and job['done'] == job['total'] == 2, job
                assert state['first_receipt'] in job['results']
                assert len(runtime.worker.with_name('fixture-starts.log').read_text(encoding='utf-8').splitlines()) == 2
                assert not dialog.start_button.IsEnabled()
                dialog.pages.SetSelection(2)
                click(find('Load transcript'))
                state['phase'] = 'loaded'
            elif phase == 'loaded' and idle:
                assert dialog.editor.GetValue() == 'Синтетичний протокольний результат.', dialog.status.GetValue()
                assert wx.Window.FindFocus() is dialog.editor
                assert 'unverified' in dialog.provenance.GetValue().lower()
                dialog.editor.SetValue('Окрема відредагована копія.')
                click(find('Save edits'))
                state['phase'] = 'edited'
            elif phase == 'edited' and idle:
                source_id = dialog.transcript_source_id
                assert service.read_transcript(state['job'], source_id)['text'] == 'Синтетичний протокольний результат.'
                assert service.read_transcript(state['job'], source_id, edited=True)['text'] == 'Окрема відредагована копія.'
                with patch.object(wx, 'DirDialog', DirectoryAnswer):
                    click(find('Export results…'))
                state['phase'] = 'exported'
            elif phase == 'exported' and idle:
                exports = list(temporary.glob('transcription-*'))
                assert len(exports) == 1, exports
                assert len(list(exports[0].iterdir())) == 10
                assert {path.suffix for path in exports[0].iterdir()} == {'.txt', '.srt', '.vtt', '.json'}
                status = dialog.status.GetValue()
                heading, separator, payload = status.partition('\n')
                assert heading == 'Export saved.' and separator, status
                receipt = json.loads(payload)
                assert receipt['status'] == 'complete' and receipt['job_id'] == state['job']
                assert isinstance(receipt['files'], list) and len(receipt['files']) == 9
                manifest = Path(receipt['manifest_path'])
                assert manifest.parent == exports[0]
                assert sha256(manifest.read_bytes()).hexdigest() == receipt['manifest_sha256']
                assert {Path(row['path']) for row in receipt['files']} == (
                    set(exports[0].iterdir()) - {manifest})
                assert all(source.read_bytes() == raw for source, raw in originals.items())
                assert [row['id'] for row in service.list()] == old_legacy
                # Simulate an unbound canonical historical job. The actual
                # Bind control must review it without launching another child.
                binding = service.root / 'jobs' / state['job'] / 'workspace.json'
                binding.unlink()
                dialog.jobs.SetSelection(0)
                click(find('Load selected job'))
                state['phase'] = 'unbound'
            elif phase == 'unbound' and idle:
                assert dialog.current_job['execution_review_required'] is True
                assert 'Bind to current workspace' in dialog.status.GetValue()
                with patch.object(wx, 'MessageBox', return_value=wx.YES):
                    click(find('Bind to current workspace…'))
                state['phase'] = 'bound'
            elif phase == 'bound' and idle:
                assert not service.get_job(state['job']).get('execution_review_required', False)
                assert service.get_job(state['job'])['status'] == 'complete'
                assert len(runtime.worker.with_name('fixture-starts.log').read_text(encoding='utf-8').splitlines()) == 2
                state['done'] = True
                click(dialog.close_button)
                return
            wx.CallLater(50, drive)
        except BaseException:
            failures.append(traceback.format_exc())
            dialog.cancel_event.set()
            frame.cancel_event.set()
            dialog.EndModal(wx.ID_CANCEL)

    try:
        wx.CallAfter(dialog.refresh)
        wx.CallLater(50, drive)
        dialog.ShowModal()
        if failures:
            raise AssertionError('\n'.join(failures))
        assert state['done'], 'The native runtime/profile dialog exited before acceptance.'
    finally:
        dialog.closed = True
        dialog.cancel_event.set()
        dialog.timer.Stop()
        dialog.Destroy()
    return {'status': 'PASS', 'protocol': 'local-file-job/1',
        'checks': ['actual named runtime inspect/trust/register controls',
            'complete synthetic Python/distribution/model inventory',
            'actual file action and retained originals', 'actual native Pause and Resume',
            'same accepted receipt across real child restart',
            'actual transcript/edit/export controls', 'four formats and separate edit',
            'explicit historical workspace binding without execution',
            'legacy workspace list remains separate', 'exact imported source identities'],
        'module_origins': origins,
        'file_directory_picker_interaction': 'NOT_RUN; answers are controlled',
        'binding_confirmation_interaction': 'NOT_RUN; affirmative answer is controlled',
        'recognition_backend': 'public synthetic file protocol fixture',
        'acoustic_inference': 'NOT_RUN', 'manual_NVDA': 'NOT_RUN'}

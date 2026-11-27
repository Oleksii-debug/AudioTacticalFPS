"""Controller/event tests. These do not substitute for native wx or NVDA."""
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from carr_library.audio.models import AudioCancelled
from carr_library.audio.transcription import TranscriptionSettings
from carr_library.speech.session import ReaderDraftStore, ReaderResume
from carr_library.ui.transcription_dialog import TranscriptionController, prepare_reading_copy


JOB = 'a' * 64
SOURCE = 'b' * 64 + '.wav'


class QueuedHost:
    def __init__(self, root):
        self.library = SimpleNamespace(root=root)
        self.working = self._closing = False
        self.cancel_event = threading.Event()
        self.queue = []

    def run_job(self, title, operation, done=None, failed=None):
        self.working = True
        self.cancel_event.clear()
        self.queue.append(SimpleNamespace(title=title, operation=operation, done=done, failed=failed))

    def launch(self):
        job = self.queue[-1]
        outcome = {}
        def work():
            try:
                outcome['value'] = job.operation()
            except BaseException as exc:
                outcome['error'] = exc
        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        return job, outcome, thread

    def finish(self, pending=None):
        job, outcome, thread = pending or self.launch()
        thread.join(3)
        assert not thread.is_alive(), 'Controller work failed to stop.'
        self.working = False
        if 'error' in outcome:
            job.failed(outcome['error'])
        else:
            job.done(outcome['value'])
        return outcome


class SyntheticService:
    def __init__(self, root):
        self.calls, self.controls = [], []
        self.command = 'run'
        self.started = threading.Event()
        self.wait_for_stop = False
        self.text = 'Synthetic transcript\n'
        self.output = root / 'verified-output'
        self.output.mkdir()
        self.record = {'id': JOB, 'status': 'queued', 'completed': [], 'error': None,
                       'request': {'settings': asdict(TranscriptionSettings(language='uk')),
                                   'formats': ['txt', 'srt'], 'inputs': [{'file': SOURCE}]}}

    def call(self, name, value=None):
        self.calls.append((name, value, threading.get_ident()))

    def create(self, sources, *, settings, formats, cancelled):
        self.call('create', (sources, settings, formats))
        if cancelled():
            raise AudioCancelled('Preparation stopped.')
        self.record['request']['settings'] = asdict(settings)
        self.record['request']['formats'] = list(formats)
        return JOB

    def control(self, job_id, command):
        assert job_id == JOB
        self.call('control', command)
        self.controls.append(command)
        self.command = command

    def run(self, job_id, *, cancelled, on_status):
        assert job_id == JOB
        self.call('run')
        self.record['status'] = 'running'
        on_status('Transcribing file 1 of 1 locally.')
        self.started.set()
        if self.wait_for_stop:
            deadline = threading.Event()
            for _ in range(500):
                external = cancelled()
                if external or self.command != 'run':
                    self.record['status'] = 'paused' if self.command == 'pause' else 'cancelled'
                    raise AudioCancelled('Active file stopped.')
                deadline.wait(.002)
            raise AssertionError('No stop request reached the worker.')
        if cancelled():
            self.record['status'] = 'cancelled'
            raise AudioCancelled('Stopped.')
        self.record.update(status='complete', completed=[SOURCE])
        on_status('Transcription complete; every requested output has been verified.')
        return deepcopy(self.record)

    def jobs(self, *, cancelled):
        self.call('jobs')
        if cancelled():
            raise AudioCancelled('Stopped.')
        return [{'id': JOB, 'status': self.record['status'],
                 'completed': len(self.record['completed']), 'total': 1, 'error': None}]

    def details(self, job_id, **kwargs):
        self.call('details')
        assert job_id == JOB
        return {'record': deepcopy(self.record),
                'outputs': ([{'file': SOURCE, 'label': 'synthetic.wav',
                              'formats': ['txt', 'srt'], 'directory': str(self.output)}]
                            if self.record['completed'] else [])}

    def interrupted_details(self, job_id):
        self.call('interrupted_details')
        assert job_id == JOB
        record = deepcopy(self.record)
        if record['status'] == 'complete':
            record['status'] = 'unverified'
        return {'record': record, 'outputs': [], 'validation_cancelled': True}

    def transcript_text(self, job_id, source_file, *, cancelled):
        self.call('transcript_text')
        assert job_id == JOB and source_file == SOURCE
        if cancelled():
            raise AudioCancelled('Stopped.')
        return self.text, 'synthetic.wav'


@pytest.fixture
def setup(tmp_path):
    host = QueuedHost(tmp_path)
    service = SyntheticService(tmp_path)
    progress, changes, runtimes = [], [], []
    def runtime_factory(path):
        runtimes.append((path, threading.get_ident()))
        def verify(cancelled):
            if cancelled():
                raise AudioCancelled('Stopped.')
            return ('c' * 64, b'', None, None, None)
        return SimpleNamespace(verify=verify)
    def service_factory(state_root, runtime=None):
        service.call('factory', (state_root, runtime))
        return service
    controller = TranscriptionController(host, changed=lambda focus: changes.append(focus),
        dispatch=progress.append, service_factory=service_factory, runtime_factory=runtime_factory)
    return SimpleNamespace(host=host, service=service, controller=controller,
                           progress=progress, changes=changes, runtimes=runtimes)


def ready(setup):
    setup.controller.set_manifest('/synthetic-reviewed-runtime.json')
    setup.controller.set_files(['/synthetic.wav'])


def test_dependency_inventory_verification_is_deferred_to_host_worker(setup):
    setup.controller.set_manifest('/synthetic-reviewed-runtime.json')
    assert setup.controller.verify_runtime()
    assert setup.controller.busy and not setup.runtimes
    setup.host.finish()
    assert not setup.controller.busy
    assert setup.runtimes[0][1] != threading.get_ident()
    assert 'inventories verified' in setup.controller.dependency
    assert 'does not run recognition' in setup.controller.dependency


def test_missing_runtime_cannot_start_or_download_anything(setup):
    setup.controller.set_files(['/synthetic.wav'])
    assert not setup.controller.start()
    assert not setup.host.queue and not setup.runtimes and not setup.service.calls


def test_invalid_runtime_reports_failure_and_releases_busy_state(setup):
    ready(setup)
    def fail(path):
        raise ValueError('No reviewed runtime manifest.')
    setup.controller.runtime_factory = fail
    setup.controller.start()
    setup.host.finish()
    assert not setup.controller.busy
    assert 'No reviewed runtime' in setup.controller.status
    assert not setup.service.controls


def test_new_job_captures_parameters_and_only_runs_on_background_thread(setup):
    ready(setup)
    selected = TranscriptionSettings(language='uk', task='transcribe', device='cpu', compute_type='int8')
    assert setup.controller.start(selected, ('txt', 'srt'))
    assert setup.controller.busy
    assert setup.service.calls == []
    assert setup.controller.set_files(['/different.wav']) is False
    setup.host.finish()
    calls = [entry for entry in setup.service.calls if entry[0] == 'create']
    assert calls[0][1] == (('/synthetic.wav',), selected, ('txt', 'srt'))
    assert all(entry[2] != threading.get_ident() for entry in setup.service.calls)
    assert setup.controller.record['status'] == 'complete'
    assert setup.controller.outputs[0]['file'] == SOURCE


def test_resume_uses_saved_request_even_when_new_settings_are_passed(setup):
    ready(setup)
    setup.service.record['status'] = 'paused'
    setup.controller.inspect(JOB)
    setup.host.finish()
    assert setup.controller.job_id == JOB
    setup.controller.start(TranscriptionSettings(language='en'), ('vtt',))
    setup.host.finish()
    assert not any(name == 'create' for name, _, _ in setup.service.calls)
    assert setup.controller.record['request']['settings']['language'] == 'uk'
    assert setup.controller.record['request']['formats'] == ['txt', 'srt']


@pytest.mark.parametrize('command,status', [('pause', 'paused'), ('cancel', 'cancelled')])
def test_stop_request_is_flushed_on_worker_and_durable_status_is_preserved(setup, command, status):
    ready(setup)
    setup.service.wait_for_stop = True
    setup.controller.start()
    pending = setup.host.launch()
    assert setup.service.started.wait(1)
    assert not setup.controller.close()
    assert setup.controller.request_stop(command)
    setup.host.finish(pending)
    assert setup.controller.record['status'] == status
    assert setup.service.controls == ['run', command]
    assert all(thread != threading.get_ident() for name, _, thread in setup.service.calls if name == 'control')
    assert setup.controller.close()


def test_cancel_before_input_retention_does_not_create_or_control_job(setup):
    ready(setup)
    setup.controller.start()
    setup.controller.request_stop('cancel')
    setup.host.finish()
    assert not setup.service.controls
    assert setup.controller.record is None
    assert 'Preparation cancelled' in setup.controller.status


def test_host_cancel_button_stops_active_transcription(setup):
    ready(setup)
    setup.service.wait_for_stop = True
    setup.controller.start()
    pending = setup.host.launch()
    assert setup.service.started.wait(1)
    setup.host.cancel_event.set()
    setup.host.finish(pending)
    assert setup.controller.record['status'] == 'cancelled'


def test_busy_host_does_not_queue_second_operation(setup):
    ready(setup)
    setup.host.working = True
    assert not setup.controller.start()
    assert not setup.controller.busy and not setup.host.queue


def test_submission_rejection_and_exception_release_controller(setup):
    ready(setup)
    setup.host.run_job = lambda *args, **kwargs: False
    assert not setup.controller.start()
    assert not setup.controller.busy
    def fail(*args, **kwargs):
        raise RuntimeError('Executor is closed.')
    setup.host.run_job = fail
    assert not setup.controller.start()
    assert 'Executor is closed' in setup.controller.status
    assert not setup.controller.busy


def test_saved_jobs_are_inspectable_without_any_runtime(setup):
    assert setup.controller.refresh()
    assert setup.service.calls == []
    setup.host.finish()
    assert setup.controller.jobs[0]['id'] == JOB
    assert not setup.runtimes
    assert setup.controller.inspect(JOB)
    setup.host.finish()
    assert setup.controller.record['status'] == 'queued'
    assert not setup.runtimes


def test_library_switch_rejects_late_completion(setup):
    ready(setup)
    setup.controller.start()
    pending = setup.host.launch()
    pending[2].join(3)
    setup.host.library = SimpleNamespace(root=Path('/different-library'))
    setup.host.finish(pending)
    assert setup.controller.record is None
    assert 'library folder changed' in setup.controller.status


def test_closed_view_drops_queued_progress_and_completion(setup):
    ready(setup)
    setup.controller.start()
    pending = setup.host.launch()
    pending[2].join(3)
    setup.controller.dispose()
    assert setup.controller.closed and setup.controller._token is None
    status = setup.controller.status
    for callback in setup.progress:
        callback()
    setup.host.finish(pending)
    assert setup.controller.status == status
    assert setup.controller.record is None


def test_failed_job_reports_error_without_claiming_complete(setup):
    ready(setup)
    def fail(*args, **kwargs):
        setup.service.record.update(status='failed', error='Synthetic engine failure')
        raise ValueError('Synthetic engine failure')
    setup.service.run = fail
    setup.controller.start()
    result = setup.host.finish()['value']
    assert result['status'] == 'failed'
    assert setup.controller.record['status'] == 'failed'
    assert 'Synthetic engine failure' in setup.controller.status


def test_reading_copy_reopens_saved_edits_undo_voice_and_cursor(tmp_path):
    service = SyntheticService(tmp_path)
    prepared = prepare_reading_copy(tmp_path, service, JOB, SOURCE)
    store = ReaderDraftStore(tmp_path, slot=prepared['slot'])
    saved = store.load()
    assert saved.document._source_path is None
    assert saved.document.source_text == service.text
    saved.document.set_text('User correction, separate from transcript.')
    store.save(ReaderResume(saved.document, 'synthetic-voice', 2, 65, 4, 1.5))
    reopened = prepare_reading_copy(tmp_path, service, JOB, SOURCE)
    restored = store.load()
    assert reopened == {'slot': prepared['slot'], 'recovered': True}
    assert restored.document.text == 'User correction, separate from transcript.'
    assert restored.document.can_undo and restored.document.source_text == service.text
    assert (restored.voice_id, restored.rate, restored.volume, restored.cursor) == ('synthetic-voice', 2, 65, 4)
    assert service.text == 'Synthetic transcript\n'


def test_changed_transcript_does_not_block_or_overwrite_existing_reading_draft(tmp_path):
    service = SyntheticService(tmp_path)
    prepared = prepare_reading_copy(tmp_path, service, JOB, SOURCE)
    store = ReaderDraftStore(tmp_path, slot=prepared['slot'])
    before = store.path.read_bytes()
    service.text = 'Different transcript bytes.'
    recovered = prepare_reading_copy(tmp_path, service, JOB, SOURCE)
    assert recovered['recovered'] is True
    assert store.path.read_bytes() == before


def test_editor_opens_only_after_background_prepare_completed(setup):
    setup.controller.job_id = JOB
    opened = []
    setup.controller.edit(SOURCE, lambda slot: opened.append((slot, setup.host.working, threading.get_ident())))
    assert not opened
    setup.host.finish()
    assert opened == [(f'transcription:{JOB}:{SOURCE}', False, threading.get_ident())]
    assert 'not citation evidence' in setup.controller.status


def test_folder_launch_is_deferred_and_only_receives_verified_directory(setup):
    setup.controller.job_id = JOB
    setup.service.record.update(status='complete', completed=[SOURCE])
    launched = []
    setup.controller.open_folder(SOURCE, lambda path: launched.append(path) or True)
    assert not launched
    setup.host.finish()
    assert launched == [str(setup.service.output)]
    assert 'Opened the local result folder for review' in setup.controller.status


def test_result_replaced_by_file_before_launch_is_not_opened(setup):
    setup.controller.job_id = JOB
    setup.service.record.update(status='complete', completed=[SOURCE])
    launched = []
    setup.controller.open_folder(SOURCE, lambda path: launched.append(path) or True)
    pending = setup.host.launch()
    pending[2].join(3)
    setup.service.output.rmdir()
    setup.service.output.write_text('Not a directory.', encoding='utf-8')
    setup.host.finish(pending)
    assert not launched



def test_folder_scan_is_background_and_does_not_install_runtime(setup):
    setup.controller.set_files(['/existing.wav'])
    observed = []
    def folder_inputs(folder, *, existing, cancelled):
        observed.append((folder, existing, threading.get_ident()))
        assert not cancelled()
        return ['/existing.wav', '/folder/one.wav']
    setup.service.folder_inputs = folder_inputs
    assert setup.controller.add_folder('/folder')
    assert not observed and setup.controller.busy
    setup.host.finish()
    assert observed == [('/folder', ('/existing.wav',), observed[0][2])]
    assert observed[0][2] != threading.get_ident()
    assert setup.controller.paths == ['/existing.wav', '/folder/one.wav']
    assert 'subfolders were not scanned' in setup.controller.status
    assert not setup.runtimes


def test_failed_folder_scan_keeps_previous_file_selection(setup):
    setup.controller.set_files(['/keep.wav'])
    def failed(*args, **kwargs):
        raise ValueError('The selected audio exceeds the file-count limit.')
    setup.service.folder_inputs = failed
    setup.controller.add_folder('/folder')
    setup.host.finish()
    assert setup.controller.paths == ['/keep.wav']
    assert 'file-count limit' in setup.controller.status



@pytest.mark.parametrize('status', ['queued', 'paused', 'failed', 'invalid'])
def test_history_status_does_not_turn_refresh_into_new_failed_operation(setup, status):
    from carr_library.ui.operation_progress import operation_outcome
    setup.service.record['status'] = status
    setup.controller.refresh()
    result = setup.host.finish()['value']
    assert operation_outcome(result) == 'succeeded'
    assert setup.controller.jobs[0]['status'] == status



def test_cancelled_run_does_not_start_another_receipt_scan(setup):
    ready(setup)
    setup.service.record['completed'] = [SOURCE]
    setup.service.wait_for_stop = True
    setup.controller.start()
    pending = setup.host.launch()
    assert setup.service.started.wait(1)
    setup.controller.request_stop('pause')
    setup.host.finish(pending)
    assert setup.controller.record['status'] == 'paused'
    assert setup.controller.outputs == []
    assert not any(name == 'details' for name, _, _ in setup.service.calls)
    assert any(name == 'interrupted_details' for name, _, _ in setup.service.calls)
    assert 'not rechecked after stop' in setup.controller.status


def test_cancel_during_post_run_validation_returns_unverified_metadata_promptly(setup):
    ready(setup)
    validating = threading.Event()
    def slow_details(job_id, *, cancelled):
        validating.set()
        for _ in range(500):
            if cancelled():
                raise AudioCancelled('Receipt validation stopped.')
            threading.Event().wait(.002)
        raise AssertionError('Cancellation never reached receipt validation.')
    setup.service.details = slow_details
    setup.controller.start()
    pending = setup.host.launch()
    assert validating.wait(1)
    setup.controller.request_stop('cancel')
    result = setup.host.finish(pending)['value']
    assert result['status'] == 'cancelled'
    assert setup.service.record['status'] == 'complete'
    assert setup.controller.record['status'] == 'unverified'
    assert setup.controller.outputs == []



def test_dispose_requests_background_stop_and_never_touches_view_again(setup):
    ready(setup)
    setup.service.wait_for_stop = True
    setup.controller.start()
    pending = setup.host.launch()
    assert setup.service.started.wait(1)
    setup.controller.dispose()
    changes = list(setup.changes)
    assert not setup.host.cancel_event.is_set()
    result = setup.host.finish(pending)['value']
    for callback in setup.progress:
        callback()
    assert result['status'] == 'cancelled'
    assert setup.service.record['status'] == 'cancelled'
    assert setup.service.controls == ['run']
    assert setup.controller.closed and setup.controller._token is None
    assert setup.controller.record is None and setup.changes == changes


def _deferred_native_dialog(controller):
    """Execute the actual view methods against a delayed native-destruction boundary."""
    import ast
    from carr_library.ui import transcription_dialog

    source = Path(transcription_dialog.__file__)
    module = ast.parse(source.read_text(encoding='utf-8'))
    factory = next(node for node in module.body
                   if isinstance(node, ast.FunctionDef) and node.name == 'create_transcription_dialog')
    view = next(node for node in factory.body if isinstance(node, ast.ClassDef))
    definition = deepcopy(view)
    definition.name = 'DeferredTranscriptionDialog'
    definition.bases = [ast.Name(id='DeferredNativeDialog', ctx=ast.Load())]
    definition.body = [node for node in definition.body
                       if isinstance(node, ast.FunctionDef) and node.name in ('Destroy', 'on_destroy')]

    class DeferredNativeDialog:
        def __init__(self, controller):
            self.controller = controller
            self.destroy_requests = 0
            self._native_id = 4101

        def GetId(self):
            raise AssertionError('Destruction must not call through an invalid native wrapper.')

        def Destroy(self):
            self.destroy_requests += 1
            # Native top-level deletion and its event have not happened yet.
            return True

    namespace = {'DeferredNativeDialog': DeferredNativeDialog}
    code = ast.Module(body=[definition], type_ignores=[])
    exec(compile(ast.fix_missing_locations(code), str(source), 'exec'), namespace)
    return namespace[definition.name](controller)


@pytest.mark.parametrize('termination', ['destroy-request', 'destroy-event', 'rewrapped-destroy-event'])
def test_view_destruction_detaches_active_worker_before_late_callbacks(setup, termination):
    ready(setup)
    setup.service.wait_for_stop = True
    setup.controller.start()
    pending = setup.host.launch()
    assert setup.service.started.wait(1)
    window = _deferred_native_dialog(setup.controller)
    skipped = []

    def event(target, native_id=None):
        identity = native_id if native_id is not None else (window._native_id if target is window else 4102)
        return SimpleNamespace(GetEventObject=lambda: target, GetId=lambda: identity,
                               Skip=lambda: skipped.append(target))

    try:
        child = object()
        window.on_destroy(event(child))
        assert skipped == [child] and not setup.controller.closed
        if termination == 'destroy-request':
            assert window.Destroy() is True
            assert window.destroy_requests == 1
        elif termination == 'destroy-event':
            # Parent-driven native deletion retains the event fallback.
            window.on_destroy(event(window))
        else:
            # SIP can invalidate and recreate the Python wrapper before the
            # base native destructor delivers the same window's real ID.
            window.on_destroy(event(object(), native_id=window._native_id))
        assert setup.controller.closed and setup.controller._token is None
        status, changes = setup.controller.status, list(setup.changes)
        setup.controller.dispose()  # Repeated disposal has no host or view side effects.
        result = setup.host.finish(pending)['value']
        for callback in setup.progress:
            callback()
        window.on_destroy(event(window))  # Eventual native notification is harmless.
        assert result['status'] == 'cancelled'
        assert setup.service.record['status'] == 'cancelled'
        assert setup.service.controls == ['run']
        assert not setup.host.cancel_event.is_set()
        assert setup.controller.status == status and setup.changes == changes
        assert setup.controller.record is None and setup.controller._token is None
    finally:
        setup.controller.dispose()
        pending[2].join(3)


def test_destroy_tolerates_partial_view_construction():
    window = _deferred_native_dialog(None)
    del window.controller
    assert window.Destroy() is True
    assert window.destroy_requests == 1


def test_destroy_after_parent_native_deletion_keeps_wrapper_guard(setup, monkeypatch):
    window = _deferred_native_dialog(setup.controller)
    monkeypatch.setattr(type(window), '__bool__', lambda self: False, raising=False)
    assert window.Destroy() is False
    assert setup.controller.closed and window.destroy_requests == 0


@pytest.mark.parametrize('native_deleted', [False, True])
def test_saved_job_modal_handoff_imports_wx_and_cleans_up_dead_parent(monkeypatch, native_deleted):
    import sys
    from unittest.mock import Mock
    from carr_library.ui import transcription_dialog as ui
    queued = []
    monkeypatch.setitem(sys.modules, 'wx', SimpleNamespace(
        CallAfter=lambda callback, *args: queued.append((callback, args))))
    class Dialog:
        def __init__(self):
            self.controller = SimpleNamespace(inspect=Mock(), dispose=Mock())
            self.pages = SimpleNamespace(SetSelection=Mock())
            self.CentreOnParent, self.Destroy = Mock(), Mock()
        def __bool__(self): return not native_deleted
        def ShowModal(self):
            for callback, args in queued:
                callback(*args)
    dialog = Dialog()
    monkeypatch.setattr(ui, 'create_transcription_dialog', lambda frame: dialog)
    ui.show_transcription(SimpleNamespace(), job_id=JOB)
    dialog.pages.SetSelection.assert_called_once_with(1)
    dialog.controller.inspect.assert_called_once_with(JOB)
    dialog.controller.dispose.assert_called_once()
    assert dialog.Destroy.call_count == (0 if native_deleted else 1)


def test_worker_progress_updates_only_its_bound_canonical_task(setup):
    from carr_library.progress import ProgressHub, ProgressState, bind_progress
    ready(setup)
    hub = ProgressHub()
    captured = hub.start('Captured transcription')
    unrelated = hub.start('Unrelated later operation')
    setup.host.progress_panel = SimpleNamespace(active_task=unrelated)
    setup.host.operation_progress = SimpleNamespace(
        update=lambda *args, **kwargs: pytest.fail('Canonical work updated the legacy observer.'))
    assert setup.controller.start()
    queued = setup.host.queue[-1]
    original = queued.operation
    def bound():
        with bind_progress(captured):
            return original()
    queued.operation = bound
    outcome = setup.host.finish()
    assert 'error' not in outcome and setup.controller.record['status'] == 'complete'
    assert 'every requested output' in captured.snapshot().phase
    assert len(captured.snapshot().phase) <= 200
    assert captured.snapshot().state == ProgressState.RUNNING
    assert unrelated.snapshot().revision == 1 and not unrelated.snapshot().phase
    # Durable completion remains the existing desktop host's responsibility.


@pytest.mark.parametrize('state', ['cancelling', 'cancelled', 'complete'])
def test_late_progress_does_not_rewrite_a_canonical_terminal_or_cancelling_task(setup, state):
    from carr_library.progress import ProgressHub
    task = ProgressHub().start('Owned ASR observation')
    if state == 'cancelling':
        task.request_cancel()
    elif state == 'cancelled':
        task.cancelled()
    else:
        task.succeed(durable=True)
    before = task.snapshot()
    token = setup.controller._token = object()
    setup.controller.busy = True
    setup.controller._progress(token, 'Late owned phase', None, progress_task=task)
    assert task.snapshot() == before
    assert len(setup.progress) == 1
    setup.controller.dispose()
    setup.progress.pop()()
    assert setup.controller.closed and setup.controller.status != 'Late owned phase'


def _routed_native_dialog(*, profile_pending=False, manifest_pending=False, page=0):
    """Run the actual native methods, replacing only native controls."""
    import ast
    from unittest.mock import Mock
    from carr_library.ui import transcription_dialog as ui
    source = Path(ui.__file__)
    tree = ast.parse(source.read_text(encoding='utf-8'))
    factory = next(node for node in tree.body
                   if isinstance(node, ast.FunctionDef) and node.name == 'create_transcription_dialog')
    selected = next(node for node in factory.body if isinstance(node, ast.ClassDef))
    definition = deepcopy(selected)
    definition.name = 'RoutedNativeDialog'
    definition.bases = [ast.Name(id='NativeOwner', ctx=ast.Load())]
    names = {'_profile_active', 'refresh_shared_actions', 'page_changed',
             'start', 'pause', 'cancel', 'query_progress'}
    definition.body = [node for node in definition.body
                       if isinstance(node, ast.FunctionDef) and node.name in names]

    class Control:
        def __init__(self, value='', selection=0):
            self.value, self.selection = value, selection
            self.enabled, self.focused = None, False
            self.dead = False
        def touch(self):
            if self.dead:
                raise AssertionError('A detached action touched its native control.')
        def GetSelection(self): self.touch(); return self.selection
        def GetValue(self): self.touch(); return self.value
        def GetStringSelection(self): self.touch(); return self.value
        def Enable(self, value): self.touch(); self.enabled = value
        def SetFocus(self): self.touch(); self.focused = True

    class NativeOwner:
        def __bool__(self): return self.native_alive

    namespace = {'NativeOwner': NativeOwner, 'TranscriptionSettings': ui.TranscriptionSettings}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[definition], type_ignores=[])),
                 str(source), 'exec'), namespace)
    window = namespace['RoutedNativeDialog']()
    window.native_alive = True
    window.controller = SimpleNamespace(closed=False, busy=manifest_pending, mode='run',
        manifest='explicit-manifest', paths=['selected.wav'], job_id=None,
        start=Mock(), request_stop=Mock(), report=Mock())
    window.profile_view = SimpleNamespace(closed=False, pending=profile_pending, _kind='transcribe',
        current_job=None, start=Mock(), pause=Mock(), cancel=Mock(), status=Control())
    window.pages, window.status = Control(selection=page), Control()
    window.start_button, window.pause_button, window.cancel_button = Control(), Control(), Control()
    window.language, window.task = Control('uk'), Control(selection=0)
    window.device, window.compute = Control('cpu'), Control('int8')
    window.formats = {'txt': Control(True)}
    return window


@pytest.mark.parametrize('owner,page', [('profile', 0), ('profile', 2), ('manifest', 0), ('manifest', 2)])
def test_shared_native_asr_commands_select_the_pending_owner_before_the_visible_page(owner, page):
    view = _routed_native_dialog(profile_pending=owner == 'profile',
                                manifest_pending=owner == 'manifest', page=page)
    view.refresh_shared_actions()
    assert not view.start_button.enabled
    assert view.pause_button.enabled and view.cancel_button.enabled
    view.start()
    view.pause()
    view.cancel()
    view.query_progress()
    if owner == 'profile':
        view.profile_view.start.assert_called_once()
        view.profile_view.pause.assert_called_once()
        view.profile_view.cancel.assert_called_once()
        assert view.profile_view.status.focused and not view.status.focused
        view.controller.start.assert_not_called()
        view.controller.request_stop.assert_not_called()
    else:
        view.controller.start.assert_called_once()
        assert [call.args[0] for call in view.controller.request_stop.call_args_list] == ['pause', 'cancel']
        assert view.status.focused and not view.profile_view.status.focused
        view.profile_view.start.assert_not_called()
        view.profile_view.pause.assert_not_called()
        view.profile_view.cancel.assert_not_called()


@pytest.mark.parametrize('page', [0, 1, 2])
def test_idle_shared_start_uses_only_the_selected_transcription_page(page):
    view = _routed_native_dialog(page=page)
    view.start()
    assert view.profile_view.start.call_count == (1 if page == 2 else 0)
    assert view.controller.start.call_count == (0 if page == 2 else 1)


@pytest.mark.parametrize('closed,native_alive', [(True, True), (False, False), (True, False)])
def test_detached_shared_transcription_commands_never_query_deleted_controls(closed, native_alive):
    view = _routed_native_dialog(profile_pending=True, page=2)
    view.controller.closed, view.native_alive = closed, native_alive
    for value in vars(view).values():
        if hasattr(value, 'dead'):
            value.dead = True
    view.profile_view.status.dead = True
    view.refresh_shared_actions()
    view.start()
    view.pause()
    view.cancel()
    view.query_progress()
    view.controller.start.assert_not_called()
    view.controller.request_stop.assert_not_called()
    view.profile_view.start.assert_not_called()
    view.profile_view.pause.assert_not_called()
    view.profile_view.cancel.assert_not_called()

    view.controller.report.assert_not_called()

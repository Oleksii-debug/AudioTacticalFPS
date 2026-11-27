"""Queued native-action contracts; fixtures never execute an ASR runtime.

These tests cover lifecycle, privacy boundaries, cancellation and derivatives.
They do not claim native screen-reader or recognition-quality acceptance.
"""
from copy import deepcopy
from pathlib import Path
import sys
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from carr_library.progress import ProgressHub, bind_progress
from carr_library.ui.transcription_dialog import TranscriptionActions, TranscriptionController


class Control:
    def __init__(self, value='', selection=-1):
        self.value, self.selection = value, selection
        self.items, self.enabled, self.focused = [], True, False

    def GetValue(self): return self.value
    def SetValue(self, value): self.value = value
    def ChangeValue(self, value): self.value = value
    def GetSelection(self): return self.selection
    def SetSelection(self, value): self.selection = value
    def SetItems(self, values): self.items, self.selection = list(values), -1
    def Enable(self, value=True): self.enabled = value
    def SetFocus(self): self.focused = True


def saved_job(status='pending', done=0):
    return {'id': 'job-one', 'profile_id': 'runtime-one', 'title': 'Synthetic recording',
            'status': status, 'done': done, 'total': 2, 'error': '',
            'sources': [{'id': 'source-one', 'original_path': '/synthetic/one.wav'},
                        {'id': 'source-two', 'original_path': '/synthetic/two.wav'}],
            'settings': {'language': 'uk', 'task': 'transcribe', 'device': 'cpu',
                         'compute_type': 'int8', 'output_txt': True, 'output_json': True},
            'results': [{'source_id': 'source-one'}] if done else []}


class Service:
    def __init__(self):
        self.calls = []
        self.job = saved_job()
        self.runtimes = [{'id': 'runtime-one', 'name': 'Reviewed runtime', 'trusted': True,
                          'model_name': 'existing-model', 'qualification': 'unqualified'}]
        self.edits = {}
        self.on_run = None

    def list_runtimes(self): return deepcopy(self.runtimes)
    def list_jobs(self): return [deepcopy(self.job)]
    def get_job(self, identity):
        assert identity == self.job['id']
        return deepcopy(self.job)

    def create_job(self, profile, sources, **options):
        self.calls.append(('create', profile, sources, options))
        if options['cancel']():
            raise InterruptedError('Synthetic preparation cancelled.')
        self.job['settings'] = deepcopy(options['settings'])
        return deepcopy(self.job)

    def _perform(self, name, identity, **options):
        self.calls.append((name, identity, options))
        options['progress']({'phase': 'Transcribing', 'done': 1, 'total': 2, 'current': 'one.wav'})
        if self.on_run:
            self.on_run(options)
        if not options['cancel']():
            self.job.update(status='complete', done=2)
        return deepcopy(self.job)

    def run(self, identity, **options): return self._perform('run', identity, **options)
    def resume(self, identity, **options): return self._perform('resume', identity, **options)

    def pause(self, identity):
        self.calls.append(('pause', identity))
        self.job.update(status='paused', done=1)

    def cancel(self, identity):
        self.calls.append(('cancel', identity))
        self.job.update(status='cancelled', done=1)

    def inspect_runtime(self, *paths, **options):
        self.calls.append(('inspect', paths, options))
        return {'fingerprint': 'exact-inspected-fingerprint', 'model_name': 'existing-model'}

    def register_runtime(self, name, *paths, **options):
        self.calls.append(('register', name, paths, options))
        return deepcopy(self.runtimes[0])

    def read_transcript(self, identity, source, *, edited=False, cancel=None):
        self.calls.append(('read', identity, source, edited))
        if edited and source not in self.edits:
            raise FileNotFoundError('No saved edit.')
        return {'text': self.edits[source] if edited else 'Original recognition text.',
                'source_id': source, 'edited': edited,
                'provenance': {'job_id': identity, 'source_id': source, 'derivative': True}}

    def save_edit(self, identity, source, text, *, cancelled=lambda: False):
        self.calls.append(('save', identity, source, text))
        self.edits[source] = text
        return {'text': text, 'edited': True, 'provenance': {'source_id': source, 'derivative': True}}

    def export(self, identity, directory, **options):
        self.calls.append(('export', identity, directory, options))
        return {'status': 'exported', 'directory': str(directory), 'files': ['one.txt']}

    def rebind_runtime(self, identity, profile):
        self.calls.append(('rebind', identity, profile))
        return deepcopy(self.job)

    def bind_canonical_job(self, identity, **options):
        self.calls.append(('bind', identity, options))
        self.job['execution_review_required'] = False
        return deepcopy(self.job)

    def acknowledge_interruption(self, identity, **options):
        self.calls.append(('acknowledge', identity, options))
        self.job.update(status='paused', recovery_review_required=False)
        return deepcopy(self.job)


@pytest.fixture
def view(tmp_path):
    service, operations = Service(), []
    root = tmp_path / 'library'
    root.mkdir()
    frame = SimpleNamespace(library=SimpleNamespace(root=root), active=('workspace-one', 'Fixture'), working=False, _closing=False,
                            cancel_event=Event(), progress_hub=ProgressHub())
    def run_job(title, operation, done, failed):
        frame.working = True
        frame.cancel_event.clear()
        task = frame.progress_hub.start(title)
        def bound_operation():
            with bind_progress(task):
                return operation()
        operations.append(SimpleNamespace(operation=bound_operation, done=done, failed=failed, task=task))
    frame.run_job = Mock(side_effect=run_job)
    result = TranscriptionActions()
    for name in ('status', 'jobs', 'result_source', 'editor', 'provenance', 'runtime_details',
                 'start_button', 'pause_button', 'cancel_button'):
        setattr(result, name, Control())
    result.runtime = Control(selection=0)
    result.language, result.title = Control('uk'), Control('Selected title')
    result.task, result.device = Control(selection=0), Control(selection=0)
    result.beam, result.parallel = Control(5), Control(1)
    result.vad, result.context, result.multilingual = Control(True), Control(True), Control(False)
    result.headers, result.recursive, result.latest_edit = Control(False), Control(False), Control(True)
    result.formats = {name: Control(name in ('txt', 'json')) for name in ('txt', 'srt', 'vtt', 'json')}
    result.python_path = Control('/synthetic/python')
    result.worker_path = Control('/synthetic/worker.py')
    result.model_path = Control('/synthetic/model')
    result.runtime_name, result.trust = Control('Reviewed runtime'), Control(False)
    result.mutable = [result.runtime, result.jobs, result.editor, result.language,
                      result.title, result.python_path, result.worker_path, result.model_path]
    result.new_controls, result.read_buttons = [result.language, result.title], [Control()]
    result.confirm_discard = Mock(return_value=True)
    result.confirm_rebind = Mock(return_value=True)
    result.confirm_bind_workspace = Mock(return_value=True)
    result.confirm_worker_stopped = Mock(return_value=True)
    result.finish_close = Mock()
    result.configure(frame, TranscriptionController(root, service=service))
    result.profiles = deepcopy(service.runtimes)
    result.sources = (Path('/synthetic/selected.wav'),)
    result.operations, result.fake_service = operations, service
    return result


def finish(view, *, operation=None):
    job = operation or view.operations[-1]
    try:
        value = job.operation()
    except Exception as exc:
        view.frame.working = False
        job.failed(exc)
        return exc
    view.frame.working = False
    job.done(value)
    return value


def test_start_snapshots_exact_choices_and_routes_to_isolated_service(view):
    view.start()
    assert view.pending and not view.language.enabled
    assert view.fake_service.calls == []
    view.language.SetValue('en')
    view.sources = (Path('/not/the/selected.wav'),)
    finish(view)
    create, run = view.fake_service.calls
    assert create[0:3] == ('create', 'runtime-one', (Path('/synthetic/selected.wav'),))
    assert create[3]['settings']['language'] == 'uk'
    assert create[3]['settings']['output_mode'] == 'separate'
    assert create[3]['recursive'] is False
    assert run[0:2] == ('run', 'job-one')
    assert view.current_job['status'] == 'complete'
    assert 'complete' in view.status.value and not view.start_button.enabled
    assert not view.editor.focused


@pytest.mark.parametrize('change', ['untrusted', 'language', 'format', 'device', 'sources'])
def test_invalid_or_untrusted_choices_never_start_runtime(view, change):
    if change == 'untrusted': view.profiles[0]['trusted'] = False
    if change == 'language': view.language.SetValue('not a code')
    if change == 'format':
        for control in view.formats.values(): control.SetValue(False)
    if change == 'device': view.device.SetSelection(-1)
    if change == 'sources': view.sources = ()
    view.start()
    assert not view.pending and not view.fake_service.calls
    view.frame.run_job.assert_not_called()


def test_resume_uses_persisted_job_and_ignores_new_options(view):
    view.publish_job(saved_job('paused', 1))
    view.language.SetValue('bad new settings')
    view.runtime.SetSelection(-1)
    view.start()
    finish(view)
    assert [row[0] for row in view.fake_service.calls] == ['resume']
    assert view.fake_service.calls[0][1] == 'job-one'
    assert view.current_job['settings']['language'] == 'uk'


def test_frame_cancellation_during_preparation_never_starts_inference(view):
    view.start()
    view.frame.cancel_event.set()
    assert isinstance(finish(view), InterruptedError)
    assert view.fake_service.calls == []
    assert view.current_job is None and not view.pending
    assert 'stopped before completion' in view.status.value


def test_pause_records_service_intent_and_preserves_partial_results(view):
    view.fake_service.on_run = lambda options: view.pause()
    view.start()
    finish(view)
    assert ('pause', 'job-one') in view.fake_service.calls
    assert view.current_job['status'] == 'paused' and view.current_job['done'] == 1
    assert view.start_button.enabled and not view.pending
    assert 'paused' in view.status.value and 'complete;' not in view.status.value
    view.fake_service.on_run = None
    view.start()
    finish(view)
    assert view.fake_service.calls[-1][0] == 'resume'
    assert view.current_job['status'] == 'complete'


def test_cancel_records_cancellation_and_does_not_claim_success(view):
    view.fake_service.on_run = lambda options: view.cancel()
    view.start()
    finish(view)
    assert ('cancel', 'job-one') in view.fake_service.calls
    assert view.current_job['status'] == 'cancelled'
    assert 'cancelled' in view.status.value


def test_progress_is_coalesced_without_moving_focus(view):
    def progress(options):
        options['progress']({'phase': 'File one checkpoint', 'done': 1, 'total': 2})
        options['progress']({'phase': 'Writing outputs', 'done': 1, 'total': 2})
        assert 'Writing outputs' not in view.status.value
        view.flush_progress()
        assert 'Writing outputs: 1 of 2 files' in view.status.value
        global_progress = view.operations[-1].task.snapshot()
        assert global_progress.phase == 'Writing outputs'
        assert '1 of 2 files' in global_progress.message
        assert global_progress.total is None
        assert not view.status.focused
    view.fake_service.on_run = progress
    view.start()
    finish(view)
    view.flush_progress()
    assert 'complete' in view.status.value


def test_incomparable_phase_units_and_observer_cancellation_do_not_fail_asr(view):
    def progress(options):
        options['progress']({'phase': 'Verifying model files', 'done': 2000, 'total': 3000})
        options['progress']({'phase': 'Recognizing retained files', 'done': 0, 'total': 2})
        view.operations[-1].task.request_cancel()
        options['progress']({'phase': 'Checkpoint preserved', 'done': 1, 'total': 2})
    view.fake_service.on_run = progress
    view.start()
    result = finish(view)
    assert result['status'] == 'complete'
    assert view.operations[-1].task.snapshot().total is None


def test_failed_global_observer_preserves_real_completed_result(view, monkeypatch):
    view.start()
    def unavailable(**kwargs):
        raise RuntimeError('Synthetic unavailable observer')
    monkeypatch.setattr(view.operations[-1].task, 'update', unavailable)
    result = finish(view)
    assert result['status'] == 'complete'
    assert view.current_job['status'] == 'complete'


def test_transcription_menu_registers_searchable_configurable_command(monkeypatch):
    from carr_library.ui.command_registry import CommandRegistry
    from carr_library.ui.transcription_dialog import install_transcription_menu
    monkeypatch.setitem(sys.modules, 'wx', SimpleNamespace(ID_ANY=-1, EVT_MENU='menu'))
    item = SimpleNamespace(label='Local transcription')
    menu = SimpleNamespace(Append=Mock(return_value=item))
    frame = SimpleNamespace(Bind=Mock())
    install_transcription_menu(frame, menu)
    [(command, registered_item)] = frame._pending_keyboard_commands
    assert command.command_id == 'tools.transcription' and registered_item is item
    registry = CommandRegistry((command,))
    assert registry.shortcut('tools.transcription', {}) == ''
    assert registry.shortcut('tools.transcription', {'tools.transcription': 'Ctrl+Alt+F11'}) == 'Ctrl+Alt+F11'
    assert frame.Bind.call_args.args[2] is item


def test_old_callbacks_and_root_changes_cannot_publish_results(view, tmp_path):
    view.start()
    old = view.operations[-1]
    finish(view)
    view.new_job()
    view.start()
    old.done(saved_job('failed'))
    assert view.pending and view.current_job is None
    before = view.status.value
    view.frame.library = SimpleNamespace(root=tmp_path / 'another-library')
    assert isinstance(finish(view), InterruptedError)
    assert view.current_job is None and view.status.value == before
    assert [row[0] for row in view.fake_service.calls].count('create') == 1


def test_closed_window_ignores_late_progress_and_completion(view):
    view.start()
    job = view.operations[-1]
    view.closed = True
    original = view.status.value
    with pytest.raises(InterruptedError):
        job.operation()
    job.done(saved_job('complete', 2))
    view.flush_progress()
    assert view.fake_service.calls == []
    assert view.current_job is None and view.status.value == original


def test_close_during_run_waits_for_paused_checkpoint(view):
    event = SimpleNamespace(CanVeto=lambda: True, Veto=Mock())
    def close_while_running(options):
        view.close(event)
        assert not view.closed
        view.finish_close.assert_not_called()
    view.fake_service.on_run = close_while_running
    view.start()
    finish(view)
    event.Veto.assert_called_once()
    assert view.closed and view.current_job['status'] == 'paused'
    view.finish_close.assert_called_once()


def test_runtime_trust_binds_registration_to_inspected_paths_and_fingerprint(view):
    view.register_runtime()
    assert not view.operations
    view.inspect_runtime()
    finish(view)
    assert view.inspection and view.trust.value is False
    view.trust.SetValue(True)
    view.register_runtime()
    finish(view)
    register = view.fake_service.calls[-1]
    assert register[0:3] == ('register', 'Reviewed runtime',
                            ('/synthetic/python', '/synthetic/worker.py', '/synthetic/model'))
    assert register[3]['expected_fingerprint'] == 'exact-inspected-fingerprint'
    assert register[3]['owner_trusted'] is True
    view.worker_path.SetValue('/changed/worker.py')
    view.runtime_changed()
    assert view.inspection is None and not view.trust.value
    before = len(view.operations)
    view.register_runtime()
    assert len(view.operations) == before


def test_inspection_completion_does_not_trust_paths_changed_while_pending(view):
    view.inspect_runtime()
    view.model_path.SetValue('/different/model')
    finish(view)
    assert view.inspection is None and not view.trust.value


def test_loading_editing_undo_and_save_preserve_original_and_source_identity(view):
    view.publish_job(saved_job('complete', 2))
    view.load_transcript()
    finish(view)
    assert view.document.source_text == view.editor.value == 'Original recognition text.'
    assert not view.is_dirty() and view.editor.focused
    view.editor.SetValue('Corrected reading copy.')
    view.undo()
    assert view.editor.value == 'Original recognition text.'
    view.redo()
    assert view.editor.value == 'Corrected reading copy.'
    view.result_source.SetSelection(1)  # changing the chooser must not retarget an open editor
    view.save_edits()
    finish(view)
    assert view.fake_service.calls[-1] == ('save', 'job-one', 'source-one', 'Corrected reading copy.')
    assert view.document.source_text == 'Original recognition text.'
    assert not view.is_dirty()
    assert 'derivative' in view.provenance.value


def test_saved_edit_load_and_declined_discard_preserve_reading_copy(view):
    view.fake_service.edits['source-one'] = 'Saved correction.'
    view.publish_job(saved_job('complete', 2))
    view.load_transcript()
    finish(view)
    assert view.editor.value == 'Saved correction.'
    assert view.document.source_text == 'Original recognition text.'
    view.editor.SetValue('Unsaved correction.')
    view.confirm_discard.return_value = False
    view.new_job()
    view.load_transcript()
    view.close()
    assert view.current_job['id'] == 'job-one'
    assert view.editor.value == 'Unsaved correction.' and not view.closed


def test_export_uses_job_formats_and_blocks_unsaved_derivative(view, tmp_path):
    view.publish_job(saved_job('partial', 1))
    view.load_transcript()
    finish(view)
    view.editor.SetValue('Unsaved edit')
    count = len(view.operations)
    view.export_to(tmp_path / 'export')
    assert len(view.operations) == count
    assert 'Save the edited' in view.status.value
    view.editor.SetValue(view.saved_text)
    view.formats['vtt'].SetValue(True)  # new-job options cannot change the durable job's formats
    view.export_to(tmp_path / 'new-export')
    finish(view)
    exported = view.fake_service.calls[-1]
    assert exported[:3] == ('export', 'job-one', tmp_path / 'new-export')
    assert exported[3]['formats'] == ('txt', 'json') and exported[3]['include_edits'] is True
    assert callable(exported[3]['cancel']) and (tmp_path / 'new-export').is_dir()


def test_restore_rebind_requires_explicit_confirmation_and_trusted_profile(view):
    view.publish_job(saved_job('interrupted'))
    view.confirm_rebind.return_value = False
    view.rebind_runtime()
    assert not view.operations
    view.confirm_rebind.return_value = True
    view.rebind_runtime()
    finish(view)
    assert view.fake_service.calls[-1] == ('rebind', 'job-one', 'runtime-one')


def test_host_busy_and_start_failure_restore_controls(view):
    view.frame.working = True
    view.start()
    assert not view.operations and not view.pending
    view.frame.working = False
    view.frame.run_job.side_effect = RuntimeError('Synthetic executor failure')
    view.start()
    assert not view.pending and view.language.enabled
    assert 'Synthetic executor failure' in view.status.value


def test_interrupted_job_requires_explicit_worker_stop_review(view):
    job = saved_job('interrupted')
    job['recovery_review_required'] = True
    view.publish_job(job)
    view.confirm_worker_stopped.return_value = False
    view.review_interruption()
    assert not view.operations
    view.confirm_worker_stopped.return_value = True
    view.review_interruption()
    finish(view)
    assert view.fake_service.calls[-1] == ('acknowledge', 'job-one', {'worker_stopped': True})
    assert view.current_job['status'] == 'paused'


def test_restored_complete_job_is_readable_without_reclassifying_it_as_pending(view):
    job = saved_job('complete', 2)
    job.update(restored=True, recovery_review_required=True)
    view.publish_job(job)
    view.review_interruption()
    assert view.current_job['status'] == 'complete' and not view.operations
    view.confirm_worker_stopped.assert_not_called()
    assert not view.start_button.enabled


@pytest.mark.parametrize('existing', [False, True])
def test_export_rejects_private_or_preexisting_destination(view, tmp_path, existing):
    view.publish_job(saved_job('complete', 2))
    target = tmp_path / 'export' if existing else view.library.root / 'inside-private-data'
    if existing:
        target.mkdir()
        (target / 'keep.txt').write_text('Keep existing file.')
    view.export_to(target)
    result = finish(view)
    assert isinstance(result, (ValueError, FileExistsError))
    assert not any(row[0] == 'export' for row in view.fake_service.calls)
    if existing:
        assert (target / 'keep.txt').read_text() == 'Keep existing file.'


def test_ui_actions_integrate_real_service_subprocess_and_durable_derivatives(view, tmp_path):
    """Real protocol process and files, synthetic recognition only; no model inference."""
    import runpy
    from carr_library.transcription import TranscriptionService
    from carr_library.operations import ResourceBroker
    fixture = runpy.run_path(str(Path(__file__).parents[1] / 'transcription' / 'fixture_runtime.py'))
    runtime = fixture['make_runtime'](tmp_path / 'synthetic-runtime')
    adapter = fixture['make_service'](view.library.root / 'state' / 'transcription')
    service = adapter.host
    view.frame.library = view.library = service.app
    view.frame.active = (service.workspace, 'Actual service fixture')
    profile = fixture['register'](service, runtime)
    source = tmp_path / 'synthetic-input.wav'
    original = b'Synthetic protocol bytes; this fixture performs no audio recognition.'
    source.write_bytes(original)
    view.controller = TranscriptionController(view.library.root, service=service)
    view.controller.attach(view.frame)
    view.profiles = [profile]
    view.sources = (source,)
    view.formats['srt'].SetValue(True)
    view.start()
    result = finish(view)
    assert isinstance(result, dict) and result['status'] == 'complete', result
    assert result['done'] == 1 and len(result['results']) == 1
    assert source.read_bytes() == original
    identity = result['id']
    view.load_transcript()
    finish(view)
    assert view.editor.GetValue() == 'Синтетичний протокольний результат.'
    view.editor.SetValue('Відредагована копія протокольного результату.')
    view.save_edits()
    finish(view)
    assert service.read_transcript(identity)['text'] == 'Синтетичний протокольний результат.'
    assert service.read_transcript(identity, edited=True)['text'] == view.editor.GetValue()
    destination = tmp_path / 'new-transcript-export'
    view.export_to(destination)
    receipt = finish(view)
    assert receipt['status'] == 'complete' and receipt['citation_authority'] is False
    assert {row['format'] for row in receipt['files']} == {'txt', 'srt', 'json'}
    assert any(row['edited'] for row in receipt['files'])
    assert all(Path(row['path']).is_file() for row in receipt['files'])


@pytest.mark.parametrize('change', ['same-path-library', 'workspace', 'closing', 'destroy'])
def test_queued_runtime_action_rechecks_current_library_workspace_and_lifetime(view, change):
    view.start()
    queued = view.operations[-1]
    before = view.status.value
    if change == 'same-path-library':
        view.frame.library = SimpleNamespace(root=view.library.root)
    elif change == 'workspace':
        view.frame.active = ('other-workspace', 'Other')
    elif change == 'closing':
        view.frame._closing = True
    else:
        view.destroyed()
    with pytest.raises(InterruptedError):
        queued.operation()
    queued.done(saved_job('complete', 2))
    queued.failed(RuntimeError('Late synthetic result'))
    view.flush_progress()
    assert view.fake_service.calls == []
    assert view.current_job is None and view.status.value == before


def test_binding_is_explicit_and_does_not_run_or_trust_runtime(view):
    job = saved_job('interrupted')
    job.update(restored=True, execution_review_required=True, recovery_review_required=True)
    view.fake_service.job = deepcopy(job)
    view.publish_job(job)
    view.confirm_bind_workspace.return_value = False
    view.bind_workspace()
    assert not view.operations and not view.fake_service.calls
    view.confirm_bind_workspace.return_value = True
    view.bind_workspace()
    result = finish(view)
    assert result['execution_review_required'] is False
    [(kind, identity, options)] = view.fake_service.calls
    assert (kind, identity) == ('bind', 'job-one')
    assert options['reviewed'] is True and callable(options['cancel'])
    assert view.current_job['status'] == 'interrupted'
    assert view.fake_service.runtimes[0]['trusted'] is True  # untouched existing metadata


def test_library_switch_inside_confirmation_cannot_queue_rebind(view):
    view.publish_job(saved_job('interrupted'))
    def switch(*args):
        view.frame.library = SimpleNamespace(root=view.library.root)
        return True
    view.confirm_rebind.side_effect = switch
    before = view.status.value
    view.rebind_runtime()
    assert not view.operations and not view.fake_service.calls
    assert view.status.value == before


def test_late_transcript_read_never_updates_or_focuses_replaced_library(view):
    view.publish_job(saved_job('complete', 2))
    view.load_transcript()
    queued = view.operations[-1]
    result = queued.operation()
    before = (view.editor.value, view.status.value, view.provenance.value)
    view.frame.library = SimpleNamespace(root=view.library.root)
    queued.done(result)
    assert view.document is None and not view.editor.focused
    assert (view.editor.value, view.status.value, view.provenance.value) == before



@pytest.mark.parametrize('publication', ['published', 'partial', 'uncertain'])
def test_export_failure_displays_actual_publication_receipt_without_changing_job(view, tmp_path, publication):
    from carr_library.integrations.transcription_exports import TranscriptionExportedError
    job = saved_job('complete', 2)
    view.publish_job(job)
    destination = tmp_path / ('reported-' + publication)
    receipt = {'schema': 'resonance-transcription-publication/1', 'status': publication,
               'directory': str(destination), 'job_id': job['id'], 'files': ['retained.txt']}
    error = TranscriptionExportedError(receipt)
    view.fake_service.export = Mock(side_effect=error)
    view.export_to(destination)
    assert finish(view) is error
    assert publication in view.status.value and str(destination) in view.status.value
    assert 'retained.txt' in view.status.value and view.status.focused
    assert view.current_job['status'] == 'complete'
    assert 'Refresh saved jobs' not in view.status.value
    view.fake_service.export.assert_called_once()


def test_late_publication_receipt_never_updates_replaced_library(view, tmp_path):
    from carr_library.integrations.transcription_exports import TranscriptionExportedError
    view.publish_job(saved_job('complete', 2))
    error = TranscriptionExportedError({'status': 'partial', 'directory': str(tmp_path / 'retained'),
                                       'files': ['already-published.txt']})
    view.fake_service.export = Mock(side_effect=error)
    view.export_to(tmp_path / 'chosen-export')
    queued = view.operations[-1]
    with pytest.raises(TranscriptionExportedError):
        queued.operation()
    before = view.status.value
    view.status.focused = False
    view.frame.library = SimpleNamespace(root=view.library.root)
    queued.failed(error)
    assert view.status.value == before and not view.status.focused

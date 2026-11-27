"""Queued native-action contracts; fixtures never execute an ASR runtime.

These tests cover lifecycle, privacy boundaries, cancellation and derivatives.
They do not claim native screen-reader or recognition-quality acceptance.
"""
from copy import deepcopy
from contextlib import nullcontext
import os
from pathlib import Path
import sys
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock
import venv

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


def saved_job(status='ready', done=0):
    return {'id': 'job-one', 'runtime_profile_id': 'runtime-one', 'title': 'Synthetic recording',
            'status': status, 'done': done, 'total': 2, 'last_error': '', 'protocol': 'receipts-v1',
            'sources': [{'filename': 'source-one', 'original_name': 'one.wav', 'sha256': 'a' * 64},
                        {'filename': 'source-two', 'original_name': 'two.wav', 'sha256': 'b' * 64}],
            'settings': {'language': 'uk', 'task': 'transcribe', 'device': 'cpu', 'model': 'small',
                         'compute_type': 'int8', 'output_txt': True, 'output_json': True,
                         'output_srt': True, 'output_vtt': True},
            'artifacts': [{'source': 'source-one'}] if done else []}


class Service:
    def __init__(self):
        self.calls = []
        self.job = saved_job()
        self.runtimes = [{'id': 'runtime-one', 'name': 'Reviewed runtime', 'trusted': True,
                          'configuration': {'protocol': 'receipts-v1'},
                          'qualification': 'unqualified'}]
        self.edit_rows = {}
        self.on_run = None

    def list_runtimes(self): return deepcopy(self.runtimes)
    def list(self): return [deepcopy(self.job)]
    def get(self, identity):
        assert identity == self.job['id']
        return deepcopy(self.job)

    def submit_sources(self, sources, *, profile_id, protocol, title, options, recursive, cancelled):
        self.calls.append(('create', profile_id, sources,
                           {'settings': options, 'recursive': recursive, 'protocol': protocol,
                            'title': title, 'cancelled': cancelled}))
        if cancelled():
            raise InterruptedError('Synthetic preparation cancelled.')
        self.job['settings'] = deepcopy(options)
        self.job['protocol'] = protocol
        return {'id': self.job['id'], 'files': len(sources), 'excluded_files': 0}

    def _perform(self, name, identity, **options):
        self.calls.append((name, identity, options))
        options['progress'](1, 2, 'Transcribing')
        if self.on_run:
            self.on_run(options)
        if not options['cancelled']():
            self.job.update(status='completed', done=2)
        return deepcopy(self.job)

    def run(self, identity, **options): return self._perform('run', identity, **options)
    def resume(self, identity, **options): return self._perform('resume', identity, **options)

    def pause(self, identity):
        self.calls.append(('pause', identity))
        self.job.update(status='paused', done=1)
        return deepcopy(self.job)

    def cancel(self, identity):
        self.calls.append(('cancel', identity))
        self.job.update(status='cancelled', done=1)
        return deepcopy(self.job)

    def inspect_runtime(self, *paths, **options):
        self.calls.append(('inspect', paths, options))
        return {'fingerprint': 'exact-inspected-fingerprint', 'models_dir': paths[2],
                'protocol': options['protocol'], 'inference_qualified': False}

    def register_runtime(self, name, *paths, **options):
        self.calls.append(('register', name, paths, options))
        profile = deepcopy(self.runtimes[0])
        profile['configuration']['protocol'] = options['protocol']
        return profile

    def text(self, identity, source, *, cancelled=lambda: False):
        self.calls.append(('read', identity, source))
        if cancelled():
            raise InterruptedError('Synthetic transcript reading cancelled.')
        return 'Original recognition text.'

    def edits(self, identity):
        return [{'id': 'revision-one', 'source': source, 'text': value}
                for source, value in self.edit_rows.items()]

    def save_edit(self, identity, source, text, *, cancelled=lambda: False):
        self.calls.append(('save', identity, source, text))
        if cancelled():
            raise InterruptedError('Synthetic derivative save cancelled.')
        self.edit_rows[source] = text
        return 'revision-one'

    def export(self, identity, directory, **options):
        self.calls.append(('export', identity, directory, options))
        assert not directory.exists(), 'The UI must leave create-only publication to the service.'
        directory.mkdir()
        (directory / 'one.txt').write_text('Original recognition text.', encoding='utf-8')
        return directory

    def rebind_runtime(self, identity, profile, *, cancelled, progress):
        self.calls.append(('rebind', identity, profile))
        assert callable(cancelled) and callable(progress)
        return deepcopy(self.job)

    def copy_job(self, identity, *, profile_id, cancelled):
        self.calls.append(('copy', identity, profile_id))
        assert not cancelled()
        self.job = {**deepcopy(self.job), 'id': 'job-two', 'status': 'ready', 'done': 0, 'artifacts': []}
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
    from carr_library.ui.save_all import SaveCoordinator
    frame = SimpleNamespace(library=SimpleNamespace(root=root, read=lambda workspace: nullcontext()), active=('workspace-one', None),
                            _save_coordinator=SaveCoordinator(), working=False, _closing=False,
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
                 'start_button', 'pause_button', 'cancel_button', 'save_edits_button'):
        setattr(result, name, Control())
    result.runtime = Control(selection=0)
    result.language, result.title = Control('uk'), Control('Selected title')
    result.model, result.words, result.protocol = Control('small'), Control(True), Control(selection=0)
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
    result.confirm_copy = Mock(return_value=True)
    result.confirm_worker_stopped = Mock(return_value=True)
    result.finish_close = Mock()
    result.configure(frame, TranscriptionController(frame.library, frame.active[0], service=service))
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
    assert all(create[3]['settings']['output_' + name] for name in ('txt', 'srt', 'vtt', 'json'))
    assert create[3]['recursive'] is False
    assert run[0:2] == ('run', 'job-one')
    assert view.current_job['status'] == 'completed'
    assert 'completed' in view.status.value and not view.start_button.enabled
    assert not view.editor.focused


@pytest.mark.parametrize('change', ['untrusted', 'language', 'task', 'device', 'sources'])
def test_invalid_or_untrusted_choices_never_start_runtime(view, change):
    if change == 'untrusted': view.profiles[0]['trusted'] = False
    if change == 'language': view.language.SetValue('not a code')
    if change == 'task': view.task.SetSelection(1)
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
    assert [row[0] for row in view.fake_service.calls] == ['create']
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
    assert view.current_job['status'] == 'completed'


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
    assert 'completed' in view.status.value


def test_incomparable_phase_units_and_observer_cancellation_do_not_fail_asr(view):
    def progress(options):
        options['progress']({'phase': 'Verifying model files', 'done': 2000, 'total': 3000})
        options['progress']({'phase': 'Recognizing retained files', 'done': 0, 'total': 2})
        view.operations[-1].task.request_cancel()
        options['progress']({'phase': 'Checkpoint preserved', 'done': 1, 'total': 2})
    view.fake_service.on_run = progress
    view.start()
    result = finish(view)
    assert result['status'] == 'completed'
    assert view.operations[-1].task.snapshot().total is None


def test_failed_global_observer_preserves_real_completed_result(view, monkeypatch):
    view.start()
    def unavailable(**kwargs):
        raise RuntimeError('Synthetic unavailable observer')
    monkeypatch.setattr(view.operations[-1].task, 'update', unavailable)
    result = finish(view)
    assert result['status'] == 'completed'
    assert view.current_job['status'] == 'completed'


def test_transcription_menu_registers_searchable_configurable_command(monkeypatch):
    from carr_library.ui.command_registry import CommandRegistry
    from carr_library.ui.transcription_dialog import install_transcription_menu
    monkeypatch.setitem(sys.modules, 'wx', SimpleNamespace(ID_ANY=-1, EVT_MENU='menu'))
    item = SimpleNamespace(label='Local transcription')
    menu = SimpleNamespace(Append=Mock(return_value=item))
    frame = SimpleNamespace(Bind=Mock())
    install_transcription_menu(frame, menu)
    [(command, registered_item)] = frame._pending_keyboard_commands
    assert command.command_id == 'transcription.open' and registered_item is item
    registry = CommandRegistry((command,))
    assert registry.shortcut('transcription.open', {}) == ''
    assert registry.shortcut('transcription.open', {'transcription.open': 'Ctrl+Alt+F11'}) == 'Ctrl+Alt+F11'
    assert frame.Bind.call_args.args[2] is item


def test_old_callbacks_and_root_changes_cannot_publish_results(view, tmp_path):
    view.start()
    old = view.operations[-1]
    finish(view)
    view.new_job()
    view.start()
    old.done(saved_job('failed'))
    assert view.pending and view.current_job is None
    view.frame.library = SimpleNamespace(root=tmp_path / 'another-library')
    finish(view)
    assert view.current_job is None
    assert 'library folder changed' in view.status.value
    assert [row[0] for row in view.fake_service.calls].count('create') == 1


def test_closed_window_ignores_late_progress_and_completion(view):
    view.start()
    job = view.operations[-1]
    value = job.operation()  # The real work finished before the view closed.
    view.closed = True
    original = view.status.value
    job.done(value)
    view.flush_progress()
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
    view.publish_job(saved_job('completed', 2))
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
    view.fake_service.edit_rows['source-one'] = 'Saved correction.'
    view.publish_job(saved_job('completed', 2))
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


def test_export_subset_preserves_all_saved_job_formats_and_blocks_unsaved_derivative(view, tmp_path):
    view.publish_job(saved_job('partial', 1))
    view.load_transcript()
    finish(view)
    view.editor.SetValue('Unsaved edit')
    count = len(view.operations)
    view.export_to(tmp_path / 'export')
    assert len(view.operations) == count
    assert 'Save the edited' in view.status.value
    view.editor.SetValue(view.saved_text)
    saved_options = deepcopy(view.current_job['settings'])
    view.formats['vtt'].SetValue(True)  # This changes only the explicit export subset.
    view.export_to(tmp_path / 'new-export')
    finish(view)
    exported = view.fake_service.calls[-1]
    assert exported[:3] == ('export', 'job-one', tmp_path / 'new-export')
    assert exported[3]['formats'] == ('txt', 'vtt', 'json')
    assert callable(exported[3]['cancelled']) and (tmp_path / 'new-export').is_dir()
    assert view.current_job['settings'] == saved_options


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
    job = saved_job('completed', 2)
    job.update(restored=True, recovery_review_required=True)
    view.publish_job(job)
    view.review_interruption()
    assert view.current_job['status'] == 'completed' and not view.operations
    view.confirm_worker_stopped.assert_not_called()
    assert not view.start_button.enabled


@pytest.mark.parametrize('existing', [False, True])
def test_export_rejects_private_or_preexisting_destination(view, tmp_path, existing):
    view.publish_job(saved_job('completed', 2))
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
    """Real existing protocol process and receipts; no acoustic inference."""
    import runpy
    from carr_library.application import LibraryApp
    from carr_library.durable_state import read_json
    fixture = runpy.run_path(str(Path(__file__).parents[1] / 'transcription' / 'test_supplied_worker_bridge.py'))
    app = LibraryApp(tmp_path / 'real-library')
    workspace = app.create_workspace('Native action protocol fixture')
    service = app.transcription(workspace)
    models = tmp_path / 'models'
    models.mkdir()
    worker = tmp_path / 'synthetic-worker.py'
    worker.write_text(fixture['SYNTHETIC_WORKER'].replace('__MODE__', repr('complete')), encoding='utf-8')
    # This existing protocol peer is standard-library-only. Review an owned
    # interpreter instead of the application's unrelated installed packages.
    environment = tmp_path / 'selected-python'
    venv.EnvBuilder(with_pip=False, system_site_packages=False,
                    symlinks=os.name != 'nt').create(environment)
    python = environment / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    inspection = service.inspect_runtime(python, worker, models)
    profile = service.register_runtime('Synthetic protocol only', python, worker, models,
        protocol='receipts-v1', owner_trusted=True, expected_fingerprint=inspection['fingerprint'])
    inventory = profile['configuration']['runtime_inventory']
    assert inventory['python_executable'] == str(python.absolute())
    assert inventory['python_configuration']
    assert all(row == {'present': False}
               for row in inventory['runtime_distributions'].values())
    source = tmp_path / 'synthetic-input.wav'
    original = b'Synthetic protocol bytes; this fixture performs no audio recognition.'
    source.write_bytes(original)
    view.frame.library, view.frame.active = app, (workspace, None)
    view.library = app
    view.controller = TranscriptionController(app, workspace, service=service)
    view.profiles = [profile]
    view.sources = (source,)
    view.prepare()
    prepared = finish(view)
    assert isinstance(prepared, tuple), prepared
    assert view.current_job['status'] == 'ready' and view.current_job['artifacts'] == []
    identity = view.current_job['id']
    view.start()
    result = finish(view)
    assert isinstance(result, dict) and result['status'] == 'completed', result
    assert result['done'] == 1 and len(result['artifacts']) == 6
    assert source.read_bytes() == original
    view.load_transcript()
    finish(view)
    retained_source = result['sources'][0]['filename']
    recognized = service.text(identity, retained_source)
    assert view.editor.GetValue() == recognized
    assert recognized.startswith('Synthetic transcript.')
    view.editor.SetValue('Відредагована копія протокольного результату.')
    view.save_edits()
    finish(view)
    assert service.text(identity, retained_source) == recognized
    assert service.edits(identity)[-1]['text'] == view.editor.GetValue()
    destination = tmp_path / 'new-transcript-export'
    view.export_to(destination)
    receipt = finish(view)
    assert receipt == {'exported_to': str(destination)}
    manifest = read_json(destination / 'transcription-manifest.json')
    assert manifest['status'] == 'completed'
    assert len(manifest['artifacts']) == 2
    assert {Path(row['filename']).suffix for row in manifest['artifacts']} == {'.txt', '.json'}
    assert manifest['edits'][-1]['text'] == view.editor.GetValue()
    assert all((destination / row['filename']).is_file() for row in manifest['artifacts'])
    assert not list(destination.rglob('*.vtt')) and not list(destination.rglob('*.srt'))
    assert len(service.verify(identity)['artifacts']) == 6
    assert source.read_bytes() == original


def test_closed_before_worker_admission_never_reads_or_starts_service(view):
    view.start()
    job = view.operations[-1]
    view.closed = True
    with pytest.raises(ValueError, match='closed'):
        job.operation()
    assert view.fake_service.calls == []


@pytest.mark.parametrize('change', ['workspace', 'root-identity', 'closing'])
def test_saved_actions_recheck_exact_workspace_and_root_before_worker(view, tmp_path, change):
    view.start()
    if change == 'workspace':
        view.frame.active = ('another-workspace', None)
    elif change == 'root-identity':
        root = view.library.root
        root.rename(tmp_path / 'old-library')
        root.mkdir()
    else:
        view.frame._closing = True
    result = finish(view)
    assert isinstance(result, ValueError)
    assert view.fake_service.calls == [] and view.current_job is None


def test_prepare_retains_without_running_and_idle_pause_cancel_are_durable(view):
    view.prepare()
    finish(view)
    assert [row[0] for row in view.fake_service.calls] == ['create']
    assert view.current_job['status'] == 'ready'
    assert 'No inference has run' in view.status.GetValue()
    view.pause()
    finish(view)
    assert view.current_job['status'] == 'paused'
    view.cancel()
    finish(view)
    assert view.current_job['status'] == 'cancelled'
    assert not any(row[0] in ('run', 'resume') for row in view.fake_service.calls)


def test_empty_export_subset_is_rejected_without_a_worker_or_target(view, tmp_path):
    view.publish_job(saved_job('completed', 2))
    for control in view.formats.values():
        control.SetValue(False)
    destination = tmp_path / 'uncreated-export'
    view.export_to(destination)
    assert not view.operations and not destination.exists()
    assert 'at least one format' in view.status.GetValue()


def test_protocol_change_invalidates_inspected_trust_and_v5_headers(view):
    from carr_library.transcription import PROTOCOLS, V5_PROTOCOL
    view.inspect_runtime()
    finish(view)
    view.trust.SetValue(True)
    view.protocol.SetSelection(PROTOCOLS.index(V5_PROTOCOL))
    view.register_runtime()  # Even a programmatic change without EVT_CHOICE is rejected.
    assert len(view.operations) == 1
    view.runtime_changed()
    assert view.inspection is None and not view.trust.GetValue()
    view.inspect_runtime()
    finish(view)
    view.trust.SetValue(True)
    view.headers.SetValue(True)
    view.register_runtime()
    finish(view)
    assert view.fake_service.calls[-1][3]['protocol'] == V5_PROTOCOL
    assert not view.headers.enabled and not view.headers.GetValue()
    view.start()
    finish(view)
    created = next(row for row in view.fake_service.calls if row[0] == 'create')
    assert created[3]['protocol'] == V5_PROTOCOL
    assert created[3]['settings']['max_attempts'] == 3
    assert not created[3]['settings']['include_headers']


def test_saved_attempts_display_real_effective_options(view):
    job = saved_job('completed', 2)
    job['effective_runs'] = [{'source': 'source-one', 'attempt': 2,
        'settings': {'beam_size': 3, 'vad_filter': False, 'condition_on_previous_text': False}}]
    view.publish_job(job)
    message = view.status.GetValue()
    assert 'one.wav: attempt 2; beam=3, VAD=False, context=False' in message
    assert 'receipts-v1' in message


def test_save_all_waits_for_derivative_commit_and_preserves_newer_edits(view):
    view.publish_job(saved_job('completed', 2))
    view.load_transcript()
    finish(view)
    view.editor.SetValue('Captured correction.')
    completed = []
    coordinator = view.frame._save_coordinator
    assert coordinator.save_all(completed.append)
    assert coordinator.busy and not completed and view.is_dirty()
    view.editor.SetValue('Newer correction while save was queued.')
    finish(view)
    assert view.fake_service.edit_rows['source-one'] == 'Captured correction.'
    assert not coordinator.busy and len(completed) == 1
    assert completed[0].saved == ('Transcription reading copy',)
    assert not completed[0].complete
    assert view.is_dirty() and view.saved_text == 'Captured correction.'
    assert view.document.source_text == 'Original recognition text.'


def test_save_all_failure_retains_dirty_editor_and_close_unregisters(view, monkeypatch):
    view.publish_job(saved_job('completed', 2))
    view.load_transcript()
    finish(view)
    view.editor.SetValue('Unsaved correction.')
    completed = []
    def failure(*args, **kwargs):
        raise OSError('Synthetic durable save failure.')
    monkeypatch.setattr(view.fake_service, 'save_edit', failure)
    coordinator = view.frame._save_coordinator
    coordinator.save_all(completed.append)
    finish(view)
    assert len(completed) == 1 and not completed[0].complete
    assert 'Synthetic durable save failure' in completed[0].message()
    assert not coordinator.busy and view.is_dirty()
    view.confirm_discard.return_value = True
    view.close()
    assert not coordinator.participants and view.closed


def test_save_all_old_scope_completion_is_not_confirmed(view):
    view.publish_job(saved_job('completed', 2))
    view.load_transcript()
    finish(view)
    view.editor.SetValue('Captured correction.')
    completed = []
    view.frame._save_coordinator.save_all(completed.append)
    operation = view.operations[-1]
    value = operation.operation()
    view.frame.working = False
    view.frame.active = ('another-workspace', None)
    operation.done(value)
    assert len(completed) == 1 and not completed[0].complete
    assert view.is_dirty() and view.saved_text == 'Original recognition text.'


def test_archived_scope_after_worker_cannot_publish_transcript(view):
    view.publish_job(saved_job('completed', 2))
    view.load_transcript()
    operation = view.operations[-1]
    value = operation.operation()
    view.frame.working = False
    def denied(workspace):
        raise PermissionError('The selected workspace is archived.')
    view.library.read = denied
    operation.done(value)
    assert view.document is None and not view.editor.GetValue()
    assert 'archived' in view.status.GetValue()


def test_save_all_refused_second_admission_settles_instead_of_hanging(view, monkeypatch):
    view.publish_job(saved_job('completed', 2))
    view.load_transcript()
    finish(view)
    view.editor.SetValue('Unsaved correction.')
    monkeypatch.setattr(view, 'submit', lambda *args, **kwargs: False)
    completed = []
    view.frame._save_coordinator.save_all(completed.append)
    assert len(completed) == 1 and not completed[0].complete
    assert not view.frame._save_coordinator.busy and view.is_dirty()


def test_copy_requires_explicit_trusted_same_protocol_profile_without_running(view):
    job = saved_job('failed')
    view.publish_job(job)
    view.confirm_copy.return_value = False
    view.copy_job()
    assert not view.operations
    view.confirm_copy.return_value = True
    view.profiles[0]['configuration']['protocol'] = 'whisper-v5-manifest-v1'
    view.copy_job()
    assert not view.operations and 'keeps the saved worker protocol' in view.status.GetValue()
    view.profiles[0]['configuration']['protocol'] = 'receipts-v1'
    view.profiles[0]['trusted'] = False
    view.copy_job()
    assert not view.operations
    view.profiles[0]['trusted'] = True
    view.copy_job()
    finish(view)
    assert view.fake_service.calls == [('copy', 'job-one', 'runtime-one')]
    assert view.current_job['id'] == 'job-two' and view.current_job['status'] == 'ready'
    assert view.current_job['settings'] == job['settings'] and not view.current_job['artifacts']
    assert 'no inference has run' in view.status.GetValue()

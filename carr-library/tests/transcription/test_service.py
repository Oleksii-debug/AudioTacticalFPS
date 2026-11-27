"""Actual subprocess/filesystem regressions; synthetic protocol is not ASR quality."""
import json
import importlib.util
from contextlib import contextmanager
import os
from pathlib import Path
import threading
import time

import pytest

from carr_library.audio.models import AudioBudget
from carr_library.durable_state import atomic_json, read_json
from carr_library.research.export_file import ExportReceipt
from carr_library.transcription import TranscriptionService, expand_sources
from carr_library.transcription import service as module
from carr_library.audio import transcription as execution
_spec = importlib.util.spec_from_file_location('transcription_service_fixture_builder', Path(__file__).with_name('fixture_runtime.py'))
_builder = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_builder)
make_runtime, register = _builder.make_runtime, _builder.register


@pytest.fixture
def setup(tmp_path):
    runtime = make_runtime(tmp_path / 'runtime')
    service = TranscriptionService(tmp_path / 'library/state/transcription',
        budget=AudioBudget(timeout=5, reserve_disk_bytes=0, max_log_bytes=65536), stop_grace=.15)
    profile = register(service, runtime)
    audio = tmp_path / 'Оригінал.wav'
    audio.write_bytes(b'Synthetic input; never interpreted as real speech.')
    return service, runtime, profile, audio


def starts(runtime):
    return len(_builder.observations(runtime, 'started'))


def job(setup, **options):
    service, runtime, profile, audio = setup
    return service.create_job(profile['id'], [audio], **options)


def test_inspection_and_registration_never_execute_selected_code(setup):
    service, runtime, profile, audio = setup
    assert profile['trusted'] is True
    assert profile['qualification'] == 'unqualified'
    assert len(profile['runtime_distributions']) == 7
    assert starts(runtime) == 0
    assert set(runtime.model.iterdir()) == {runtime.model / name for name in ('model.bin', 'config.json', 'tokenizer.json')}


def test_untrusted_profile_cannot_start_a_process(setup):
    service, runtime, profile, audio = setup
    untrusted = register(service, runtime, trusted=False)
    created = service.create_job(untrusted['id'], [audio])
    result = service.run(created['id'])
    assert result['status'] == 'failed'
    assert 'trust' in result['error']
    assert starts(runtime) == 0


def test_stale_inspection_cannot_authorize_changed_dependency(setup):
    service, runtime, profile, audio = setup
    inspected = service.inspect_runtime(runtime.python, runtime.worker, runtime.model)
    runtime.dependencies['ctranslate2'].write_text('# Changed installed engine bytes\n')
    with pytest.raises(ValueError, match='exact current'):
        service.register_runtime('Reviewed', runtime.python, runtime.worker, runtime.model,
            owner_trusted=True, expected_fingerprint=inspected['fingerprint'])
    assert starts(runtime) == 0


@pytest.mark.parametrize('component', ['model', 'worker', 'dependency'])
def test_changed_reviewed_bytes_block_execution_before_native_start(setup, component):
    service, runtime, profile, audio = setup
    created = job(setup)
    target = {'model': runtime.model / 'model.bin', 'worker': runtime.worker,
              'dependency': runtime.dependencies['faster-whisper']}[component]
    target.write_bytes(target.read_bytes() + b'\n# changed\n')
    result = service.run(created['id'])
    assert result['status'] == 'failed'
    assert 'changed' in result['error']
    assert starts(runtime) == 0


def test_actual_process_receipts_reading_and_subtitle_rollover(setup):
    service, runtime, profile, audio = setup
    original = audio.read_bytes()
    created = job(setup, settings={'output_srt': True, 'output_vtt': True})
    events = []
    result = service.run(created['id'], progress=events.append)
    assert result['status'] == 'complete', result['error']
    assert result['done'] == result['total'] == 1
    assert starts(runtime) == 1
    assert audio.read_bytes() == original
    read = service.read_transcript(created['id'])
    assert read['text'] == 'Синтетичний протокольний результат.'
    assert read['provenance']['citation_authority'] is False
    assert read['provenance']['quality'] == 'unqualified'
    assert read['provenance']['translation_target'] is None
    receipt = service._receipt(result, result['results'][0])
    srt = service._job_root(created['id']) / receipt['files']['srt']['path']
    assert '00:01:00,000 --> 00:01:01,000' in srt.read_text()
    assert '00:00:60' not in srt.read_text()
    assert events[-1]['status'] == 'complete'
    assert service.run(created['id'])['status'] == 'complete'
    assert starts(runtime) == 1


def test_isolated_worker_preserves_unicode_paths_and_diagnostics(tmp_path, monkeypatch):
    runtime = make_runtime(tmp_path / 'Рушій з пробілами')
    diagnostic = 'Українська діагностика — перевірено.'
    runtime.options.write_text(json.dumps({'require_utf8': True, 'diagnostic': diagnostic},
                                         ensure_ascii=False), encoding='utf-8')
    service = TranscriptionService(tmp_path / 'Бібліотека/дані/state/transcription',
                                  budget=AudioBudget(timeout=5, reserve_disk_bytes=0))
    profile = register(service, runtime)
    source = tmp_path / 'Розмова українською.wav'
    source.write_bytes(b'Synthetic input identity; no acoustic inference.')
    original_run, processes = execution.run_process, []
    def capture(argv, **kwargs):
        result = original_run(argv, **kwargs)
        processes.append((argv, result))
        return result
    monkeypatch.setattr(execution, 'run_process', capture)
    # -I intentionally ignores these; the command-line mode must prevail.
    monkeypatch.setenv('PYTHONUTF8', '0')
    monkeypatch.setenv('PYTHONIOENCODING', 'ascii')
    result = service.run(service.create_job(profile['id'], [source])['id'])
    assert result['status'] == 'complete', result['error']
    assert len(processes) == 1
    argv, process = processes[0]
    assert argv[1:6] == ['-I', '-S', '-B', '-X', 'utf8']
    assert diagnostic in process.stdout.decode('utf-8')
    assert diagnostic in process.stderr.decode('utf-8')
    assert service.read_transcript(result['id'])['text'] == 'Синтетичний протокольний результат.'


@pytest.mark.parametrize('flag', ['wrong_source', 'wrong_language', 'invalid_timing', 'malformed_status'])
def test_invalid_worker_protocol_cannot_become_success(setup, flag):
    service, runtime, profile, audio = setup
    runtime.options.write_text(json.dumps({flag: True}))
    result = service.run(job(setup)['id'])
    assert result['status'] == 'failed', result
    assert result['done'] == 0 and result['results'] == []


def test_diagnostics_are_bounded_even_when_worker_exits_fast(setup):
    service, runtime, profile, audio = setup
    runtime.options.write_text(json.dumps({'flood': True}))
    result = service.run(job(setup)['id'])
    assert result['status'] in ('failed', 'partial')
    assert 'diagnostic' in result['error']


def test_cancel_stops_unresponsive_worker_and_releases_resource_lease(setup):
    service, runtime, profile, audio = setup
    runtime.options.write_text(json.dumps({'delay': 10, 'ignore_stop': True}))
    created = job(setup)
    beginning = time.monotonic()
    result = service.run(created['id'], cancel=lambda: time.monotonic() - beginning > .3)
    assert result['status'] == 'cancelled'
    assert time.monotonic() - beginning < 3
    from carr_library.operations import ResourceBroker
    assert not ResourceBroker.for_app(service.library_root).snapshot()['leases']


def test_pause_intent_wins_over_ui_cancel_and_resume_reuses_verified_source(setup, tmp_path):
    service, runtime, profile, audio = setup
    runtime.options.write_text(json.dumps({'delay': .4}))
    second = tmp_path / 'second.wav'
    second.write_bytes(b'Second synthetic input')
    created = service.create_job(profile['id'], [audio, second])
    cancelled = threading.Event()
    def progress(event):
        if event['phase'].startswith('Transcribing locally') and event['done'] == 1 and not cancelled.is_set():
            service.pause(created['id'])
            cancelled.set()
    paused = service.run(created['id'], progress=progress, cancel=cancelled.is_set)
    assert paused['status'] == 'paused', paused
    assert paused['done'] == 1
    retained = service.read_transcript(created['id'], paused['results'][0]['source_id'])['text']
    complete = service.resume(created['id'])
    assert complete['status'] == 'complete', complete['error']
    assert complete['done'] == 2
    assert len(_builder.observations(runtime, 'source')) == 2
    assert service.read_transcript(created['id'], paused['results'][0]['source_id'])['text'] == retained


def test_export_failure_preserves_recognition_and_retry_needs_no_second_inference(setup, monkeypatch):
    service, runtime, profile, audio = setup
    created = job(setup, settings={'output_srt': True})
    original = module.atomic_bytes
    def failing(path, payload, **kw):
        if Path(path).suffix == '.srt':
            raise OSError('Synthetic export disk failure')
        return original(path, payload, **kw)
    monkeypatch.setattr(module, 'atomic_bytes', failing)
    result = service.run(created['id'])
    assert result['status'] == 'failed' and not result['results']
    assert starts(runtime) == 1
    monkeypatch.setattr(module, 'atomic_bytes', original)
    recovered = service.resume(created['id'])
    assert recovered['status'] == 'complete', recovered['error']
    assert starts(runtime) == 1


def test_changed_profile_does_not_relabel_an_old_unaccepted_checkpoint(setup, monkeypatch):
    service, runtime, profile, audio = setup
    created = job(setup, settings={'output_srt': True})
    original = module.atomic_bytes
    def failing(path, payload, **kw):
        if Path(path).suffix == '.srt':
            raise OSError('Synthetic export failure after recognition.')
        return original(path, payload, **kw)
    monkeypatch.setattr(module, 'atomic_bytes', failing)
    failed = service.run(created['id'])
    assert failed['status'] == 'failed' and not failed['results']
    assert starts(runtime) == 1
    # A support-file change need not change the worker's reported launcher or
    # installed-distribution identity, as with a Windows base-runtime update.
    (runtime.python.parent / 'vcruntime-synthetic.dll').write_bytes(b'New reviewed support identity; never loaded.')
    changed = register(service, runtime)
    assert changed['fingerprint'] != profile['fingerprint']
    assert changed['python_identity'] == profile['python_identity']
    assert changed['runtime_distributions'] == profile['runtime_distributions']
    monkeypatch.setattr(module, 'atomic_bytes', original)
    service.rebind_runtime(created['id'], changed['id'])
    recovered = service.resume(created['id'])
    assert recovered['status'] == 'complete', recovered['error']
    assert starts(runtime) == 2
    assert service.read_transcript(created['id'])['provenance']['profile_fingerprint'] == changed['fingerprint']
    archived = service._job_root(created['id']) / 'worker-history'
    assert list(archived.glob('*/logs/cache/**/*.cache.json'))


def test_rebind_preserves_accepted_artifact_and_its_original_profile(setup, tmp_path):
    service, runtime, profile, audio = setup
    second = tmp_path / 'second.wav'
    second.write_bytes(b'Second retained synthetic input.')
    runtime.options.write_text(json.dumps({'fail_after': 1}), encoding='utf-8')
    created = service.create_job(profile['id'], [audio, second])
    partial = service.run(created['id'])
    assert partial['status'] == 'partial' and partial['done'] == 1
    accepted = dict(partial['results'][0])
    (runtime.python.parent / 'vcruntime-synthetic.dll').write_bytes(b'New reviewed support identity; never loaded.')
    changed = register(service, runtime)
    runtime.options.write_text('{}', encoding='utf-8')
    service.rebind_runtime(created['id'], changed['id'])
    complete = service.resume(created['id'])
    assert complete['status'] == 'complete', complete['error']
    assert complete['results'][0] == accepted
    assert service.read_transcript(created['id'], accepted['source_id'])['provenance']['profile_fingerprint'] == profile['fingerprint']
    assert service.read_transcript(created['id'], complete['results'][1]['source_id'])['provenance']['profile_fingerprint'] == changed['fingerprint']
    assert len(_builder.observations(runtime, 'source')) == 2


def test_rebind_cannot_move_an_unreviewed_interrupted_worker(setup):
    service, runtime, profile, audio = setup
    created = job(setup)
    created.update(status='interrupted', recovery_review_required=True)
    service._save(created)
    with pytest.raises(RuntimeError, match='worker stopped'):
        service.rebind_runtime(created['id'], profile['id'])


def test_retained_input_change_blocks_processing_without_touching_original(setup):
    service, runtime, profile, audio = setup
    original = audio.read_bytes()
    created = job(setup)
    retained = service._job_root(created['id']) / created['sources'][0]['retained_path']
    retained.write_bytes(b'Changed retained bytes')
    result = service.run(created['id'])
    assert result['status'] == 'failed'
    assert starts(runtime) == 0
    assert audio.read_bytes() == original


def test_edit_is_versioned_derivative_and_reading_does_not_require_runtime_trust(setup):
    service, runtime, profile, audio = setup
    created = job(setup)
    result = service.run(created['id'])
    source = result['sources'][0]['id']
    original = service.read_transcript(created['id'], source)
    edited = service.save_edit(created['id'], source, 'Відредагована похідна копія.')
    register(service, runtime, trusted=False)
    assert service.acknowledge_interruption(created['id'], worker_stopped=True)['status'] == 'complete'
    assert service.read_transcript(created['id'], source)['text'] == original['text']
    assert service.read_transcript(created['id'], source, edited=True)['text'] == edited['text']
    assert edited['provenance']['revision_kind'] == 'user-edited-transcript'
    assert edited['provenance']['citation_authority'] is False


def test_export_never_overwrites_and_rejects_entire_private_library(setup, tmp_path):
    service, runtime, profile, audio = setup
    created = job(setup)
    service.run(created['id'])
    private_destination = service.library_root / 'state/speech'
    private_destination.mkdir()
    with pytest.raises(ValueError, match='entire private library'):
        service.export(created['id'], private_destination)
    destination = tmp_path / 'exports'
    destination.mkdir()
    receipt = service.export(created['id'], destination)
    assert receipt['status'] == 'complete'
    first = Path(receipt['files'][0]['path'])
    first.write_text('Owner edit to exported copy')
    with pytest.raises(FileExistsError):
        service.export(created['id'], destination)
    assert first.read_text() == 'Owner edit to exported copy'


def test_saved_export_with_surviving_staging_link_remains_truthful_success(setup, tmp_path, monkeypatch):
    service, runtime, profile, audio = setup
    created = job(setup)
    service.run(created['id'])
    destination = tmp_path / 'exports'
    destination.mkdir()
    original = module.write_binary_export
    def remaining_link(path, content, **kw):
        receipt = original(path, content, **kw)
        os.link(receipt.path, receipt.path.with_name('.owned-stage-' + receipt.path.name))
        return ExportReceipt(receipt.path, 'Synthetic cleanup remains pending; publication succeeded.')
    monkeypatch.setattr(module, 'write_binary_export', remaining_link)
    result = service.export(created['id'], destination)
    assert result['status'] == 'complete' and result['warnings']
    assert Path(result['manifest_path']).is_file()


def test_model_rebinding_cannot_mix_changed_weights_in_one_job(setup, tmp_path):
    service, runtime, profile, audio = setup
    created = job(setup)
    runtime.model.joinpath('model.bin').write_bytes(b'Different synthetic weights')
    changed = register(service, runtime)
    with pytest.raises(ValueError, match='same model content'):
        service.rebind_runtime(created['id'], changed['id'])


def test_unclean_runner_requires_explicit_review_before_resume(setup):
    service, runtime, profile, audio = setup
    created = job(setup)
    path = service._job_root(created['id']) / 'job.json'
    state = read_json(path)
    state['status'] = 'running'
    atomic_json(path, state, replace=True)
    with pytest.raises(RuntimeError, match='prior runner'):
        service.run(created['id'])
    assert service.get_job(created['id'])['status'] == 'interrupted'
    assert starts(runtime) == 0
    service.acknowledge_interruption(created['id'], worker_stopped=True)
    assert service.resume(created['id'])['status'] == 'complete'


def test_folder_selection_is_bounded_explicit_and_link_safe(tmp_path):
    folder = tmp_path / 'selected'
    folder.mkdir()
    (folder / 'one.wav').write_bytes(b'one')
    (folder / 'ignore.txt').write_bytes(b'not audio')
    child = folder / 'child'
    child.mkdir()
    (child / 'two.wav').write_bytes(b'two')
    assert len(expand_sources([folder])) == 1
    assert len(expand_sources([folder], recursive=True)) == 2
    if os.name != 'nt':
        (folder / 'linked.wav').symlink_to(child / 'two.wav')
        with pytest.raises(ValueError):
            expand_sources([folder])


def test_changed_dependency_after_resource_wait_is_rechecked_before_launch(setup):
    service, runtime, profile, audio = setup
    from carr_library.operations import ResourceBroker
    broker = ResourceBroker.for_app(service.library_root)
    class ChangingAfterAdmission:
        root, policy_path, limits = broker.root, broker.policy_path, broker.limits
        @contextmanager
        def reserve(self, request, **kwargs):
            with broker.reserve(request, **kwargs) as lease:
                runtime.dependencies['av'].write_text('# changed while this job waited\n')
                yield lease
    service.broker = ChangingAfterAdmission()
    result = service.run(job(setup)['id'])
    assert result['status'] == 'failed'
    assert starts(runtime) == 0
    assert not broker.snapshot()['leases']


def test_cuda_without_explicit_generic_pool_cannot_start_a_process(setup, monkeypatch):
    service, runtime, profile, audio = setup
    monkeypatch.setenv('CUDA_VISIBLE_DEVICES', '1')
    created = job(setup, settings={'device': 'cuda', 'compute_type': 'float16'})
    result = service.run(created['id'])
    assert result['status'] == 'failed'
    assert 'explicit shared resource capacity (cuda)' in result['error']
    assert starts(runtime) == 0


def test_profile_cuda_reserves_same_generic_pool_and_exclusive_token_as_legacy(setup):
    service, runtime, profile, audio = setup
    from dataclasses import replace
    from carr_library.operations import ResourceBroker
    broker = ResourceBroker.for_app(service.library_root)
    broker.configure(replace(broker.limits, gpu_devices=(('cuda', 512 * 1024**2),)))
    observed = []
    class ObservedAdmission:
        root, policy_path, limits = broker.root, broker.policy_path, broker.limits
        @contextmanager
        def reserve(self, request, **kwargs):
            observed.append(request)
            with broker.reserve(request, **kwargs) as lease:
                yield lease
    service.broker = ObservedAdmission()
    state = service.run(job(setup, settings={'device': 'cuda', 'compute_type': 'float16'})['id'])
    assert state['status'] == 'complete', state['error']
    assert len(observed) == 1
    request = observed[0]
    assert request.gpu_device == 'cuda' and request.devices == ('accelerator:cuda',)
    assert request.gpu_memory_bytes == 512 * 1024**2
    assert request.cpu_slots == broker.limits.cpu_slots and request.memory_bytes == broker.limits.memory_bytes
    assert not broker.snapshot()['leases']
    # The actual synthetic peer proves admission/protocol only, not GPU inference.
    assert starts(runtime) == 1


@pytest.mark.parametrize('action', ['pause', 'cancel'])
def test_control_budget_keeps_immediate_persisted_stop_during_actual_hash(setup, monkeypatch, action):
    from carr_library import durable_state
    from carr_library.transcription import runtime as selected_runtime

    service, runtime, profile, audio = setup
    audio.write_bytes(b'Synthetic retained input; no acoustic claim.\n' * 80000)
    original_bytes = audio.read_bytes()
    created = job(setup)
    root = service._job_root(created['id'])
    control = root / 'control.json'
    retained = root / created['sources'][0]['retained_path']
    real_read_json = module.read_json
    real_json_open = durable_state._open_json_file
    real_selected_open = selected_runtime._open_selected_file
    configured_budgets, read_requests, observed_actions, consumed = [], [], [], []

    def observed_json(path, **options):
        if Path(path) == control:
            configured_budgets.append(options.get('maximum'))
        return real_read_json(path, **options)

    @contextmanager
    def observed_open(path):
        with real_json_open(path) as stream:
            if Path(path) != control:
                yield stream
                return

            class ActualControlReader:
                def fileno(self):
                    return stream.fileno()

                def read(self, size=-1):
                    read_requests.append(size)
                    return stream.read(size)

            yield ActualControlReader()

    @contextmanager
    def stop_after_retained_chunk(path):
        with real_selected_open(path) as stream:
            if Path(path) != retained:
                yield stream
                return

            class ActualRetainedReader:
                def fileno(self):
                    return stream.fileno()

                def read(self, size=-1):
                    value = stream.read(size)
                    consumed.append(len(value))
                    if len(consumed) == 1:
                        getattr(service, action)(created['id'])
                    return value

            yield ActualRetainedReader()

    def cancelled():
        selected = service._control(created['id'])
        observed_actions.append(selected)
        return selected != 'run'

    monkeypatch.setattr(module, 'read_json', observed_json)
    monkeypatch.setattr(durable_state, '_open_json_file', observed_open)
    monkeypatch.setattr(selected_runtime, '_open_selected_file', stop_after_retained_chunk)
    with pytest.raises(InterruptedError, match='cancelled'):
        selected_runtime.file_identity(retained, cancel=cancelled)

    assert consumed == [1024 * 1024]
    assert observed_actions[0] == 'run' and observed_actions[-1] == action
    assert service._control(created['id']) == action
    assert configured_budgets and set(configured_budgets) == {4096}
    assert read_requests and set(read_requests) == {4097}
    assert len(read_requests) == len(configured_budgets)
    assert audio.read_bytes() == original_bytes == retained.read_bytes()
    assert starts(runtime) == 0


def test_oversized_control_fails_before_any_worker_or_control_payload_read(setup, monkeypatch):
    from carr_library import durable_state

    service, runtime, profile, audio = setup
    created = job(setup)
    root = service._job_root(created['id'])
    control = root / 'control.json'
    atomic_json(control, {'action': 'run', 'unexpected_padding': 'x' * 4096}, replace=True)
    saved_control = control.read_bytes()
    saved_job = (root / 'job.json').read_bytes()
    saved_audio = audio.read_bytes()
    real_open = durable_state._open_json_file
    real_read_json = module.read_json
    configured_budgets = []

    @contextmanager
    def no_control_payload(path):
        assert Path(path) != control, 'Oversized control bytes must fail before opening.'
        with real_open(path) as stream:
            yield stream

    def observed_json(path, **options):
        if Path(path) == control:
            configured_budgets.append(options.get('maximum'))
        return real_read_json(path, **options)

    def no_worker(*args, **options):
        raise AssertionError('An oversized control record cannot launch a worker.')

    monkeypatch.setattr(durable_state, '_open_json_file', no_control_payload)
    monkeypatch.setattr(module, 'read_json', observed_json)
    monkeypatch.setattr(execution, 'run_process', no_worker)
    with pytest.raises(ValueError, match='JSON budget'):
        service.run(created['id'])

    assert configured_budgets and set(configured_budgets) == {4096}
    assert control.read_bytes() == saved_control
    assert (root / 'job.json').read_bytes() == saved_job
    assert audio.read_bytes() == saved_audio
    assert starts(runtime) == 0

def test_control_missing_leaf_does_not_hide_missing_root_or_read_errors(setup, tmp_path, monkeypatch):
    service, runtime, profile, audio = setup
    state = job(setup)
    control = service.jobs_root / state['id'] / 'control.json'
    original = control.read_bytes()
    control.unlink()
    assert service._control(state['id']) == 'run'
    control.write_bytes(original)
    actual = module.read_json
    calls = []
    def denied(path, **kwargs):
        calls.append((Path(path), kwargs.get('maximum')))
        if Path(path) == control:
            raise PermissionError('Authored current control read refusal.')
        return actual(path, **kwargs)
    with monkeypatch.context() as failure:
        failure.setattr(module, 'read_json', denied)
        with pytest.raises(PermissionError, match='read refusal'):
            service._control(state['id'])
    assert calls == [(control, 4096)] and control.read_bytes() == original
    prior = tmp_path / 'retained-original-state'
    service.root.rename(prior)
    with pytest.raises(FileNotFoundError):
        service._control(state['id'])
    assert (prior / 'jobs' / state['id'] / 'control.json').read_bytes() == original
    assert starts(runtime) == 0


def test_actual_worker_status_reads_use_the_declared_bounded_snapshot(setup, monkeypatch):
    from carr_library import durable_state
    service, runtime, profile, audio = setup
    state = job(setup)
    real_open, calls = durable_state._open_json_file, []

    class StatusReader:
        def __init__(self, stream):
            self.stream = stream
        def __enter__(self):
            self.stream.__enter__()
            return self
        def __exit__(self, *args):
            return self.stream.__exit__(*args)
        def fileno(self):
            return self.stream.fileno()
        def read(self, count):
            value = self.stream.read(count)
            calls.append((count, len(value)))
            return value

    def opened(path):
        stream = real_open(path)
        return StatusReader(stream) if Path(path).name == 'job_status.json' else stream
    monkeypatch.setattr(durable_state, '_open_json_file', opened)
    result = service.run(state['id'])
    assert result['status'] == 'complete', result['error']
    assert starts(runtime) == 1 and calls
    assert all(requested == 256 * 1024 + 1 and 0 < consumed <= 256 * 1024
               for requested, consumed in calls)
    assert service.read_transcript(state['id'])['text']

"""Approved-plan integration through the actual retained-input worker protocol.

The existing synthetic worker is a process/receipt fixture, never an acoustic
model. These tests qualify scheduling, authority and durable publication only.
"""
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import sys
from threading import Event
from types import SimpleNamespace

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant.models import Plan, Scope, Step, canonical
from carr_library.assistant.service import AssistantService
from carr_library.assistant.workflow_access import WorkflowAccess
from carr_library.durable_state import atomic_json, read_json
from carr_library.state_recovery import prepare_restored_state
from carr_library import transcription as runtime


def protocol_fixture():
    # Load the shared, already qualified synthetic protocol source by its test
    # file. Never add checkout src to sys.path or replace carr_library imports;
    # --import-mode=importlib and the installed wheel use the same real service.
    path = Path(__file__).resolve().parents[1] / 'transcription' / 'test_supplied_worker_bridge.py'
    spec = importlib.util.spec_from_file_location('_carr_assistant_protocol_fixture', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    fixture = protocol_fixture()
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Approved transcription protocol fixture')
    service = app.transcription(workspace)
    def selected(selected_workspace):
        assert selected_workspace == workspace
        return service
    monkeypatch.setattr(app, 'transcription', selected)
    models = fixture.synthetic_models(tmp_path / 'local-models')
    source = tmp_path / 'selected-media' / 'Лекція.wav'
    source.parent.mkdir()
    source.write_bytes(b'Synthetic receipt media; no acoustic inference.')
    output = tmp_path / 'selected-exports'
    output.mkdir()
    def configure(mode='complete', *, words=True):
        worker = tmp_path / ('worker-' + mode + ('-words' if words else '-segments') + '.py')
        code = fixture.SYNTHETIC_WORKER.replace('__MODE__', repr(mode))
        if mode == 'partial-report':
            # Actual receipt-producing child, but an explicitly unfinished
            # terminal protocol outcome must never become an assistant success.
            old = "result_state='partial' if status['failed'] else 'completed'"
            assert code.count(old) == 1
            code = code.replace(old, "result_state='partial'")
        if not words:
            code = code.replace("'word_timestamps': True,", "'word_timestamps': False,", 1)
        worker.write_text(code, encoding='utf-8')
        service.configure(Path(sys.executable).resolve(), worker, models)
        return worker
    configure()
    assistant = AssistantService(app)
    scope = Scope(workspace_id=workspace, input_roots=(source.parent,), output_root=output,
                  max_seconds=60, max_bytes=256 * 1024)
    args = {'path': str(source), 'name': 'transcript', 'model': 'small'}
    def plan(arguments=None, *, extra=(), chosen_scope=None):
        return Plan(chosen_scope or scope, (Step('audio.transcribe', arguments or args), *extra),
                    'One explicitly selected local recording')
    return SimpleNamespace(app=app, workspace=workspace, service=service, assistant=assistant,
        source=source, models=models, output=output, configure=configure, scope=scope, args=args, plan=plan)


def record(context, identity):
    return context.assistant.store.load(identity)


def retained_id(context, identity):
    return record(context, identity)['steps'][0]['intent']['transcription_job']


def must_not_run(*args, **kwargs):
    pytest.fail('No worker may be launched or duplicated at this boundary.')


def test_approved_json_uses_real_existing_worker_and_verifies_every_export(prepared):
    c = prepared
    plan = c.assistant.preview(json.dumps({'steps': [{'tool': 'audio.transcribe', 'arguments': c.args}]}), c.scope)
    identity = c.assistant.submit(plan)
    report = c.assistant.run(identity)
    assert report.status == 'complete', report.message
    result, = report.results
    job = c.service.verify(result['transcription_job'])
    assert len(c.service.list()) == 1 and job['done'] == 1
    assert not job['settings']['word_timestamps']
    assert len(result['artifacts']) == 5
    assert result['model']['sha256'] == job['run_context']['model_identity']['sha256']
    assert result['engine']['worker_sha256'] == c.service.configuration()['worker_sha256']
    assert result['acoustic_quality'] == 'NOT_RUN'
    assert c.service.configuration()['inference_qualified'] is False
    retained = c.service._path(job['id']) / 'input' / job['sources'][0]['filename']
    assert retained.read_bytes() == c.source.read_bytes()
    for artifact in result['artifacts']:
        raw = Path(artifact['path']).read_bytes()
        assert len(raw) == artifact['size'] and sha256(raw).hexdigest() == artifact['sha256']
    intent = record(c, identity)['steps'][0]['intent']
    assert intent['submission_started'] is True
    assert intent['transcript_export']['job'] == job['id']
    assert not (c.output / 'transcript' / 'INCOMPLETE.txt').exists()
    manifest = read_json(c.output / 'transcript' / 'transcription-manifest.json')
    assert manifest['status'] == 'completed' and manifest['settings'] == job['settings']


@pytest.mark.parametrize('problem', ['missing-model', 'outside-input', 'outside-output', 'input-budget', 'word-capability'])
def test_preview_rejects_unapproved_inputs_and_unsupported_capabilities_before_retention(prepared, problem):
    c = prepared
    args, scope = dict(c.args), c.scope
    if problem == 'missing-model':
        args.pop('model')
    elif problem == 'outside-input':
        outside = c.output / 'not-selected.wav'
        outside.write_bytes(b'Not selected for this plan.')
        args['path'] = str(outside)
    elif problem == 'outside-output':
        args['name'] = '../escape'
    elif problem == 'input-budget':
        scope = Scope(workspace_id=c.workspace, input_roots=(c.source.parent,), output_root=c.output,
                      max_bytes=1)
    else:
        c.configure(words=False)
        args['word_timestamps'] = True
    with pytest.raises((ValueError, PermissionError)):
        c.assistant.submit(c.plan(args, chosen_scope=scope))
    assert c.service.list() == ()
    assert not (c.output / 'transcript').exists()


@pytest.mark.parametrize('change', ['configuration', 'model'])
def test_run_admission_rechecks_expected_engine_before_any_worker_launch(prepared, monkeypatch, change):
    c = prepared
    identity = c.assistant.submit(c.plan())
    actual_run = c.service.run
    def changed_run(job_id, **kwargs):
        assert kwargs['expected_configuration'] and kwargs['expected_model']
        if change == 'configuration':
            c.configure(words=False)
        else:
            path = c.models / 'small' / 'model.bin'
            path.write_bytes(path.read_bytes() + b'\nChanged after the assistant persisted its binding.')
        return actual_run(job_id, **kwargs)
    monkeypatch.setattr(c.service, 'run', changed_run)
    monkeypatch.setattr(runtime, 'run_process', must_not_run)
    result = c.assistant.run(identity)
    assert result.status == 'attention' and 'changed after plan approval' in result.message
    job = c.service.get(retained_id(c, identity))
    assert job['status'] == 'ready' and job['run_context'] is None
    assert not (c.output / 'transcript').exists()


def test_real_child_stop_resumes_same_retained_job_and_refuses_changed_source(prepared, monkeypatch):
    c = prepared
    c.configure('pause-once')
    identity = c.assistant.submit(c.plan())
    job_id = [None]
    actual_submit = c.service.submit
    def retained(*args, **kwargs):
        value = actual_submit(*args, **kwargs)
        job_id[0] = value
        return value
    monkeypatch.setattr(c.service, 'submit', retained)
    def ready_to_stop():
        return job_id[0] is not None and (c.service._path(job_id[0]) / 'job_status.json').exists()
    first = c.assistant.run(identity, cancelled=ready_to_stop)
    assert first.status == 'paused', first.message
    assert c.service.get(job_id[0])['status'] == 'stopped'
    original = c.source.read_bytes()
    c.source.write_bytes(original + b'\nChanged selected input.')
    changed = c.assistant.resume(identity)
    assert changed.status == 'attention' and 'source or permissions changed' in changed.message
    assert len(c.service.list()) == 1
    c.source.write_bytes(original)
    monkeypatch.setattr(c.service, 'submit', must_not_run)
    resumed = c.assistant.resume(identity)
    assert resumed.status == 'complete', resumed.message
    assert retained_id(c, identity) == job_id[0] and len(c.service.list()) == 1


def test_unknown_outcome_after_actual_retained_input_publication_is_never_repeated(prepared, monkeypatch):
    c = prepared
    identity = c.assistant.submit(c.plan())
    class InterruptedHost(BaseException):
        pass
    actual_submit = c.service.submit
    def interrupted_submit(*args, **kwargs):
        actual_submit(*args, **kwargs)
        raise InterruptedHost('Host interrupted after durable job publication, before ID acknowledgement.')
    monkeypatch.setattr(c.service, 'submit', interrupted_submit)
    with pytest.raises(InterruptedHost):
        c.assistant.run(identity)
    saved = record(c, identity)
    assert saved['steps'][0]['intent']['submission_started'] is True
    assert 'transcription_job' not in saved['steps'][0]['intent']
    assert len(c.service.list()) == 1
    monkeypatch.setattr(c.service, 'submit', must_not_run)
    monkeypatch.setattr(c.service, 'run', must_not_run)
    report = c.assistant.resume(identity)
    assert report.status == 'attention' and 'outcome is unknown' in report.message
    assert len(c.service.list()) == 1 and not (c.output / 'transcript').exists()


def test_cancel_after_receipt_before_publication_keeps_destination_absent_and_resumes_export_only(prepared, monkeypatch):
    c = prepared
    identity = c.assistant.submit(c.plan())
    stop = Event()
    actual_export = c.service.export
    def stopped_export(job_id, destination, **kwargs):
        actual_callback = kwargs['before_publish']
        def received(receipt):
            actual_callback(receipt)
            assert record(c, identity)['steps'][0]['intent']['transcript_export'] == receipt
            assert not destination.exists()
            stop.set()
        return actual_export(job_id, destination, **{**kwargs, 'before_publish': received})
    monkeypatch.setattr(c.service, 'export', stopped_export)
    first = c.assistant.run(identity, cancelled=stop.is_set)
    assert first.status == 'paused', first.message
    job_id = retained_id(c, identity)
    run_id = c.service.get(job_id)['worker_run_id']
    assert c.service.get(job_id)['status'] == 'completed'
    assert not (c.output / 'transcript').exists()
    stop.clear()
    monkeypatch.setattr(c.service, 'export', actual_export)
    monkeypatch.setattr(c.service, 'run', must_not_run)
    completed = c.assistant.resume(identity)
    assert completed.status == 'complete', completed.message
    assert c.service.get(job_id)['worker_run_id'] == run_id


@pytest.mark.parametrize('later_step', [False, True])
def test_late_cancel_after_real_export_commit_acknowledges_result_and_stops_only_later_work(prepared, monkeypatch, later_step):
    c = prepared
    plan = c.plan(extra=(Step('library.catalog'),) if later_step else ())
    identity = c.assistant.submit(plan)
    stop = Event()
    actual_export = c.service.export
    def committed_export(*args, **kwargs):
        destination = actual_export(*args, **kwargs)
        assert (destination / 'transcription-manifest.json').is_file()
        assert not (destination / 'INCOMPLETE.txt').exists()
        stop.set()
        return destination
    monkeypatch.setattr(c.service, 'export', committed_export)
    report = c.assistant.run(identity, cancelled=stop.is_set)
    assert stop.is_set()
    assert report.completed == 1 and report.status == ('paused' if later_step else 'complete'), report.message
    saved = record(c, identity)
    assert saved['steps'][0]['status'] == 'complete' and saved['steps'][0]['result']['artifacts']
    if later_step:
        assert saved['steps'][1]['status'] == 'pending'
    run_id = c.service.get(retained_id(c, identity))['worker_run_id']
    stop.clear()
    monkeypatch.setattr(c.service, 'run', must_not_run)
    monkeypatch.setattr(c.service, 'export', must_not_run)
    completed = c.assistant.resume(identity)
    assert completed.status == 'complete', completed.message
    assert c.service.get(retained_id(c, identity))['worker_run_id'] == run_id


def test_lost_assistant_acknowledgement_recovers_exact_export_without_another_worker(prepared, monkeypatch):
    c = prepared
    identity = c.assistant.submit(c.plan())
    actual_export = c.service.export
    class InterruptedHost(BaseException):
        pass
    def committed_then_interrupted(*args, **kwargs):
        actual_export(*args, **kwargs)
        raise InterruptedHost('Host interrupted after the durable export commit.')
    monkeypatch.setattr(c.service, 'export', committed_then_interrupted)
    with pytest.raises(InterruptedHost):
        c.assistant.run(identity)
    receipt = record(c, identity)['steps'][0]['intent']['transcript_export']
    assert receipt['files'] and (c.output / 'transcript' / 'transcription-manifest.json').is_file()
    run_id = c.service.get(retained_id(c, identity))['worker_run_id']
    # Recovery is a byte/receipt operation; a removed current runtime is not
    # required to acknowledge this already completed user-owned export.
    monkeypatch.setattr(c.service, 'configuration', must_not_run)
    monkeypatch.setattr(c.service, 'run', must_not_run)
    monkeypatch.setattr(c.service, 'export', must_not_run)
    resumed = c.assistant.resume(identity)
    assert resumed.status == 'complete', resumed.message
    assert c.service.get(retained_id(c, identity))['worker_run_id'] == run_id


@pytest.mark.parametrize('damage', ['bytes', 'extra-file', 'incomplete'])
def test_completed_export_damage_rejects_replay_without_overwriting_or_inference(prepared, monkeypatch, damage):
    c = prepared
    identity = c.assistant.submit(c.plan())
    first = c.assistant.run(identity)
    assert first.status == 'complete', first.message
    target = c.output / 'transcript'
    if damage == 'bytes':
        changed = Path(first.results[0]['artifacts'][0]['path'])
        changed.write_bytes(b'x' * changed.stat().st_size)
    elif damage == 'extra-file':
        (target / 'unreviewed.txt').write_text('Must not be silently admitted.', encoding='utf-8')
    else:
        (target / 'INCOMPLETE.txt').write_text('Publication was not committed.', encoding='utf-8')
    before = {path.relative_to(target).as_posix(): path.read_bytes() for path in target.rglob('*') if path.is_file()}
    monkeypatch.setattr(c.service, 'run', must_not_run)
    monkeypatch.setattr(c.service, 'export', must_not_run)
    result = c.assistant.resume(identity)
    assert result.status == 'failed' and result.message
    assert before == {path.relative_to(target).as_posix(): path.read_bytes()
                      for path in target.rglob('*') if path.is_file()}


def test_export_budget_is_checked_before_the_selected_destination_is_created(prepared, monkeypatch):
    c = prepared
    identity = c.assistant.submit(c.plan())
    actual_export = c.service.export
    def limited_export(*args, **kwargs):
        # Exercise the production cumulative artifact+manifest ceiling with
        # already verified actual child results, rather than a fake return.
        return actual_export(*args, **{**kwargs, 'max_bytes': 1})
    monkeypatch.setattr(c.service, 'export', limited_export)
    result = c.assistant.run(identity)
    assert result.status == 'failed'
    assert c.service.get(retained_id(c, identity))['status'] == 'completed'
    assert not (c.output / 'transcript').exists()
    assert 'transcript_export' not in record(c, identity)['steps'][0]['intent']


def test_restored_canonical_plan_is_readable_without_old_paths_and_cannot_activate_adapter(prepared, monkeypatch):
    c = prepared
    identity = c.assistant.submit(c.plan())
    saved = record(c, identity)
    saved['plan']['steps'][0]['arguments'].pop('model')
    saved['plan']['steps'][0]['arguments']['path'] = 'Z:/unavailable/legacy.wav'
    saved['plan']['scope']['input_roots'] = ['Z:/unavailable']
    saved['plan']['scope']['output_root'] = 'Z:/unavailable-output'
    saved['data_dir'] = 'Z:/old-library'
    saved['plan_sha256'] = sha256(canonical(saved['plan']).encode('utf-8')).hexdigest()
    saved['steps'][0]['intent'] = {'transcription': {'adapter': 'supplied-v43-single-file/1'}}
    c.assistant.store.save(saved)
    path = c.assistant.store.path(identity)
    before_plan = read_json(path)['plan']
    prepare_restored_state(c.app.root / 'state', [{
        'filename': 'assistant/' + path.name, 'size_bytes': path.stat().st_size,
        'sha256': sha256(path.read_bytes()).hexdigest()}])
    monkeypatch.setattr(runtime, 'model_inventory', must_not_run)
    monkeypatch.setattr(c.service, 'configuration', must_not_run)
    restored, inert = c.assistant.review_saved_plan(identity)
    assert restored['restore_requires_review'] is True and restored['plan'] == before_plan
    assert inert.scope.input_roots == () and inert.scope.output_root is None
    assert 'model' not in inert.steps[0].arguments
    selection, = WorkflowAccess(c.assistant).entries()
    recipe = WorkflowAccess(c.assistant).recipe(selection)
    assert 'scope' not in recipe and recipe['steps'] == before_plan['steps']
    with pytest.raises(PermissionError, match='Restored assistant jobs'):
        c.assistant.resume(identity)


def test_foreign_adapter_checkpoint_requires_new_plan_even_without_restore_marker(prepared, monkeypatch):
    c = prepared
    identity = c.assistant.submit(c.plan())
    saved = record(c, identity)
    saved['steps'][0]['intent'] = {'transcription': {'adapter': 'supplied-v43-single-file/1'}}
    c.assistant.store.save(saved)
    monkeypatch.setattr(c.service, 'configuration', must_not_run)
    monkeypatch.setattr(runtime, 'run_process', must_not_run)
    report = c.assistant.run(identity)
    assert report.status == 'attention' and 'historical transcription adapter' in report.message
    assert c.service.list() == ()


def test_restored_retained_job_rejects_before_current_runtime_or_model_access(prepared, monkeypatch):
    c = prepared
    identity = c.assistant.submit(c.plan())
    actual_run = c.service.run
    def stopped_before_run(job_id, **kwargs):
        job = c.service.get(job_id)
        job['restore_requires_review'] = True
        atomic_json(c.service._path(job_id) / 'job.json', job, replace=True)
        return actual_run(job_id, **kwargs)
    monkeypatch.setattr(c.service, 'run', stopped_before_run)
    report = c.assistant.run(identity)
    assert report.status == 'attention' and 'Restored transcription job' in report.message
    monkeypatch.setattr(c.service, 'configuration', must_not_run)
    monkeypatch.setattr(runtime, 'model_inventory', must_not_run)
    resumed = c.assistant.resume(identity)
    assert resumed.status == 'attention' and 'was restored' in resumed.message
    assert not (c.output / 'transcript').exists()


@pytest.mark.parametrize('mode', ['partial-report', 'missing-output', 'model-mismatch'])
def test_incomplete_or_wrong_identity_worker_result_is_never_a_completed_tool(prepared, mode):
    c = prepared
    c.configure(mode)
    identity = c.assistant.submit(c.plan())
    report = c.assistant.run(identity)
    assert report.status == 'failed' and report.completed == 0, report
    job = c.service.get(retained_id(c, identity))
    assert job['status'] in ('partial', 'failed')
    if mode == 'partial-report':
        assert job['status'] == 'partial' and job['done'] == 1 and len(job['artifacts']) == 4
    assert not (c.output / 'transcript').exists()

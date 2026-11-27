"""The assistant delegates to the existing retained-input ASR service.

Actual synthetic children exercise both accepted file protocols. No private
model, audio, network provider or acoustic-quality qualification is used.
"""
import ast
from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
from threading import Event

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant import AssistantService, Plan, Scope, Step
from carr_library.assistant.state import atomic_json, read_json
from carr_library import transcription
from carr_library.durable_state import digest_file


def worker_fixture(name, constant):
    path = Path(__file__).parents[1] / 'transcription' / name
    module = ast.parse(path.read_text(encoding='utf-8'))
    return next(ast.literal_eval(item.value) for item in module.body
                if isinstance(item, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == constant
                        for target in item.targets))


@pytest.fixture(params=transcription.PROTOCOLS)
def prepared(request, tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Approved assistant transcription')
    runtime, inputs, outputs = (tmp_path / name for name in ('runtime', 'inputs', 'outputs'))
    for directory in (runtime, inputs, outputs):
        directory.mkdir()
    models = tmp_path / 'models'
    models.mkdir()
    native = app.transcription(workspace)
    if request.param == transcription.V5_PROTOCOL:
        worker = runtime / 'worker.py'
        worker.write_text(worker_fixture('test_v5_worker_profile.py', 'SYNTHETIC_V5_WORKER'),
                          encoding='utf-8')
        for name in ('local_models.py', 'transcript_features.py'):
            (runtime / name).write_text('# Explicit synthetic helper fixture.\n', encoding='utf-8')
        (models / 'small').mkdir()
        for name in ('model.bin', 'config.json', 'tokenizer.json'):
            (models / 'small' / name).write_bytes(('Synthetic model bytes: ' + name).encode())
    else:
        worker = runtime / 'worker.py'
        worker.write_text(worker_fixture('test_supplied_worker_bridge.py', 'SYNTHETIC_WORKER')
                          .replace('__MODE__', repr('complete')), encoding='utf-8')
    native.configure(Path(sys.executable), worker, models, protocol=request.param)
    source = inputs / 'Український запис.wav'
    source.write_bytes(b'Synthetic retained media, deliberately no acoustic inference.')
    service = AssistantService(app)
    scope = Scope(workspace, (inputs,), outputs, max_seconds=30, max_files=32, max_bytes=1024**2)
    plan = Plan(scope, (Step('audio.transcribe', {
        'path': str(source), 'name': 'Approved transcript', 'language': 'uk',
        'beam_size': 2, 'vad_filter': False}),))
    return app, workspace, native, service, source, outputs, plan, worker, models


def checkpoint(service, identity):
    return service.store.load(identity)['steps'][0]['intent']


def no_worker(*args, **kwargs):
    pytest.fail('A verified retained result must not start another worker.')


def test_default_product_route_uses_one_existing_job_and_receipt_authority(prepared):
    app, workspace, native, service, source, outputs, plan, worker, models = prepared
    original = source.read_bytes()
    identity = service.submit(plan)
    approved = checkpoint(service, identity)['library_transcription']
    assert approved['workspace'] == workspace
    assert approved['sha256'] == sha256(original).hexdigest()
    report = service.run(identity)
    assert report.status == 'complete', report
    intent = checkpoint(service, identity)
    child = native.verify(intent['transcription_job'])
    assert child['status'] == 'completed'
    assert child['settings']['language'] == 'uk'
    assert child['settings']['beam_size'] == 2
    assert child['settings']['vad_filter'] is False
    assert child['sources'][0]['sha256'] == approved['sha256']
    assert len(native.list()) == 1
    destination = outputs / 'Approved transcript'
    assert len(intent['transcription_export']) == len(report.results[0]['artifacts']) == 7
    exported = read_json(destination / 'transcription-manifest.json')
    assert exported['job'] == child['id'] and exported['protocol'] == child['protocol']
    assert source.read_bytes() == original
    assert not list(outputs.glob('.pending-*'))
    assert AssistantService(LibraryApp(app.root)).resume(identity).status == 'complete'
    assert len(native.list()) == 1


def test_default_transcription_requires_current_library_and_configured_runtime(prepared):
    app, workspace, native, service, source, outputs, plan, worker, models = prepared
    with pytest.raises(PermissionError, match='existing library'):
        service.submit(Plan(replace(plan.scope, workspace_id=None), plan.steps))
    (native.root / 'engine.json').unlink()
    with pytest.raises(RuntimeError, match='Configure and verify'):
        service.submit(plan)
    assert native.list() == ()


@pytest.mark.parametrize('change', ('source', 'engine'))
def test_approved_source_and_engine_cannot_change_before_retention_or_child(prepared, monkeypatch, change):
    app, workspace, native, service, source, outputs, plan, worker, models = prepared
    identity = service.submit(plan)
    target = source if change == 'source' else worker
    before = target.stat()
    raw = target.read_bytes()
    target.write_bytes(bytes((raw[0] ^ 1,)) + raw[1:])
    os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
    monkeypatch.setattr(transcription, 'run_process', no_worker)
    report = service.run(identity)
    assert report.status == 'failed' and report.completed == 0, report
    assert native.list() == ()
    assert not (outputs / 'Approved transcript').exists()


def test_same_source_other_job_cannot_replace_this_assistant_step(prepared, monkeypatch):
    app, workspace, native, service, source, outputs, plan, worker, models = prepared
    identity = service.submit(plan)
    record = service.store.load(identity)
    binding = record['steps'][0]['intent']['library_transcription']
    other = native.submit([source], title='Independent user transcription',
                          options=binding['settings'], protocol=binding['engine']['protocol'])
    record['steps'][0]['intent']['transcription_job'] = other
    service.store.save(record)
    monkeypatch.setattr(transcription, 'run_process', no_worker)
    report = service.run(identity)
    assert report.status == 'failed' and 'differs' in report.message, report
    assert native.get(other)['status'] == 'ready'
    assert not (outputs / 'Approved transcript').exists()


@pytest.mark.parametrize('stop', ('cancel-after-publish', 'lost-export-reply'))
def test_committed_export_recovers_exact_inventory_without_another_child(prepared, monkeypatch, stop):
    app, workspace, native, service, source, outputs, plan, worker, models = prepared
    identity = service.submit(plan)
    stopped = Event()
    original = transcription.TranscriptionService.export
    def published(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        if stop == 'cancel-after-publish':
            stopped.set()
        else:
            raise OSError('Synthetic lost reply after actual export publication.')
        return result
    monkeypatch.setattr(transcription.TranscriptionService, 'export', published)
    report = service.run(identity, cancelled=stopped.is_set)
    assert report.status == ('paused' if stop == 'cancel-after-publish' else 'failed'), report
    assert report.completed == 0
    intent = checkpoint(service, identity)
    child = native.verify(intent['transcription_job'])
    destination = outputs / 'Approved transcript'
    manifest = destination / 'transcription-manifest.json'
    before = manifest.stat()
    assert manifest.is_file() and len(intent['transcription_export']) == 7
    stopped.clear()
    monkeypatch.setattr(transcription, 'run_process', no_worker)
    monkeypatch.setattr(transcription.TranscriptionService, 'export', no_worker)
    resumed = AssistantService(LibraryApp(app.root)).resume(identity)
    assert resumed.status == 'complete', resumed
    assert native.get(child['id'])['worker_run_id'] == child['worker_run_id']
    assert manifest.stat().st_mtime_ns == before.st_mtime_ns
    assert len(native.list()) == 1


def test_prepublication_cancel_preserves_completed_child_and_exact_export_intent(prepared, monkeypatch):
    app, workspace, native, service, source, outputs, plan, worker, models = prepared
    identity = service.submit(plan)
    stopped = Event()
    save = service.store.save
    def checkpoint_then_cancel(record):
        save(record)
        if record['steps'][0]['intent'].get('transcription_export'):
            stopped.set()
    monkeypatch.setattr(service.store, 'save', checkpoint_then_cancel)
    report = service.run(identity, cancelled=stopped.is_set)
    assert report.status == 'paused', report
    intent = checkpoint(service, identity)
    assert native.verify(intent['transcription_job'])['status'] == 'completed'
    assert intent['transcription_export']
    assert not (outputs / 'Approved transcript').exists()
    assert not list(outputs.glob('.pending-*'))
    stopped.clear()
    monkeypatch.setattr(transcription, 'run_process', no_worker)
    assert AssistantService(LibraryApp(app.root)).resume(identity).status == 'complete'


@pytest.mark.parametrize('budget', ('bytes', 'files'))
def test_approved_output_budgets_stop_before_publishing_any_directory(prepared, budget):
    app, workspace, native, service, source, outputs, plan, worker, models = prepared
    scope = replace(plan.scope, max_bytes=256) if budget == 'bytes' else replace(plan.scope, max_files=3)
    identity = service.submit(Plan(scope, plan.steps))
    report = service.run(identity)
    assert report.status == 'failed' and report.completed == 0, report
    assert 'budget' in report.message
    assert native.verify(checkpoint(service, identity)['transcription_job'])['status'] == 'completed'
    assert not (outputs / 'Approved transcript').exists()
    assert not list(outputs.glob('.pending-*'))


def test_an_unrelated_destination_is_never_adopted_or_overwritten(prepared, monkeypatch):
    app, workspace, native, service, source, outputs, plan, worker, models = prepared
    identity = service.submit(plan)
    destination = outputs / 'Approved transcript'
    destination.mkdir()
    owner_file = destination / 'owner.txt'
    owner_file.write_bytes(b'Independent owner work.')
    monkeypatch.setattr(transcription, 'run_process', no_worker)
    report = service.run(identity)
    assert report.status == 'attention' and report.completed == 0, report
    assert owner_file.read_bytes() == b'Independent owner work.'
    assert native.list() == ()


def test_changed_completed_export_cannot_be_silently_rebuilt(prepared, monkeypatch):
    app, workspace, native, service, source, outputs, plan, worker, models = prepared
    identity = service.submit(plan)
    original = transcription.TranscriptionService.export
    def lost_reply(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise OSError('Synthetic lost export reply.')
    monkeypatch.setattr(transcription.TranscriptionService, 'export', lost_reply)
    assert service.run(identity).status == 'failed'
    destination = outputs / 'Approved transcript'
    manifest = destination / 'transcription-manifest.json'
    original_bytes = manifest.read_bytes()
    changed = b'x' + original_bytes[1:]
    manifest.write_bytes(changed)
    monkeypatch.setattr(transcription, 'run_process', no_worker)
    monkeypatch.setattr(transcription.TranscriptionService, 'export', no_worker)
    report = service.resume(identity)
    assert report.status == 'failed' and report.completed == 0
    assert 'changed' in report.message
    assert manifest.read_bytes() == changed


def test_interrupted_input_pointer_never_resubmits_an_unknown_job(prepared, monkeypatch):
    app, workspace, native, service, source, outputs, plan, worker, models = prepared
    identity = service.submit(plan)
    record = service.store.load(identity)
    record['steps'][0]['intent']['transcription_preparing'] = True
    service.store.save(record)
    monkeypatch.setattr(transcription.TranscriptionService, 'submit', no_worker)
    report = service.run(identity)
    assert report.status == 'failed' and 'preparation stopped' in report.message
    assert native.list() == ()


def test_workspace_archival_rejects_the_saved_default_job(prepared, monkeypatch):
    app, workspace, native, service, source, outputs, plan, worker, models = prepared
    identity = service.submit(plan)
    app.archive_workspace(workspace)
    monkeypatch.setattr(transcription, 'run_process', no_worker)
    report = service.run(identity)
    assert report.status == 'failed' and report.completed == 0
    assert 'missing or archived' in report.message
    assert not (outputs / 'Approved transcript').exists()


def test_selected_v5_model_is_bound_before_any_worker(prepared, monkeypatch):
    app, workspace, native, service, source, outputs, plan, worker, models = prepared
    if native.configuration()['protocol'] != transcription.V5_PROTOCOL:
        # The receipt profile delegates model identity to its verified worker;
        # it has no pre-admission model claim. Still exercise its complete path.
        identity = service.submit(plan)
        assert checkpoint(service, identity)['library_transcription']['model_identity'] is None
        return
    identity = service.submit(plan)
    target = models / 'small' / 'model.bin'
    raw = target.read_bytes()
    before = target.stat()
    target.write_bytes(bytes((raw[0] ^ 1,)) + raw[1:])
    os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
    monkeypatch.setattr(transcription, 'run_process', no_worker)
    report = service.run(identity)
    assert report.status == 'failed' and report.completed == 0
    assert native.list() == ()


def test_new_library_intent_cannot_be_rerouted_to_standalone_v43(prepared, monkeypatch):
    from carr_library.integrations.supplied_transcription import SuppliedTranscriptionAdapter
    app, workspace, native, service, source, outputs, plan, worker, models = prepared
    identity = service.submit(plan)
    explicit = object.__new__(SuppliedTranscriptionAdapter)
    monkeypatch.setattr(SuppliedTranscriptionAdapter, 'transcribe', no_worker)
    report = AssistantService(app, transcription=explicit).resume(identity)
    assert report.status == 'failed' and 'existing library service' in report.message
    assert native.list() == ()
    assert not (outputs / 'Approved transcript').exists()


def test_actual_child_cancellation_retains_one_job_for_explicit_resume(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Actual cancellation routing')
    native = app.transcription(workspace)
    models, outputs = tmp_path / 'models', tmp_path / 'outputs'
    models.mkdir()
    outputs.mkdir()
    worker = tmp_path / 'worker.py'
    worker.write_text(worker_fixture('test_supplied_worker_bridge.py', 'SYNTHETIC_WORKER')
                      .replace('__MODE__', repr('pause-once')), encoding='utf-8')
    native.configure(Path(sys.executable), worker, models)
    source = tmp_path / 'source.wav'
    source.write_bytes(b'Synthetic cancellation routing source.')
    service = AssistantService(app)
    scope = Scope(workspace, (source,), outputs, max_seconds=30)
    plan = Plan(scope, (Step('audio.transcribe', {'path': str(source), 'name': 'result'}),))
    identity = service.submit(plan)

    def actual_worker_started():
        if not native.jobs_root.exists():
            return False
        return any((job / 'job_status.json').exists()
                   for job in native.jobs_root.iterdir() if not job.name.startswith('.'))

    first = service.run(identity, cancelled=actual_worker_started)
    assert first.status == 'paused' and first.completed == 0, first
    child_id = checkpoint(service, identity)['transcription_job']
    child = native.get(child_id)
    assert child['status'] == 'stopped'
    assert (native._path(child_id) / 'continued').is_file()
    assert not (outputs / 'result').exists()
    assert len(native.list()) == 1
    resumed = AssistantService(LibraryApp(app.root)).resume(identity)
    assert resumed.status == 'complete', resumed
    assert checkpoint(service, identity)['transcription_job'] == child_id
    assert native.verify(child_id)['status'] == 'completed'
    assert len(native.list()) == 1

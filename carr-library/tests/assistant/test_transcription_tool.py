"""Actual current-service workflow recovery with the harmless existing CLI peer.

No private worker, model weights, recognition result or external request is used.
"""
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
import shutil
from threading import Event

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant import AssistantService, Plan, Scope, Step
from carr_library.assistant import transcription as bridge
from carr_library.assistant.transcription_model import definition_digest, job_identity
from carr_library.assistant.validation import validate_job
from carr_library.audio import jobs
from carr_library import transcription_service as asr
from carr_library import transcription_exports
from carr_library.library_backup import create_backup, restore_backup, verify_backup
from tests.test_transcription_service import prepared_environment, real_protocol_child


@pytest.fixture
def environment(prepared_environment, real_protocol_child, tmp_path, monkeypatch):
    original, manifest, source, settings = prepared_environment(
        settings={'output_txt': True, 'output_srt': True, 'output_json': True})
    app = LibraryApp(original.library_root)
    app.create_workspace('Retained originals')
    output = tmp_path / 'exports'
    output.mkdir()
    service = AssistantService(app)
    args = dict(path=str(source), name='Transcript ü', runtime_manifest=str(manifest),
                runtime_manifest_sha256=sha256(manifest.read_bytes()).hexdigest(), settings=settings)
    plan = Plan(Scope(input_roots=(source.parent,), output_root=output, max_seconds=30,
                      max_bytes=8 * 1024**2, max_files=20), (Step('audio.transcribe', args),))
    from carr_library.audio import capture_process
    start = capture_process.start_owned_process
    launches = []
    def observed(arguments, **options):
        launches.append(tuple(arguments))
        return start(arguments, **options)
    monkeypatch.setattr(capture_process, 'start_owned_process', observed)
    return app, service, plan, source, manifest, launches


def run(environment):
    app, service, plan, source, manifest, launches = environment
    identity = service.submit(plan)
    result = service.run(identity)
    assert result.status == 'complete', result
    return identity, result


def forbid(*args, **kwargs):
    pytest.fail('This boundary must not launch, prepare or publish another transcription.')


def test_actual_owned_cli_and_new_export_complete_then_verify_without_runtime(environment, monkeypatch):
    app, service, plan, source, manifest, launches = environment
    original = source.read_bytes()
    identity, result = run(environment)
    saved = service.store.load(identity)
    item = result.results[0]
    expected = job_identity(identity, 0)
    assert item['job_id'] == expected and len(launches) == 1
    assert item['runtime_manifest_sha256'] == plan.steps[0].arguments['runtime_manifest_sha256']
    assert item['source_sha256'] == sha256(original).hexdigest()
    assert {row['format'] for row in saved['steps'][0]['intent']['transcription_export']['outputs']} == {'txt', 'srt', 'json'}
    assert len(item['artifacts']) == 4
    assert source.read_bytes() == original
    managed = asr.TranscriptionService(app.root)
    definition = managed.definition(expected)
    assert (managed._job(expected) / definition['inputs'][0]['path']).read_bytes() == original
    execution = (managed._job(expected) / 'execution.json').read_bytes()
    source.unlink()
    shutil.rmtree(manifest.parent)
    monkeypatch.setattr(asr.TranscriptionService, 'prepare', forbid)
    monkeypatch.setattr(asr.TranscriptionService, 'run', forbid)
    monkeypatch.setattr(asr, 'review_runtime', forbid)
    monkeypatch.setattr(transcription_exports, 'export_results', forbid)
    reopened = AssistantService(LibraryApp(app.root)).resume(identity)
    assert reopened.status == 'complete' and reopened.results == result.results
    assert len(launches) == 1 and (managed._job(expected) / 'execution.json').read_bytes() == execution


def test_prepare_commit_before_assistant_checkpoint_recovers_the_same_job(environment, monkeypatch):
    app, service, plan, _, _, launches = environment
    actual = asr.TranscriptionService.prepare
    prepared = []
    def fail_after_prepare(*args, **options):
        value = actual(*args, **options)
        prepared.append(value)
        raise InterruptedError('Synthetic interruption after the actual prepare publication.')
    monkeypatch.setattr(asr.TranscriptionService, 'prepare', fail_after_prepare)
    identity = service.submit(plan)
    assert service.run(identity).status == 'paused'
    intent = service.store.load(identity)['steps'][0]['intent']
    assert prepared == [job_identity(identity, 0)] and 'definition_sha256' not in intent
    assert not launches
    monkeypatch.setattr(asr.TranscriptionService, 'prepare', forbid)
    assert service.resume(identity).status == 'complete'
    assert len(launches) == 1
    assert [row['id'] for row in asr.TranscriptionService(app.root).jobs()] == prepared


def test_cancel_after_durable_intent_creates_no_job_and_keeps_unique_resume_identity(environment, monkeypatch):
    app, service, plan, _, _, launches = environment
    identity = service.submit(plan)
    stop = Event()
    actual_save = service.store.save
    def save(record):
        actual_save(record)
        if record['steps'][0]['intent'] and record['status'] == 'running':
            stop.set()
    monkeypatch.setattr(service.store, 'save', save)
    assert service.run(identity, cancelled=stop.is_set).status == 'paused'
    expected = job_identity(identity, 0)
    assert service.store.load(identity)['steps'][0]['intent']['transcription']['job_id'] == expected
    assert not (app.root / 'transcription-jobs' / expected).exists() and not launches
    monkeypatch.setattr(service.store, 'save', actual_save)
    stop.clear()
    assert service.resume(identity).status == 'complete'
    assert len(launches) == 1


@pytest.mark.parametrize('damage', ['settings', 'source', 'runtime'], ids=['settings', 'source', 'runtime'])
def test_an_existing_different_prepared_job_is_not_adopted_or_run(environment, monkeypatch, damage):
    app, service, plan, _, _, launches = environment
    prepare = asr.TranscriptionService.prepare
    def pause(*args, **options):
        value = prepare(*args, **options)
        path = app.root / 'transcription-jobs' / value / 'job.json'
        definition = jobs.read_json(path)
        if damage == 'settings':
            definition['settings']['beam_size'] = 2
        elif damage == 'source':
            definition['source_provenance'][0]['path'] += '-different'
        else:
            definition['runtime_manifest_sha256'] = 'f' * 64
        jobs.atomic_json(path, definition)
        raise InterruptedError('Synthetic prepare/intent boundary.')
    monkeypatch.setattr(asr.TranscriptionService, 'prepare', pause)
    identity = service.submit(plan)
    assert service.run(identity).status == 'paused'
    monkeypatch.setattr(asr.TranscriptionService, 'prepare', forbid)
    report = service.resume(identity)
    assert report.status == 'failed' and 'differs' in report.message
    assert not launches


def test_run_pin_rejects_a_definition_changed_after_assistant_admission(environment, monkeypatch):
    app, service, plan, _, _, launches = environment
    actual = asr.TranscriptionService.run
    def changed(owner, identity, runtime, **options):
        assert len(options['expected_definition_sha256']) == 64
        path = owner._job(identity) / 'job.json'
        value = jobs.read_json(path)
        value['title'] = 'Changed after workflow inspection'
        jobs.atomic_json(path, value)
        return actual(owner, identity, runtime, **options)
    monkeypatch.setattr(asr.TranscriptionService, 'run', changed)
    identity = service.submit(plan)
    result = service.run(identity)
    assert result.status == 'failed' and 'reviewed workflow' in result.message
    assert not launches and not (app.root / 'transcription-jobs' / job_identity(identity, 0) / 'execution.json').exists()


@pytest.mark.parametrize('damage', ['output', 'receipt', 'extra'], ids=['output', 'receipt', 'extra'])
def test_completed_export_tampering_is_visible_and_never_retranscribes(environment, monkeypatch, damage):
    app, service, plan, _, _, launches = environment
    identity, report = run(environment)
    destination = Path(report.results[0]['directory'])
    if damage == 'output':
        Path(report.results[0]['artifacts'][1]['path']).write_bytes(b'Changed export')
    elif damage == 'receipt':
        (destination / 'transcription-export.json').write_bytes(b'{}')
    else:
        (destination / 'unexpected.txt').write_bytes(b'Not this completed job')
    monkeypatch.setattr(asr.TranscriptionService, 'run', forbid)
    monkeypatch.setattr(transcription_exports, 'export_results', forbid)
    result = service.resume(identity)
    assert result.status == 'failed' and result.completed == 0 and len(launches) == 1


def test_published_export_before_acknowledgement_recovers_without_copy_or_inference(environment, monkeypatch):
    app, service, plan, _, _, launches = environment
    actual = transcription_exports.export_results
    def after_publication(*args, **options):
        actual(*args, **options)
        raise OSError('Synthetic export acknowledgement loss.')
    monkeypatch.setattr(transcription_exports, 'export_results', after_publication)
    identity = service.submit(plan)
    assert service.run(identity).status == 'failed'
    destination = plan.scope.output(plan.steps[0].arguments['name'])
    before = {path.relative_to(destination): path.read_bytes() for path in destination.rglob('*') if path.is_file()}
    monkeypatch.setattr(transcription_exports, 'export_results', forbid)
    monkeypatch.setattr(asr.TranscriptionService, 'run', forbid)
    assert service.resume(identity).status == 'complete'
    assert len(launches) == 1
    assert {path.relative_to(destination): path.read_bytes() for path in destination.rglob('*') if path.is_file()} == before


def test_accepted_completion_damage_does_not_become_an_implicit_new_run(environment, monkeypatch):
    app, service, plan, _, _, launches = environment
    actual = transcription_exports.export_results
    monkeypatch.setattr(transcription_exports, 'export_results', lambda *a, **k: (_ for _ in ()).throw(InterruptedError('Before export')))
    identity = service.submit(plan)
    assert service.run(identity).status == 'paused'
    job = app.root / 'transcription-jobs' / job_identity(identity, 0)
    (job / 'logs/completion-v1.json').write_bytes(b'{}')
    monkeypatch.setattr(transcription_exports, 'export_results', actual)
    monkeypatch.setattr(asr.TranscriptionService, 'run', forbid)
    assert service.resume(identity).status == 'failed'
    assert len(launches) == 1 and not plan.scope.output(plan.steps[0].arguments['name']).exists()


def test_output_budget_is_checked_before_export_intent_or_publication(environment):
    app, service, plan, source, _, launches = environment
    plan = replace(plan, scope=replace(plan.scope, max_bytes=max(source.stat().st_size, 100)))
    identity = service.submit(plan)
    result = service.run(identity)
    assert result.status == 'failed' and 'budget' in result.message
    assert 'transcription_export' not in service.store.load(identity)['steps'][0]['intent']
    assert not plan.scope.output(plan.steps[0].arguments['name']).exists()
    assert len(launches) == 1


@pytest.mark.parametrize('change', ['scope', 'pin', 'settings', 'unknown'], ids=['scope', 'pin', 'settings', 'unknown'])
def test_explicit_plan_admission_refuses_incomplete_or_changed_authority(environment, change):
    app, service, plan, source, manifest, launches = environment
    args = deepcopy(plan.steps[0].arguments)
    if change == 'scope':
        args['path'] = str(manifest)  # Outside the supported input suffix.
    elif change == 'pin':
        args['runtime_manifest_sha256'] = '0' * 64
    elif change == 'settings':
        args['settings'].pop('language')
    else:
        args['execute_shell'] = True
    candidate = Plan(plan.scope, (Step('audio.transcribe', args),))
    with pytest.raises((ValueError, PermissionError)):
        service.submit(candidate)
    assert service.jobs() == () and not launches


@pytest.mark.parametrize('identity', ['', '../job', 'A' * 32, 'a' * 31, 7, True],
                         ids=['empty', 'traversal', 'uppercase', 'short', 'integer', 'boolean'])
def test_caller_job_identity_is_rejected_before_runtime_or_store_mutation(
        prepared_environment, monkeypatch, identity):
    service, manifest, source, settings = prepared_environment()
    monkeypatch.setattr(asr, 'review_runtime', forbid)
    before = tuple(service.library_root.rglob('*'))
    with pytest.raises(ValueError, match='32 lowercase'):
        service.prepare([source], manifest, settings, job_id=identity)
    assert tuple(service.library_root.rglob('*')) == before


def test_caller_job_identity_never_overwrites_existing_definition(prepared_environment):
    service, manifest, source, settings = prepared_environment()
    identity = 'a' * 32
    assert service.prepare([source], manifest, settings, job_id=identity) == identity
    before = (service._job(identity) / 'job.json').read_bytes()
    with pytest.raises(FileExistsError):
        service.prepare([source], manifest, settings, job_id=identity)
    assert (service._job(identity) / 'job.json').read_bytes() == before


def test_templates_and_complete_backup_preserve_bindings_but_never_activate_archived_runtime(environment, tmp_path, monkeypatch):
    app, service, plan, source, manifest, launches = environment
    template = service.workflow_templates().save('Explicit local transcript', plan)
    assert not service.jobs() and not launches
    identity, result = run(environment)
    saved = service.store.load(identity)
    monkeypatch.setattr(asr, 'review_runtime', forbid)
    raw = deepcopy(saved)
    raw['requires_reapproval'] = True
    validate_job(raw)  # Pure validation must not open runtime or old source paths.
    backup = tmp_path / 'full-backup'
    checksum = create_backup(app, backup)
    verify_backup(backup, checksum)
    restored_root = tmp_path / 'restored'
    restore_backup(backup, restored_root, checksum)
    restored = AssistantService(LibraryApp(restored_root))
    assert restored.get(identity).status == 'attention'
    assert restored.workflow_templates().get(template.id).draft == template.draft
    with pytest.raises(PermissionError, match='review and submit'):
        restored.resume(identity)
    assert len(launches) == 1


@pytest.mark.parametrize('field', ['job', 'source', 'destination', 'coverage', 'result'],
                         ids=['job', 'source', 'destination', 'coverage', 'result'])
def test_snapshot_validation_rejects_forged_execution_or_export_binding(environment, field):
    _, service, _, _, _, _ = environment
    identity, _ = run(environment)
    record = service.store.load(identity)
    intent = record['steps'][0]['intent']
    if field == 'job':
        intent['transcription']['job_id'] = '0' * 32
    elif field == 'source':
        intent['transcription']['source'] += '-foreign'
    elif field == 'destination':
        intent['transcription']['destination'] += '-foreign'
    elif field == 'coverage':
        intent['transcription_export']['outputs'][0]['source_indexes'] = [1]
    else:
        record['steps'][0]['result']['runtime_manifest_sha256'] = 'f' * 64
    with pytest.raises(ValueError):
        validate_job(record)


def test_post_move_directory_sync_failure_requires_successful_verified_retry(environment, monkeypatch):
    app, service, plan, _, _, launches = environment
    destination = plan.scope.output(plan.steps[0].arguments['name'])
    sync = jobs._sync_directory
    refused = []
    def fail_parent(path):
        if Path(path) == destination.parent and destination.exists():
            refused.append(Path(path))
            raise OSError('Synthetic post-move directory sync failure.')
        return sync(path)
    monkeypatch.setattr(jobs, '_sync_directory', fail_parent)
    identity = service.submit(plan)
    assert service.run(identity).status == 'failed'
    assert destination.is_dir() and refused
    before = {p.relative_to(destination): p.read_bytes() for p in destination.rglob('*') if p.is_file()}
    monkeypatch.setattr(asr.TranscriptionService, 'run', forbid)
    monkeypatch.setattr(transcription_exports, 'export_results', forbid)
    assert service.resume(identity).status == 'failed'
    assert {p.relative_to(destination): p.read_bytes() for p in destination.rglob('*') if p.is_file()} == before
    monkeypatch.setattr(jobs, '_sync_directory', sync)
    assert service.resume(identity).status == 'complete' and len(launches) == 1


def test_guided_native_help_and_cli_explain_the_exact_runtime_contract(monkeypatch, capsys):
    from carr_library.assistant import HELP
    from carr_library.assistant import cli, llm
    from carr_library.assistant.models import TOOLS
    assert 'audio.transcribe' in TOOLS
    for value in ('audio.transcribe', 'runtime_manifest', 'runtime_manifest_sha256', 'settings'):
        assert value in HELP and value in llm.SYSTEM
    monkeypatch.setattr(asr, 'review_runtime', forbid)
    with pytest.raises(SystemExit) as error:
        cli.main(['--help'])
    assert error.value.code == 0
    output = capsys.readouterr().out
    assert 'audio.transcribe' in output and 'all 17 explicit settings' in output


@pytest.mark.parametrize('phase', ['hash', 'copy'], ids=['mid-hash', 'mid-copy'])
def test_cancel_inside_shared_input_reader_or_copy_is_durable_pause(environment, monkeypatch, phase):
    app, service, plan, source, _, launches = environment
    stop, reached = Event(), []
    actual_hash, actual_copy = jobs.sha256_file, asr.publish_new
    def hashing(path, **options):
        if phase == 'hash' and Path(path) == source and not reached:
            reached.append('hash')
            stop.set()
        return actual_hash(path, **options)
    def copying(path, destination, **options):
        if phase == 'copy' and Path(path) == source and not reached:
            reached.append('copy')
            stop.set()
        return actual_copy(path, destination, **options)
    monkeypatch.setattr(jobs, 'sha256_file', hashing)
    monkeypatch.setattr(asr, 'publish_new', copying)
    identity = service.submit(plan)
    report = service.run(identity, cancelled=stop.is_set)
    assert reached == [phase]
    assert report.status == 'paused' and report.completed == 0 and not launches
    assert not (app.root / 'transcription-jobs' / job_identity(identity, 0)).exists()
    assert not list((app.root / 'transcription-jobs').glob('.prepare-*'))
    stop.clear()
    assert service.resume(identity).status == 'complete' and len(launches) == 1


def test_preview_cancellation_reaches_actual_manifest_inventory_without_creating_a_job(environment, monkeypatch):
    app, _, plan, _, _, launches = environment
    stop = Event()
    service = AssistantService(app, validation_cancelled=stop.is_set)
    review = asr.review_runtime
    observed = []
    def reviewing(path, settings, **options):
        assert callable(options['cancelled'])
        observed.append(path)
        stop.set()
        # The actual manifest load/inventory receives the checked existing
        # signal. Its shared readers must not convert it to a failed job.
        return review(path, settings, **options)
    monkeypatch.setattr(asr, 'review_runtime', reviewing)
    with pytest.raises(InterruptedError, match='stopped'):
        service.preview(plan.to_json(), plan.scope)
    assert len(observed) == 1 and service.jobs() == () and not launches


def test_invalid_validation_callback_cannot_create_assistant_state(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    with pytest.raises(TypeError, match='callable cancellation'):
        AssistantService(app, validation_cancelled=False)
    assert not (app.root / 'state' / 'assistant').exists()


def _unicode_cli_arguments(environment, tmp_path):
    app, _, plan, _, _, _ = environment
    name = 'Розшифрування Україна Ω'
    arguments = dict(plan.steps[0].arguments, name=name)
    plan = replace(plan, steps=(Step('audio.transcribe', arguments),))
    instruction = tmp_path / 'reviewed-plan.json'
    instruction.write_text(plan.to_json(), encoding='utf-8')
    command = ['--data-dir', str(app.root), '--instructions', str(instruction),
               '--input-root', str(plan.scope.input_roots[0]),
               '--output-root', str(plan.scope.output_root), '--run']
    for field in ('max_bytes', 'max_files', 'max_steps', 'max_seconds'):
        command.extend(['--' + field.replace('_', '-'), str(getattr(plan.scope, field))])
    return command, plan.scope.output(name)


def test_cli_actual_commit_and_reopen_use_lossless_json_on_cp1252_stdout(environment, tmp_path, monkeypatch):
    import io
    import json
    import sys
    from carr_library.assistant import cli
    app, service, _, _, _, launches = environment
    arguments, destination = _unicode_cli_arguments(environment, tmp_path)
    output, diagnostics = io.BytesIO(), io.BytesIO()
    stdout = io.TextIOWrapper(output, encoding='cp1252', errors='strict', write_through=True, newline='\n')
    stderr = io.TextIOWrapper(diagnostics, encoding='cp1252', errors='strict', write_through=True, newline='\n')
    with monkeypatch.context() as patch:
        patch.setattr(sys, 'stdout', stdout)
        patch.setattr(sys, 'stderr', stderr)
        assert cli.main(arguments) == 0
        stdout.flush()
        first = output.getvalue()
        result = json.loads(first.decode('ascii'))
        assert result['status'] == 'complete'
        assert result['results'][0]['directory'] == str(destination)
        assert service.get(result['id']).status == 'complete'
        assert len(launches) == 1 and destination.is_dir()
        output.seek(0)
        output.truncate()
        assert cli.main(['--data-dir', str(app.root), '--resume', result['id'], '--run']) == 0
        stdout.flush()
        reopened = json.loads(output.getvalue().decode('ascii'))
        assert reopened['results'] == result['results'] and len(launches) == 1
    assert b'\\u0420' in first  # JSON decoding, not console substitution, restores the exact name.


@pytest.mark.parametrize('failure', ['write', 'flush'], ids=['stderr-write', 'stderr-flush'])
def test_cli_progress_stream_failure_cannot_relabel_an_actual_committed_transcription(
        environment, tmp_path, monkeypatch, failure):
    import io
    import json
    import sys
    from carr_library.assistant import cli
    _, service, _, _, _, launches = environment
    arguments, destination = _unicode_cli_arguments(environment, tmp_path)
    class Observer:
        armed = False
        faults = 0
        def write(self, value):
            if self.armed and failure == 'write':
                self.faults += 1
                raise OSError('Synthetic disconnected progress stream.')
            return len(value)
        def flush(self):
            if self.armed and failure == 'flush':
                self.faults += 1
                raise OSError('Synthetic disconnected progress flush.')
    observer = Observer()
    actual_run = AssistantService.run
    def after_actual_commit(owner, identity, **options):
        report = actual_run(owner, identity, **options)
        assert report.status == 'complete'
        assert owner.store.load(identity)['status'] == 'complete'
        observer.armed = True
        options['progress'](1, 1, 'Збережено: ' + str(destination))
        return report
    monkeypatch.setattr(AssistantService, 'run', after_actual_commit)
    output = io.BytesIO()
    stdout = io.TextIOWrapper(output, encoding='cp1252', errors='strict', write_through=True, newline='\n')
    with monkeypatch.context() as patch:
        patch.setattr(sys, 'stdout', stdout)
        patch.setattr(sys, 'stderr', observer)
        assert cli.main(arguments) == 0
        stdout.flush()
        result = json.loads(output.getvalue().decode('ascii'))
    assert observer.faults > 0 and result['status'] == 'complete'
    assert result['results'][0]['directory'] == str(destination)
    assert service.get(result['id']).status == 'complete' and len(launches) == 1


def test_cli_progress_is_bounded_ascii_and_a_missing_stderr_never_pollutes_json(monkeypatch):
    import io
    import sys
    from carr_library.assistant import cli
    output, diagnostics = io.StringIO(), io.BytesIO()
    stderr = io.TextIOWrapper(diagnostics, encoding='cp1252', errors='strict', write_through=True, newline='\n')
    with monkeypatch.context() as patch:
        patch.setattr(sys, 'stdout', output)
        patch.setattr(sys, 'stderr', stderr)
        cli._message('Україна ' * 2000)
        stderr.flush()
        encoded = diagnostics.getvalue()
        assert len(encoded) <= 4097 and encoded.decode('ascii').endswith('...\n')
        patch.setattr(sys, 'stderr', None)
        cli._message('This is observation, not a JSON result.')
        assert output.getvalue() == ''

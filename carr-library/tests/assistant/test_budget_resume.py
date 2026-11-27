"""Approved limits cover the whole durable job, including complete backups."""
from contextlib import nullcontext
from hashlib import sha256
import os
from types import SimpleNamespace

import pytest

from carr_library.assistant import AssistantService, Plan, Scope, Step
from carr_library.assistant.backup_state import validate_state_bytes
from carr_library.assistant.models import canonical
from carr_library.storage.schema_identity import expected_schema_fingerprint


@pytest.fixture
def environment(tmp_path):
    data, output, inputs = (tmp_path / name for name in ('data', 'output', 'inputs'))
    for path in (data, output, inputs):
        path.mkdir()
    # Typed inventory for isolated budget checks; database bytes stay synthetic.
    database = b'Synthetic database' * 8
    manifest = (canonical({
        'version': 2, 'kind': 'carr-library', 'schema_version': 4,
        'schema_fingerprint': expected_schema_fingerprint(4),
        'database': {'filename': 'library.sqlite', 'size_bytes': len(database),
                     'sha256': sha256(database).hexdigest()},
        'assets': [],
    }) + '\n').encode('utf-8')
    payload = {'manifest.json': manifest, 'library.sqlite': database}

    def backup(destination, *, max_bytes, max_files, cancelled):
        if len(payload) > max_files:
            raise ValueError('Synthetic producer file budget exhausted.')
        if sum(map(len, payload.values())) > max_bytes:
            raise ValueError('Synthetic producer byte budget exhausted.')
        destination.mkdir()
        (destination / 'originals').mkdir()
        for name, raw in payload.items():
            (destination / name).write_bytes(raw)
        return sha256(payload['manifest.json']).hexdigest()
    app = SimpleNamespace(root=data, backup=backup, backup_bytes=sum(map(len, payload.values())),
                          read=lambda _workspace: nullcontext(), workspaces=lambda: ())
    return app, output, inputs


def produce_six_bytes(calls):
    def production(arguments, directory, cancelled, progress):
        if cancelled():
            raise InterruptedError('Synthetic renderer paused.')
        calls.append((arguments['max_files'], arguments['max_bytes']))
        if arguments['max_bytes'] < 6:
            raise ValueError('Synthetic renderer refuses to exceed its remaining budget.')
        path = directory / (arguments['operation_id'] + '.bin')
        path.write_bytes(b'123456')
        return {'artifacts': [str(path)]}
    return production


def test_one_step_cannot_return_more_artifacts_than_approved(environment):
    app, output, _ = environment
    def production(*_args):
        paths = [output / 'one.bin', output / 'two.bin']
        for path in paths:
            path.write_bytes(b'fixture')
        return {'artifacts': [str(path) for path in paths]}
    service = AssistantService(app, production=production)
    identity = service.submit(Plan(Scope(output_root=output, max_files=1),
                                   (Step('audio.produce', {'text': 'Fixture'}),)))
    result = service.run(identity)
    assert result.status == 'failed' and result.completed == 0 and 'file budget' in result.message


def test_hardlinked_audio_artifact_completes_and_detects_changed_cache_on_resume(environment):
    app, output, _ = environment
    cache = app.root / 'cached-chapter.wav'
    original = b'Synthetic chapter PCM bytes'
    cache.write_bytes(original)
    calls = []
    def production(*_args):
        calls.append(True)
        chapter = output / 'chapter.wav'
        os.link(cache, chapter)
        return {'artifacts': [str(chapter)]}
    service = AssistantService(app, production=production)
    identity = service.submit(Plan(Scope(output_root=output, max_files=1, max_bytes=len(original)),
                                   (Step('audio.produce', {'text': 'Fixture'}),)))
    result = service.run(identity)
    assert result.status == 'complete' and result.completed == 1
    assert result.results[0]['artifacts'][0]['sha256'] == sha256(original).hexdigest()
    assert service.resume(identity).status == 'complete' and len(calls) == 1
    cache.write_bytes(b'X' * len(original))
    changed = service.resume(identity)
    assert changed.status == 'failed' and 'completed artifact changed' in changed.message
    assert len(calls) == 1


def test_hardlinked_assistant_checkpoint_remains_rejected(environment):
    app, _output, _ = environment
    service = AssistantService(app)
    identity = service.submit(Plan(Scope(), (Step('library.list'),)))
    os.link(service.store.path(identity), app.root / 'checkpoint-alias.json')
    with pytest.raises(ValueError, match='private regular files'):
        service.get(identity)


@pytest.mark.parametrize('damage', ['missing_receipt', 'different_manifest'])
def test_completed_backup_requires_receipt_on_resume_and_pure_restore(environment, damage):
    app, output, _ = environment
    service = AssistantService(app)
    identity = service.submit(Plan(Scope(output_root=output, allow_full_backup=True),
                                   (Step('library.backup', {'name': 'backup'}),)))
    assert service.run(identity).status == 'complete'
    record = service.store.load(identity)
    result = record['steps'][0]['result']
    if damage == 'missing_receipt':
        del result['backup_receipt']
    else:
        result['manifest_sha256'] = '0' * 64
    service.store.save(record)
    (output / 'backup/library.sqlite').write_bytes(b'Altered database; manifest left unchanged')
    assert service.resume(identity).status == 'failed'
    with pytest.raises(ValueError):
        validate_state_bytes('state/assistant/' + identity + '.json', canonical(record).encode())


def test_resume_reconstructs_remaining_budget_without_repeating_completed_output(environment):
    app, output, _ = environment
    calls = []
    service = AssistantService(app, production=produce_six_bytes(calls))
    identity = service.submit(Plan(Scope(output_root=output, max_bytes=10, max_files=2),
        (Step('audio.produce', {'text': 'First'}), Step('audio.produce', {'text': 'Second'}))))
    def pause_second(index, _total, label):
        if index == 1 and label == 'audio.produce':
            service.pause(identity)
    paused = service.run(identity, progress=pause_second)
    assert paused.status == 'paused' and paused.completed == 1 and calls == [(2, 10)]
    result = service.resume(identity)
    assert result.status == 'failed' and result.completed == 1
    assert calls == [(2, 10), (1, 4)] and len(list(output.glob('*.bin'))) == 1


def test_whole_backup_bytes_are_charged_once_before_another_output(environment):
    app, output, _ = environment
    size = app.backup_bytes
    calls = []
    service = AssistantService(app, production=produce_six_bytes(calls))
    identity = service.submit(Plan(Scope(output_root=output, allow_full_backup=True, max_bytes=size + 5, max_files=3),
        (Step('library.backup', {'name': 'backup'}), Step('audio.produce', {'text': 'Next'}))))
    result = service.run(identity)
    assert result.status == 'failed' and result.completed == 1
    assert calls == [(1, 5)] and not list(output.glob('*.bin'))
    assert result.results[0]['backup_receipt']['total_bytes'] == size


def test_read_only_step_works_after_exact_file_and_byte_allowance_is_used(environment):
    app, output, _ = environment
    calls = []
    service = AssistantService(app, production=produce_six_bytes(calls))
    identity = service.submit(Plan(Scope(output_root=output, max_bytes=12, max_files=2),
        (Step('audio.produce', {'text': 'First'}), Step('audio.produce', {'text': 'Second'}), Step('library.list'))))
    assert service.run(identity).status == 'complete'
    assert calls == [(2, 12), (1, 6)]
    assert service.resume(identity).status == 'complete' and len(calls) == 2


def test_multiple_imports_cannot_reuse_original_byte_allowance(environment):
    app, output, inputs = environment
    sources = [inputs / 'one.txt', inputs / 'two.txt']
    for path in sources:
        path.write_bytes(b'123456')
    batches = {}
    def plan_batch(workspace, paths, **_kwargs):
        identity = str(len(batches))
        rows = [{'source': str(path), 'manifest': {'size': path.stat().st_size},
                 'state': 'complete', 'edition_id': str(index)} for index, path in enumerate(paths)]
        batches[identity] = {'batch_id': identity, 'rows': rows, 'state': 'complete', 'counts': {'complete': len(rows)}}
        return batches[identity]
    app.plan_import_batch = plan_batch
    app.import_batch = lambda _workspace, identity: batches[identity]
    app.run_import_batch = lambda _workspace, identity, **_kwargs: batches[identity]
    service = AssistantService(app)
    identity = service.submit(Plan(Scope('scoped', (inputs,), output, max_bytes=10, max_files=2),
        tuple(Step('library.import', {'path': str(path)}) for path in sources)))
    result = service.run(identity)
    assert result.status == 'failed' and result.completed == 1 and len(batches) == 1


def test_pure_restore_counts_full_backup_receipts_against_job_budget(environment):
    app, output, _ = environment
    service = AssistantService(app)
    identity = service.submit(Plan(Scope(output_root=output, allow_full_backup=True),
        (Step('library.backup', {'name': 'one'}), Step('library.backup', {'name': 'two'}))))
    assert service.run(identity).status == 'complete'
    record = service.store.load(identity)
    one_backup_size = record['steps'][0]['result']['backup_receipt']['total_bytes']
    record['plan']['scope']['max_bytes'] = one_backup_size + 1
    record['plan_sha256'] = sha256(canonical(record['plan']).encode()).hexdigest()
    with pytest.raises(ValueError, match='whole-job'):
        validate_state_bytes('state/assistant/' + identity + '.json', canonical(record).encode())

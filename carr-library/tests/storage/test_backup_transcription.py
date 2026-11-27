"""Typed ASR backup uses retained bytes and review flags, never an engine."""
from dataclasses import asdict
from hashlib import sha256
import json

import pytest

from carr_library.application import LibraryApp
from carr_library.audio import transcription as asr
from carr_library.durable_state import atomic_json, encode, read_json
from carr_library.library_backup import create_backup, restore_backup


def saved_job(app, status='running', *, receipt=False):
    data = b'Synthetic retained audio identity; no real model is used.'
    digest = sha256(data).hexdigest()
    item = dict(sha256=digest, bytes=len(data), file=digest + '.wav')
    request = dict(schema=1, runtime='b' * 64, settings=asdict(asr.TranscriptionSettings()),
                   formats=['txt'], inputs=[item])
    identity = sha256(encode(request)).hexdigest()
    directory = app.root / 'state/transcription' / identity
    (directory / 'inputs').mkdir(parents=True)
    (directory / 'inputs' / item['file']).write_bytes(data)
    record = dict(schema='resonance-transcription-job/1', id=identity, request=request,
        status=status, completed=[item['file']] if status == 'complete' else [], error=None,
        external_dependencies=dict(runtime_identity=request['runtime'], runtime_and_model_included=False,
                                   resume_requires_reviewed_local_installation=True))
    atomic_json(directory / 'job.json', record)
    atomic_json(directory / 'control.json', {'command': 'run'})
    if receipt or status == 'complete':
        outputs = directory / 'outputs' / item['file']
        outputs.mkdir(parents=True)
        text = 'Збережений результат.\n'.encode('utf-8')
        (outputs / 'transcript.txt').write_bytes(text)
        atomic_json(outputs / 'receipt.json', dict(schema=1, status='complete',
            identity=dict(source_sha256=digest, source_bytes=len(data), source_format='.wav',
                          runtime=request['runtime'], settings=request['settings'], formats=request['formats']),
            outputs={'txt': dict(sha256=sha256(text).hexdigest(), bytes=len(text))}))
    return directory, record


@pytest.mark.parametrize('status', ['queued', 'running', 'paused', 'cancelled', 'failed', 'complete'])
def test_full_asr_restore_preserves_bytes_and_requires_review_before_any_resume(tmp_path, monkeypatch, status):
    app = LibraryApp(tmp_path / 'source')
    app.create_workspace('Library')
    directory, record = saved_job(app, status, receipt=True)
    def no_engine(*args, **kwargs):
        raise AssertionError('Backup and restore must not invoke an ASR runtime.')
    monkeypatch.setattr(asr.SuppliedWhisperRuntime, 'verify', no_engine)
    monkeypatch.setattr(asr, 'run_process', no_engine)
    before = {path.relative_to(directory).as_posix(): path.read_bytes() for path in directory.rglob('*') if path.is_file()}
    folder = tmp_path / 'backup'
    digest = create_backup(app, folder)
    manifest = read_json(folder / 'manifest.json')
    assert manifest['version'] == 5 and manifest['extensions']['version'] == 2
    assert manifest['extensions']['transcription_jobs'] == ['transcription/' + record['id'] + '/job.json']
    assert manifest['extensions']['required_inputs'] == [dict(
        component='state/transcription/' + record['id'] + '/job.json', kind='local-asr-runtime',
        reference=record['request']['runtime'], included=False)]
    destination = restore_backup(folder, tmp_path / 'restored', digest)
    recovered_dir = destination / 'state/transcription' / record['id']
    recovered = read_json(recovered_dir / 'job.json')
    assert recovered['request'] == record['request']
    assert recovered['completed'] == record['completed']
    assert recovered['status'] == ('paused' if status in ('queued', 'running') else status)
    assert recovered['recovery_review_required'] is True
    assert read_json(recovered_dir / 'control.json') == {'command': 'pause'}
    for name, raw in before.items():
        if name not in ('job.json', 'control.json'):
            assert (recovered_dir / name).read_bytes() == raw
        assert (directory / name).read_bytes() == raw
    asr.validate_saved_snapshot(recovered_dir, recovered)


def test_workspace_scoped_backup_omits_independent_transcription_jobs(tmp_path):
    app = LibraryApp(tmp_path / 'source')
    workspace = app.create_workspace('Library')
    saved_job(app)
    folder = tmp_path / 'backup'
    digest = create_backup(app, folder, authorized_workspaces={workspace})
    value = read_json(folder / 'manifest.json')
    assert not value['state'] and not value['extensions'].get('transcription_jobs')
    destination = restore_backup(folder, tmp_path / 'restored', digest)
    assert not (destination / 'state/transcription').exists()


@pytest.mark.parametrize('damage', ['missing-input', 'changed-input', 'missing-receipt', 'unlisted-output', 'wrong-runtime'])
def test_asr_snapshot_rejects_unbound_or_missing_owned_bytes(tmp_path, damage):
    app = LibraryApp(tmp_path / 'source')
    directory, record = saved_job(app, 'complete')
    item = record['request']['inputs'][0]
    if damage == 'missing-input':
        (directory / 'inputs' / item['file']).unlink()
    elif damage == 'changed-input':
        (directory / 'inputs' / item['file']).write_bytes(b'changed source')
    elif damage == 'missing-receipt':
        (directory / 'outputs' / item['file'] / 'receipt.json').unlink()
    elif damage == 'unlisted-output':
        (directory / 'extra.json').write_text('{}')
    else:
        path = directory / 'outputs' / item['file'] / 'receipt.json'
        receipt = read_json(path)
        receipt['identity']['runtime'] = 'c' * 64
        atomic_json(path, receipt, replace=True)
    destination = tmp_path / 'rejected'
    with pytest.raises((ValueError, OSError)):
        create_backup(app, destination)
    assert not destination.exists()


def test_resealed_asr_input_change_is_rejected_before_restore_publication(tmp_path):
    app = LibraryApp(tmp_path / 'source')
    directory, record = saved_job(app)
    folder = tmp_path / 'backup'
    digest = create_backup(app, folder)
    manifest = read_json(folder / 'manifest.json')
    name = 'transcription/' + record['id'] + '/inputs/' + record['request']['inputs'][0]['file']
    changed = b'Forged retained audio identity.'
    (folder / 'state' / name).write_bytes(changed)
    next(item for item in manifest['state'] if item['filename'] == name).update(
        size_bytes=len(changed), sha256=sha256(changed).hexdigest())
    raw = json.dumps(manifest).encode()
    (folder / 'manifest.json').write_bytes(raw)
    destination = tmp_path / 'rejected'
    with pytest.raises(ValueError, match='input'):
        restore_backup(folder, destination, sha256(raw).hexdigest())
    assert not destination.exists()


def test_review_required_asr_state_can_be_rebacked_without_implicit_activation(tmp_path):
    app = LibraryApp(tmp_path / 'source')
    _, record = saved_job(app)
    first = tmp_path / 'backup'
    digest = create_backup(app, first)
    restored = LibraryApp(restore_backup(first, tmp_path / 'restored', digest))
    second = tmp_path / 'again'
    digest = create_backup(restored, second)
    final = restore_backup(second, tmp_path / 'final', digest)
    job = read_json(final / 'state/transcription' / record['id'] / 'job.json')
    assert job['status'] == 'paused' and job['recovery_review_required'] is True


@pytest.mark.parametrize('scoped', [False, True])
def test_runtime_declaration_is_disabled_or_omitted_without_manifest_probe(tmp_path, monkeypatch, scoped):
    from pathlib import Path
    app = LibraryApp(tmp_path / 'source')
    workspace = app.create_workspace('Library')
    external = str(tmp_path / 'private-runtime-manifest.json')
    settings = dict(version=1, enabled=True, manifest_path=external)
    atomic_json(app.root / 'transcription-runtime.json', settings)
    original_stat = Path.stat
    def forbidden(path, *args, **kwargs):
        assert str(path) != external, 'backup probed a separately authorized runtime manifest'
        return original_stat(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'stat', forbidden)
    folder = tmp_path / 'backup'
    digest = create_backup(app, folder, authorized_workspaces={workspace} if scoped else None)
    destination = restore_backup(folder, tmp_path / 'restored', digest)
    path = destination / 'transcription-runtime.json'
    if scoped:
        assert not path.exists()
    else:
        assert read_json(path) == {**settings, 'enabled': False}
    assert read_json(app.root / 'transcription-runtime.json') == settings

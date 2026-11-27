"""Actual synthetic worker -> typed backup -> removed originals -> restored reads.

The subprocesses implement only the supplied file protocols. They do not run a
model, decode recordings, use the network, or establish acoustic qualification.
"""
from hashlib import sha256
import json
from pathlib import Path
import runpy
import shutil
import sys

import pytest

from carr_library import backup_transcription, transcription
from carr_library.application import LibraryApp
from carr_library.durable_state import atomic_json, read_json
from carr_library.library_backup import restore_backup

V5 = transcription.V5_PROTOCOL
LEGACY = transcription.RECEIPTS_PROTOCOL


def _fixture_source(protocol):
    filename = ('test_v5_worker_profile.py' if protocol == V5
                else 'test_supplied_worker_bridge.py')
    fixture = runpy.run_path(str(Path(__file__).parents[1] / 'transcription' / filename))
    return (fixture['SYNTHETIC_V5_WORKER'] if protocol == V5
            else fixture['SYNTHETIC_WORKER'].replace('__MODE__', repr('complete')))


def _remove_fixture_instrumentation(service, identity):
    """These test-only controls/cache bytes are not a product data schema."""
    path = service._path(identity)
    for name in ('fixture-mode.json', 'fixture-runs.json'):
        (path / name).unlink(missing_ok=True)
    cache = path / 'logs' / 'cache_v5' / 'synthetic-retained.txt'
    if cache.exists():
        assert cache.read_text(encoding='utf-8') == 'Synthetic cache; no inference.'
        cache.unlink()


@pytest.fixture
def factory(tmp_path):
    def make(protocol=LEGACY, *, count=1, scenario='success', empty_text=False, word_timestamps=True, nested_outputs=False):
        app = LibraryApp(tmp_path / 'library')
        workspace = app.create_workspace('Typed retained transcription')
        service = app.transcription(workspace)
        runtime = tmp_path / 'runtime'
        runtime.mkdir()
        worker = runtime / 'worker.py'
        source = _fixture_source(protocol)
        if empty_text:
            assert protocol == LEGACY
            old = r"Synthetic transcript. Український текст.\n"
            assert old in source
            source = source.replace(old, '')
        if nested_outputs:
            assert protocol == V5
            assert "name=f'source_{index+1:03d}.'+suffix" in source
            source = source.replace("name=f'source_{index+1:03d}.'+suffix",
                                    "name=f'input/source_{index+1:03d}.'+suffix")
            assert "path=result/name\n" in source
            source = source.replace("path=result/name\n",
                                    "path=result/name\n        path.parent.mkdir(parents=True, exist_ok=True)\n")
        worker.write_text(source, encoding='utf-8')
        models = tmp_path / 'models'
        models.mkdir()
        if protocol == V5:
            for name in ('local_models.py', 'transcript_features.py'):
                (runtime / name).write_text('# Synthetic protocol fixture only.\n', encoding='utf-8')
            model = models / 'small'
            model.mkdir()
            for name in ('model.bin', 'config.json', 'tokenizer.json'):
                (model / name).write_bytes(('Synthetic model identity only: ' + name).encode())
        service.configure(Path(sys.executable).absolute(), worker, models, protocol=protocol)
        originals = []
        for index in range(count):
            original = tmp_path / ('original-' + str(index) + '.wav')
            original.write_bytes(('Synthetic retained source ' + str(index)).encode())
            originals.append(original)
        identity = service.submit(originals, protocol=protocol,
                                  options={'word_timestamps': word_timestamps})
        if protocol == V5 and scenario != 'success':
            atomic_json(service._path(identity) / 'fixture-mode.json', scenario)
        return app, workspace, service, identity, originals, runtime, models
    return make


def _complete(factory, protocol=LEGACY, **options):
    value = factory(protocol, **options)
    result = value[2].run(value[3], timeout=30)
    assert result['status'] == 'completed', result['last_error']
    _remove_fixture_instrumentation(value[2], value[3])
    return value, result


def _files(root):
    state = root / 'state' / 'transcription'
    return {path.relative_to(root).as_posix(): path.read_bytes()
            for path in state.rglob('*') if path.is_file()
            and not backup_transcription.ignored(path.relative_to(root).as_posix())}


def _resign(folder, value):
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n').encode()
    (folder / 'manifest.json').write_bytes(raw)
    return sha256(raw).hexdigest()


def _replace_member(folder, manifest, name, raw):
    (folder / name).write_bytes(raw)
    entry = next(row for row in manifest['sidecars'] if row['filename'] == name)
    entry.update(size_bytes=len(raw), sha256=sha256(raw).hexdigest())


@pytest.mark.parametrize('protocol,word_timestamps,nested_outputs', [
    (LEGACY, True, False), (V5, True, False), (V5, False, False), (V5, True, True)])
def test_actual_completed_roundtrip_keeps_receipts_edits_and_missing_engine_passive(
        factory, tmp_path, monkeypatch, protocol, word_timestamps, nested_outputs):
    value, result = _complete(factory, protocol, word_timestamps=word_timestamps,
                              nested_outputs=nested_outputs)
    app, workspace, service, identity, originals, runtime, models = value
    source = result['sources'][0]['filename']
    original_text = service.text(identity, source)
    revision = service.save_edit(identity, source, 'Separate human edit. Українська.')
    expected = _files(app.root)
    engine = expected['state/transcription/engine.json']
    for path in originals:
        path.unlink()
    shutil.rmtree(runtime)
    shutil.rmtree(models)

    def forbidden(*args, **kwargs):
        raise AssertionError('A passive backup or completed read tried to activate the engine.')
    for name in ('run_process', '_python_runtime', '_v5_model', '_v5_file'):
        monkeypatch.setattr(transcription, name, forbidden)
    destination = tmp_path / 'backup'
    checksum = app.backup(destination)
    manifest = read_json(destination / 'manifest.json')
    assert manifest['version'] == 4 and manifest['generated_audio_included'] is False
    retained = [row for row in manifest['sidecars'] if backup_transcription.is_media(row['filename'])]
    assert len(retained) == 1 and retained[0]['sha256'] == result['sources'][0]['sha256']
    assert all(not backup_transcription.ignored(row['filename']) for row in manifest['sidecars'])
    shutil.rmtree(app.root)
    restored = restore_backup(destination, tmp_path / 'restored', expected_manifest_sha256=checksum)
    assert _files(restored) == expected
    assert (restored / 'state' / 'transcription' / 'engine.json').read_bytes() == engine
    recovered = LibraryApp(restored).transcription(workspace)
    assert recovered.verify(identity)['status'] == 'completed'
    assert recovered.run(identity, timeout=30)['status'] == 'completed'
    assert recovered.text(identity, source) == original_text
    assert recovered.edits(identity)[0]['id'] == revision
    assert recovered.edits(identity)[0]['text'] == 'Separate human edit. Українська.'
    recovered.export(identity, tmp_path / 'exported')


def test_real_v5_stop_keeps_unaccepted_bound_outputs_without_restoring_run_authority(
        factory, tmp_path):
    value = factory(V5, count=2, scenario='stop-after-first')
    app, workspace, service, identity, originals, _, _ = value
    path = service._path(identity)
    def cancelled():
        status = path / 'job_status.json'
        return status.exists() and read_json(status).get('done') == 1
    stopped = service.run(identity, timeout=30, cancelled=cancelled)
    assert stopped['status'] == 'stopped' and stopped['done'] == 0
    assert stopped['receipts'] == [] and stopped['artifacts'] == []
    manifest_name = 'results/' + stopped['run_context']['result_directory'] + '/result_manifest.json'
    manifest = (path / manifest_name).read_bytes()
    _remove_fixture_instrumentation(service, identity)
    checksum = app.backup(tmp_path / 'stopped-backup')
    for original in originals:
        original.unlink()
    restored = restore_backup(tmp_path / 'stopped-backup', tmp_path / 'stopped-restored',
                              expected_manifest_sha256=checksum)
    recovered = LibraryApp(restored).transcription(workspace)
    retained = recovered.get(identity)
    assert retained['status'] == 'stopped' and retained['done'] == 0
    assert retained['receipts'] == [] and retained['artifacts'] == []
    assert (recovered._path(identity) / manifest_name).read_bytes() == manifest
    assert not (recovered._path(identity) / 'stop.flag').exists()
    assert not (recovered._path(identity) / '.transcribe.lock').exists()
    # Reading/listing does not execute. Only this explicit resume starts a new run.
    resumed = recovered.run(identity, timeout=30)
    assert resumed['status'] == 'completed' and resumed['done'] == 2, resumed['last_error']
    assert resumed['run_context']['host_run_id'] != stopped['run_context']['host_run_id']
    _remove_fixture_instrumentation(recovered, identity)
    # The previous unaccepted run remains typed history after current context
    # moves to this explicitly requested successful run.
    second_checksum = recovered.app.backup(tmp_path / 'after-resume')
    second_root = restore_backup(tmp_path / 'after-resume', tmp_path / 'after-resume-restored',
                                 expected_manifest_sha256=second_checksum)
    final = LibraryApp(second_root).transcription(workspace)
    assert final.verify(identity)['status'] == 'completed'
    assert (final._path(identity) / manifest_name).read_bytes() == manifest


@pytest.mark.parametrize('mutation', ['source', 'result', 'edit', 'unbound', 'unknown'])
def test_resigned_outer_manifest_cannot_override_typed_inner_authority(factory, tmp_path, mutation):
    value, result = _complete(factory)
    app, _, service, identity, _, _, _ = value
    service.save_edit(identity, result['sources'][0]['filename'], 'Retained human edit.')
    folder = tmp_path / 'backup'
    app.backup(folder)
    manifest = read_json(folder / 'manifest.json')
    prefix = service._path(identity).relative_to(app.root).as_posix() + '/'
    if mutation == 'source':
        name = prefix + 'input/' + result['sources'][0]['filename']
        _replace_member(folder, manifest, name, b'Changed retained original.')
    elif mutation == 'result':
        artifact = next(row for row in result['artifacts'] if row['format'] == 'txt')
        _replace_member(folder, manifest, prefix + 'results/' + artifact['path'], b'Changed recognized text.')
    elif mutation == 'edit':
        name = next(row['filename'] for row in manifest['sidecars'] if '/edits/' in row['filename'])
        edit = read_json(folder / name)
        edit['original_text_sha256'] = '0' * 64
        _replace_member(folder, manifest, name, json.dumps(edit).encode())
    else:
        name = prefix + ('results/unassigned.txt' if mutation == 'unbound' else 'worker-command.json')
        raw = b'Unassigned worker data.' if mutation == 'unbound' else b'{"run":"arbitrary"}'
        (folder / name).parent.mkdir(parents=True, exist_ok=True)
        (folder / name).write_bytes(raw)
        manifest['sidecars'].append(dict(filename=name, size_bytes=len(raw), sha256=sha256(raw).hexdigest()))
    checksum = _resign(folder, manifest)
    with pytest.raises((ValueError, OSError), match='source|original|result|edit|adapter|transcription|sidecar'):
        restore_backup(folder, tmp_path / 'rejected', expected_manifest_sha256=checksum)
    assert not (tmp_path / 'rejected').exists()


def test_current_v5_manifest_remains_bound_even_after_outer_manifest_is_resigned(factory, tmp_path):
    value, result = _complete(factory, V5)
    app, _, service, identity, _, _, _ = value
    folder = tmp_path / 'backup'
    app.backup(folder)
    manifest = read_json(folder / 'manifest.json')
    prefix = service._path(identity).relative_to(app.root).as_posix()
    name = prefix + '/results/' + result['run_context']['result_directory'] + '/result_manifest.json'
    original = read_json(folder / name)
    original['wrapper_sources_sha256']['local_models.py'] = '0' * 64
    _replace_member(folder, manifest, name, json.dumps(original).encode())
    checksum = _resign(folder, manifest)
    with pytest.raises((ValueError, OSError), match='manifest|Whisper|transcription'):
        restore_backup(folder, tmp_path / 'rejected', expected_manifest_sha256=checksum)
    assert not (tmp_path / 'rejected').exists()


def test_archived_scope_is_retained_but_missing_or_retargeted_scope_refuses(factory, tmp_path):
    value, result = _complete(factory)
    app, workspace, _, identity, _, _, _ = value
    app.archive_workspace(workspace)
    checksum = app.backup(tmp_path / 'archived')
    restored = restore_backup(tmp_path / 'archived', tmp_path / 'restored',
                              expected_manifest_sha256=checksum)
    recovered = LibraryApp(restored)
    assert next(row for row in recovered.workspaces(include_archived=True) if row.id == workspace).archived
    with pytest.raises(ValueError, match='archived'):
        recovered.transcription(workspace)
    recovered.restore_workspace(workspace)
    assert recovered.transcription(workspace).verify(identity)['done'] == 1

    folder = tmp_path / 'archived'
    manifest = read_json(folder / 'manifest.json')
    job_name = next(row['filename'] for row in manifest['sidecars']
                    if row['filename'].startswith(backup_transcription.PREFIX) and row['filename'].endswith('/job.json'))
    job = read_json(folder / job_name)
    job['workspace'] = 'missing-original-library'
    _replace_member(folder, manifest, job_name, json.dumps(job).encode())
    checksum = _resign(folder, manifest)
    with pytest.raises((ValueError, OSError), match='scope|library|transcription'):
        restore_backup(folder, tmp_path / 'wrong-scope', expected_manifest_sha256=checksum)


def test_empty_plain_transcript_is_retained_and_originals_use_separate_media_budget(
        factory, tmp_path, monkeypatch):
    value, result = _complete(factory, empty_text=True)
    app, workspace, service, identity, _, _, _ = value
    txt = next(row for row in result['artifacts'] if row['format'] == 'txt')
    assert txt['size_bytes'] == 0 and service.text(identity, txt['source']) == ''
    checksum = app.backup(tmp_path / 'empty-text')
    restored = restore_backup(tmp_path / 'empty-text', tmp_path / 'restored',
                              expected_manifest_sha256=checksum)
    assert LibraryApp(restored).transcription(workspace).text(identity, txt['source']) == ''
    monkeypatch.setattr(backup_transcription, 'MAX_MEDIA_BYTES', 1)
    with pytest.raises((ValueError, OSError), match='bound|budget|limit'):
        app.backup(tmp_path / 'over-budget')
    assert not (tmp_path / 'over-budget').exists()


def test_only_exact_known_transient_names_are_excluded():
    base = 'state/transcription/' + 'a' * 64 + '/jobs/' + 'b' * 32 + '/'
    assert backup_transcription.ignored('state/transcription/.engine.lock')
    assert backup_transcription.ignored(base + '.transcribe.lock')
    for name in ('stop.flag', 'pause.flag', 'settings_live.json'):
        assert backup_transcription.ignored(base + name)
        assert backup_transcription.limit(base + name) is None
    for name in ('logs/cache_v5/private.json', '.pending-unknown/data.json',
                 'new-worker.json', 'results/command.py', 'receipts/not-an-identity.json'):
        assert not backup_transcription.ignored(base + name)
        assert backup_transcription.limit(base + name) is None


def test_ready_job_needs_no_engine_and_collection_cancel_never_publishes(factory, tmp_path):
    app, workspace, service, identity, originals, runtime, models = factory()
    job = service.get(identity)
    assert job['status'] == 'ready' and job['run_context'] is None
    (service.root / 'engine.json').unlink()
    shutil.rmtree(runtime)
    shutil.rmtree(models)
    originals[0].unlink()
    checksum = app.backup(tmp_path / 'ready')
    restored = restore_backup(tmp_path / 'ready', tmp_path / 'restored-ready',
                              expected_manifest_sha256=checksum)
    recovered = LibraryApp(restored).transcription(workspace)
    assert recovered.get(identity) == job
    assert recovered.list()[0]['status'] == 'ready'
    assert not (recovered._path(identity) / 'results').exists()
    with pytest.raises(ValueError, match='Configure'):
        recovered.run(identity, timeout=30)

    manifest = read_json(tmp_path / 'ready' / 'manifest.json')
    calls = 0
    def cancelled():
        nonlocal calls
        calls += 1
        return calls >= 3
    with app.read() as db, pytest.raises(InterruptedError, match='cancelled'):
        backup_transcription.validate_collection(db, tmp_path / 'ready', manifest['sidecars'],
                                                  cancelled=cancelled)
    assert calls == 3
    assert service.get(identity) == job

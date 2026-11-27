"""Actual synthetic worker -> typed backup -> removed originals -> restored reads.

The subprocesses implement only the supplied file protocols. They do not run a
model, decode recordings, use the network, or establish acoustic qualification.
"""
from hashlib import sha256
import json
import os
from pathlib import Path
import runpy
import shutil
import venv

import pytest

from carr_library import backup_sidecars, backup_transcription, transcription
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
        # These real child peers use only the standard library. Select a real
        # isolated interpreter so each required inspection seals that runtime,
        # rather than repeatedly hashing unrelated application-site packages.
        # Operational-base/native-file and per-boundary verification stay real.
        environment = tmp_path / 'selected-python'
        venv.EnvBuilder(with_pip=False, system_site_packages=False,
                        symlinks=os.name != 'nt').create(environment)
        python = environment / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
        configured = service.configure(python, worker, models, protocol=protocol)
        inventory = configured['runtime_inventory']
        assert inventory['python_executable'] == str(python.absolute())
        assert inventory['python_configuration']
        assert all(row == {'present': False}
                   for row in inventory['runtime_distributions'].values())
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
    actual = _files(restored)
    assert set(actual) == set(expected)
    for name, raw in expected.items():
        if name == 'state/transcription/engine.json':
            assert json.loads(actual[name]) == {**json.loads(raw), 'execution_review_required': True}
        elif '/transcription/profiles/' in name:
            assert json.loads(actual[name]) == {**json.loads(raw), 'trusted': False, 'restored': True}
        elif name.endswith('/job.json'):
            assert json.loads(actual[name]) == {**json.loads(raw), 'execution_review_required': True}
        else:
            assert actual[name] == raw
    assert json.loads(actual['state/transcription/engine.json']) == {
        **json.loads(engine), 'execution_review_required': True}
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
    # Restored selectors remain passive until a new local review and rebind.
    with pytest.raises(ValueError, match='[Rr]eview'):
        recovered.run(identity, timeout=30)
    config = service.configuration()
    approved = recovered.configure(config['python'], config['worker'], config['models_dir'], protocol=V5)
    recovered.rebind_runtime(identity, approved['profile_id'])
    resumed = recovered.resume(identity, timeout=30)
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
    assert recovered.get(identity) == {**job, 'execution_review_required': True}
    assert recovered.list()[0]['status'] == 'ready'
    assert not (recovered._path(identity) / 'results').exists()
    assert not (recovered.root / 'engine.json').exists()
    with pytest.raises(transcription.RuntimeReviewError, match='Review and rebind'):
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


@pytest.mark.parametrize('cache_root', ['cache', 'cache_v5'])
@pytest.mark.parametrize('suffix', ['cache.json', 'receipt.json', 'json', 'txt'])
@pytest.mark.parametrize('layout', ['', '33/', 'verified-v1/', 'verified-v1/33/'])
@pytest.mark.parametrize('shape', ['v3-implicit', 'v3-state', 'v4-sidecars', 'v5-state'])
def test_manifest_requires_explicit_optional_cache_inclusion(
        cache_root, suffix, layout, shape):
    scope, job, source, run, key = 'a' * 64, 'b' * 32, 'c' * 32, 'd' * 32, 'e' * 64
    root = f'state/transcription/{scope}/jobs/{job}/'
    cache = root + f'logs/{cache_root}/{layout}{key}.{suffix}'
    result = root + f'results/runs/{run}/Whisper_V5/{job}/'
    retained = [
        root + f'input/001-{source}.wav',
        root + f'receipts/{key}.result.json',
        result + 'input/source.txt',
        result + 'result_manifest.json',
        root + f'edits/{source}.json',
        root + 'job_config.json',
    ]
    assert all(not backup_transcription.is_cache_file(name) for name in retained)
    assert backup_transcription.is_cache_file(cache)
    assert backup_transcription.limit(cache) is None  # Recognition is not admission.
    legacy = shape in ('v3-state', 'v5-state')
    field = 'state' if legacy else 'sidecars'
    entries = [dict(filename=name[6:] if legacy else name,
                    size_bytes=0 if name == cache and suffix == 'txt' else 1,
                    sha256='f' * 64) for name in [*retained, cache]]
    manifest = dict(version={'v3-implicit': 3, 'v3-state': 3,
                             'v4-sidecars': 4, 'v5-state': 5}[shape],
                    kind='carr-library', schema_version=5, schema_fingerprint='1' * 64,
                    database={}, assets=[])
    manifest[field] = entries
    extra = set()
    if shape == 'v5-state':
        manifest['pending'] = None
        manifest['reading_state'] = dict(filename='reading-state.json',
                                        size_bytes=1, sha256='f' * 64)
        extra.add('reading-state.json')
    if shape != 'v3-implicit':
        manifest['generated_audio_included'] = False
    with pytest.raises(ValueError, match='Transcription cache requires explicit manifest inclusion'):
        backup_sidecars.normalize_manifest(manifest)
    manifest[field] = entries[:-1]
    inventory = backup_sidecars.normalize_manifest(manifest)
    assert inventory.generated_audio_included is False
    assert {row['filename'] for row in inventory.entries} == set(retained) | extra
    if shape != 'v3-implicit':
        manifest[field] = entries
        manifest['generated_audio_included'] = True
        with pytest.raises(ValueError, match='Optional transcription cache inclusion is not supported'):
            backup_sidecars.normalize_manifest(manifest)


@pytest.mark.parametrize('tail', [
    'logs/cache-other/data.json', 'logs/cache_v5_extra/data.json',
    'worker/logs/cache/data.json', 'worker-history/' + 'c' * 32 + '-' + 'd' * 32 + '/logs/cache/data.json',
    'results/cache/data.json', 'receipts/cache.json', 'edits/cache.json',
])
def test_only_exact_flat_job_cache_roots_are_optional(tail):
    root = 'state/transcription/' + 'a' * 64 + '/jobs/' + 'b' * 32 + '/'
    assert not backup_transcription.is_cache_file(root + tail)
    assert not backup_transcription.is_cache_directory(root + tail)
    assert not backup_transcription.is_cache_file(
        'state/transcription/jobs/' + 'b' * 32 + '/logs/cache/data.json')
    assert not backup_transcription.is_cache_directory(
        'state/transcription/' + 'a' * 63 + '/jobs/' + 'b' * 32 + '/logs/cache')
    for cache in ('logs/cache', 'logs/cache_v5'):
        assert backup_transcription.is_cache_directory(root + cache)
        assert not backup_transcription.is_cache_directory(root + cache + '/child')


def test_actual_v5_cache_is_optional_but_all_receipts_and_manifests_restore(
        factory, tmp_path, monkeypatch):
    app, workspace, service, identity, originals, runtime, models = factory(V5)
    result = service.run(identity, timeout=30)
    assert result['status'] == 'completed', result['last_error']
    path = service._path(identity)
    cache = path / 'logs' / 'cache_v5'
    assert (cache / 'synthetic-retained.txt').read_text(encoding='utf-8') == 'Synthetic cache; no inference.'
    other_cache = path / 'logs' / 'cache'
    other_cache.mkdir()
    (other_cache / 'unparsed-private-cache.bin').write_bytes(b'Optional unparsed recognition cache.')
    for name in ('fixture-mode.json', 'fixture-runs.json'):
        (path / name).unlink(missing_ok=True)
    source = result['sources'][0]['filename']
    text = service.text(identity, source)
    revision = service.save_edit(identity, source, 'Separate retained edit.')
    expected = {name: raw for name, raw in _files(app.root).items()
                if not backup_transcription.is_cache_file(name)}
    real_open, real_descriptor_open = Path.open, os.open
    def forbid_cache_read(self, *args, **kwargs):
        if self.is_relative_to(cache) or self.is_relative_to(other_cache):
            raise AssertionError('An optional recognition-cache payload was opened.')
        return real_open(self, *args, **kwargs)
    def forbid_cache_descriptor(path, *args, **kwargs):
        candidate = Path(os.fsdecode(path))
        if candidate.is_relative_to(cache) or candidate.is_relative_to(other_cache):
            raise AssertionError('An optional recognition-cache descriptor was opened.')
        return real_descriptor_open(path, *args, **kwargs)
    # Guard both actual payload readers during backup, then release the guards
    # before shutil.rmtree may legitimately open directories for cleanup.
    with monkeypatch.context() as reads:
        reads.setattr(Path, 'open', forbid_cache_read)
        reads.setattr(os, 'open', forbid_cache_descriptor)
        with pytest.raises(ValueError, match='Optional transcription cache inclusion is not supported'):
            app.backup(tmp_path / 'unsupported-cache-inclusion', include_generated=True)
        assert not (tmp_path / 'unsupported-cache-inclusion').exists()

        backup = tmp_path / 'cache-omitted'
        checksum = app.backup(backup)
    manifest = read_json(backup / 'manifest.json')
    assert manifest['generated_audio_included'] is False
    names = {row['filename'] for row in manifest['sidecars']}
    assert set(expected) <= names
    assert all(not backup_transcription.is_cache_file(name) for name in names)
    relative = path.relative_to(app.root).as_posix() + '/'
    assert all(relative + name in names for name in result['receipts'])
    assert relative + 'results/' + result['run_context']['result_directory'] + '/result_manifest.json' in names
    assert all(relative + 'results/' + row['path'] in names for row in result['artifacts'])
    for original in originals:
        original.unlink()
    shutil.rmtree(runtime)
    shutil.rmtree(models)
    shutil.rmtree(app.root)
    def forbidden(*args, **kwargs):
        raise AssertionError('Restored retained data attempted execution or external runtime inspection.')
    for name in ('run_process', '_python_runtime', '_v5_model', '_v5_file'):
        monkeypatch.setattr(transcription, name, forbidden)
    restored = restore_backup(backup, tmp_path / 'cache-restored', expected_manifest_sha256=checksum)
    actual = _files(restored)
    assert set(actual) == set(expected)
    for name, raw in expected.items():
        if name == 'state/transcription/engine.json':
            assert json.loads(actual[name]) == {**json.loads(raw), 'execution_review_required': True}
        elif '/transcription/profiles/' in name:
            assert json.loads(actual[name]) == {**json.loads(raw), 'trusted': False, 'restored': True}
        elif name.endswith('/job.json'):
            assert json.loads(actual[name]) == {**json.loads(raw), 'execution_review_required': True}
        else:
            assert actual[name] == raw
    recovered = LibraryApp(restored).transcription(workspace)
    accepted = recovered.verify(identity)
    assert accepted['status'] == 'completed' and accepted['receipts'] == result['receipts']
    assert accepted['artifacts'] == result['artifacts']
    assert recovered.text(identity, source) == text
    assert recovered.edits(identity)[0]['id'] == revision
    assert recovered.run(identity, timeout=30)['status'] == 'completed'
    assert not (recovered._path(identity) / 'logs' / 'cache_v5').exists()
    assert not (recovered._path(identity) / 'logs' / 'cache').exists()


@pytest.mark.parametrize('cache_root', ['cache', 'cache_v5'])
@pytest.mark.parametrize('flag', [False, None, True])
def test_actual_restore_rejects_optional_cache_manifest_before_payload_reads(
        factory, tmp_path, monkeypatch, cache_root, flag):
    app, _, service, identity, _, _, _ = factory()
    backup = tmp_path / 'backup'
    app.backup(backup)
    manifest = read_json(backup / 'manifest.json')
    name = (service._path(identity).relative_to(app.root).as_posix()
            + f'/logs/{cache_root}/' + 'a' * 64 + '.cache.json')
    payload = backup / name
    payload.parent.mkdir(parents=True)
    raw = b'{"optional":"cache; never accepted as a transcript"}'
    payload.write_bytes(raw)
    manifest['sidecars'].append(dict(filename=name, size_bytes=len(raw), sha256=sha256(raw).hexdigest()))
    if flag is None:
        manifest['version'] = 3
        manifest.pop('generated_audio_included')
        manifest.pop('pending', None)
    else:
        manifest['generated_audio_included'] = flag
    checksum = _resign(backup, manifest)
    real_open, real_descriptor_open = Path.open, os.open
    def forbidden(self, *args, **kwargs):
        if self.is_relative_to(backup) and self != backup / 'manifest.json':
            raise AssertionError('Restore opened payload bytes before refusing the cache declaration.')
        return real_open(self, *args, **kwargs)
    def forbidden_descriptor(path, *args, **kwargs):
        candidate = Path(os.fsdecode(path))
        if candidate.is_relative_to(backup) and candidate != backup / 'manifest.json':
            raise AssertionError('Restore opened a payload descriptor before refusing the cache declaration.')
        return real_descriptor_open(path, *args, **kwargs)
    message = ('Optional transcription cache inclusion is not supported' if flag is True
               else 'Transcription cache requires explicit manifest inclusion')
    destination = tmp_path / 'never-published'
    with monkeypatch.context() as reads:
        reads.setattr(Path, 'open', forbidden)
        reads.setattr(os, 'open', forbidden_descriptor)
        with pytest.raises(ValueError, match=message):
            restore_backup(backup, destination, expected_manifest_sha256=checksum)
    assert not destination.exists()
    with real_open(payload, 'rb') as stream:
        assert stream.read() == raw


@pytest.mark.parametrize('cache_root', ['cache', 'cache_v5'])
@pytest.mark.parametrize('kind', ['regular', 'hardlink'])
def test_optional_cache_root_must_be_a_checked_native_directory(
        tmp_path, cache_root, kind):
    root = tmp_path / 'library'
    cache = (root / 'state' / 'transcription' / ('a' * 64)
             / 'jobs' / ('b' * 32) / 'logs' / cache_root)
    cache.parent.mkdir(parents=True)
    if kind == 'hardlink':
        source = tmp_path / 'linked-original'
        source.write_bytes(b'A cache root is not a linked file.')
        os.link(source, cache)
    else:
        cache.write_bytes(b'A cache root is not a regular file.')
    with pytest.raises(ValueError, match='regular|hard links|adapter'):
        backup_sidecars._capture(root, include_generated=False)
    assert cache.read_bytes().startswith(b'A cache root is not')

def _cancellation_manifest(tmp_path, shape):
    root = 'state/transcription/' + 'a' * 64 + '/jobs/' + '1' * 32 + '/'
    entries = [dict(filename=root + 'input/001-' + '2' * 32 + '.wav', size_bytes=1, sha256='3' * 64),
               dict(filename=root + 'results/input/source.txt', size_bytes=1, sha256='4' * 64)]
    version = {'v3-implicit': 3, 'v3-state': 3, 'v4-state': 4, 'v4-sidecars': 4, 'v5-state': 5}[shape]
    manifest = dict(version=version, kind='carr-library', schema_version=5, schema_fingerprint='5' * 64,
                    database={}, assets=[])
    if shape.endswith('-state'):
        manifest['state'] = [{**entry, 'filename': entry['filename'][6:]} for entry in entries]
    else:
        manifest['sidecars'] = entries
    if shape != 'v3-implicit':
        manifest['generated_audio_included'] = False
    if shape == 'v4-state':
        manifest['pending'] = dict(version=1, data_dir=str(tmp_path), files=[], assets=[], sources=[])
    if shape == 'v5-state':
        manifest['pending'] = None
        manifest['reading_state'] = dict(filename='reading-state.json', size_bytes=1, sha256='6' * 64)
    return manifest


@pytest.mark.parametrize('shape', ('v3-implicit', 'v3-state', 'v4-state', 'v4-sidecars', 'v5-state'))
@pytest.mark.parametrize('stop_after', (1, 2))
def test_manifest_cancelled_during_sidecar_validation(tmp_path, monkeypatch, shape, stop_after):
    manifest = _cancellation_manifest(tmp_path, shape)
    original = backup_sidecars.sidecar_limit
    assert len(backup_sidecars.normalize_manifest(manifest).entries) == (3 if shape == 'v5-state' else 2)
    calls = 0

    def counted(name):
        nonlocal calls
        result = original(name)
        calls += 1
        return result

    monkeypatch.setattr(backup_sidecars, 'sidecar_limit', counted)
    with pytest.raises(InterruptedError, match='cancelled'):
        backup_sidecars.normalize_manifest(manifest, cancelled=lambda: calls >= stop_after)
    assert calls == stop_after


@pytest.mark.parametrize('shape', ('v3-state', 'v4-state', 'v5-state'))
def test_manifest_cancelled_during_legacy_state_copy(tmp_path, monkeypatch, shape):
    manifest = _cancellation_manifest(tmp_path, shape)
    original = backup_sidecars.portable_name
    stopped = False

    def observed(name):
        nonlocal stopped
        result = original(name)
        stopped = True
        return result

    def forbidden(name):
        raise AssertionError('cancelled legacy copy reached sidecar validation')

    monkeypatch.setattr(backup_sidecars, 'portable_name', observed)
    monkeypatch.setattr(backup_sidecars, 'sidecar_limit', forbidden)
    with pytest.raises(InterruptedError, match='cancelled'):
        backup_sidecars.normalize_manifest(manifest, cancelled=lambda: stopped)


def test_manifest_propagates_cancellation_into_pending_validator(tmp_path, monkeypatch):
    from carr_library import pending_backup
    manifest = _cancellation_manifest(tmp_path, 'v4-state')
    original = pending_backup._source
    stopped = False

    def observed(value):
        nonlocal stopped
        result = original(value)
        stopped = True
        return result

    monkeypatch.setattr(pending_backup, '_source', observed)
    with pytest.raises(InterruptedError, match='cancelled'):
        backup_sidecars.normalize_manifest(manifest, cancelled=lambda: stopped)
    assert stopped


@pytest.mark.parametrize('cancel_at', range(1, 5))
def test_manifest_cancelled_even_with_empty_inventory(cancel_at):
    manifest = dict(version=4, kind='carr-library', schema_version=5, schema_fingerprint='5' * 64,
                    database={}, assets=[], sidecars=[], generated_audio_included=False)
    calls = 0

    def cancelled():
        nonlocal calls
        calls += 1
        return calls >= cancel_at

    with pytest.raises(InterruptedError, match='cancelled'):
        backup_sidecars.normalize_manifest(manifest, cancelled=cancelled)

@pytest.mark.parametrize('shape', ('v3-state', 'v4-state', 'v5-state'))
@pytest.mark.parametrize('excessive', (False, True))
def test_legacy_manifest_checks_depth_after_restoring_state_prefix(tmp_path, shape, excessive):
    manifest = _cancellation_manifest(tmp_path, shape)
    job, run = '1' * 32, '2' * 32
    root = 'state/transcription/' + 'a' * 64 + '/jobs/' + job + '/'
    name = root + 'results/runs/' + run + '/Whisper_V5/' + job + '/'
    name += ('extra/' if excessive else '') + 'input/source.txt'
    assert len(name.split('/')) == (13 if excessive else 12)
    manifest['state'] = [dict(filename=name[6:], size_bytes=1, sha256='3' * 64)]
    if excessive:
        with pytest.raises(ValueError, match='portable sidecar filename'):
            backup_sidecars.normalize_manifest(manifest)
    else:
        inventory = backup_sidecars.normalize_manifest(manifest)
        assert name in {entry['filename'] for entry in inventory.entries}
        assert inventory.generated_audio_included is False

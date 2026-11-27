"""Accepted ASR and shared policy use the one typed private-state inventory."""
from hashlib import sha256
import json
from pathlib import Path
import runpy
from types import SimpleNamespace

import pytest

from carr_library.application import LibraryApp
from carr_library.durable_state import state_lock
from carr_library.library_backup import create_backup, restore_backup
from carr_library.operations.broker import ResourceBroker
from carr_library.operations.models import ResourceLimits


@pytest.fixture
def retained(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Retained ASR scope')
    source = tmp_path / 'selected.wav'
    # Intake retains bytes without claiming they were decoded or recognized.
    source.write_bytes(b'Synthetic retained input; no recognition requested.')
    service = app.transcription(workspace)
    identity = service.submit([source], protocol='receipts-v1')
    policy = ResourceLimits(cpu_slots=1, memory_bytes=2 * 1024**3,
                            disk_bytes=4 * 1024**3, min_free_disk_bytes=256 * 1024**2)
    ResourceBroker.for_app(app.root, limits=policy)
    return app, workspace, service, identity, source, policy


def _resign(folder, relative, raw):
    path = folder / relative
    path.write_bytes(raw)
    manifest = json.loads((folder / 'manifest.json').read_bytes())
    row = next(row for row in manifest['sidecars'] if row['filename'] == relative)
    row.update(size_bytes=len(raw), sha256=sha256(raw).hexdigest())
    encoded = (json.dumps(manifest, sort_keys=True, separators=(',', ':')) + '\n').encode()
    (folder / 'manifest.json').write_bytes(encoded)
    return sha256(encoded).hexdigest()


def test_ready_asr_and_resource_policy_restore_without_runtime_or_live_leases(retained, tmp_path):
    app, workspace, service, identity, source, policy = retained
    original = service.get(identity)
    job_relative = service._path(identity).relative_to(app.root)
    source.unlink()
    folder = tmp_path / 'backup'
    digest = create_backup(app, folder)
    manifest = json.loads((folder / 'manifest.json').read_bytes())
    names = {row['filename'] for row in manifest['sidecars']}
    assert 'state/operations/resources.json' in names
    assert (job_relative / 'job.json').as_posix() in names
    assert not any(name.startswith('runtime/') for name in names)
    target = restore_backup(folder, tmp_path / 'restored', digest)
    restored = LibraryApp(target)
    recovered = restored.transcription(workspace)
    assert recovered.get(identity) == original
    assert (target / job_relative / 'input' / original['sources'][0]['filename']).read_bytes() == \
        b'Synthetic retained input; no recognition requested.'
    assert not (target / 'runtime').exists()
    # Loading policy after restoration creates fresh empty runtime coordination.
    assert ResourceBroker.for_app(target).limits == policy
    assert tuple((target / 'runtime/resources/leases').iterdir()) == ()
    assert not (target / 'state/transcription/engine.json').exists()
    other = restored.create_workspace('Unrelated restored library')
    assert restored.transcription(other).list() == ()


@pytest.mark.parametrize('damage', ['workspace', 'source-digest', 'retained-bytes'])
def test_resealed_asr_state_requires_its_workspace_and_retained_original(retained, tmp_path, damage):
    app, workspace, service, identity, source, policy = retained
    other = app.create_workspace('Different valid library')
    folder = tmp_path / 'backup'
    create_backup(app, folder)
    relative = (service._path(identity).relative_to(app.root) / 'job.json').as_posix()
    job = json.loads((folder / relative).read_bytes())
    if damage == 'workspace':
        job['workspace'] = other
    elif damage == 'source-digest':
        job['sources'][0]['sha256'] = '0' * 64
    if damage == 'retained-bytes':
        relative = (service._path(identity).relative_to(app.root) / 'input' /
                    job['sources'][0]['filename']).as_posix()
        raw = (folder / relative).read_bytes()
        raw = b'X' + raw[1:]
    else:
        raw = json.dumps(job).encode()
    digest = _resign(folder, relative, raw)
    target = tmp_path / 'rejected'
    with pytest.raises(ValueError):
        restore_backup(folder, target, digest)
    assert not target.exists()


def test_backup_obeys_existing_asr_lock_before_reading_mutable_status(retained, tmp_path, monkeypatch):
    from carr_library import backup_sidecars
    app, workspace, service, identity, source, policy = retained
    directory = service._path(identity)
    status = directory / 'job_status.json'
    status.write_bytes(b'Unfinished mutable status; deliberately not JSON.')
    opened = []
    read = backup_sidecars.read_regular

    def observed(path, *args, **kwargs):
        if path.name == 'job_status.json':
            opened.append(path)
        return read(path, *args, **kwargs)

    monkeypatch.setattr(backup_sidecars, 'read_regular', observed)
    with state_lock(directory, 'transcribe'):
        with pytest.raises(RuntimeError):
            create_backup(app, tmp_path / 'busy')
    assert not opened and not (tmp_path / 'busy').exists()
    with pytest.raises(ValueError):
        create_backup(app, tmp_path / 'malformed-after-release')
    assert opened and not (tmp_path / 'malformed-after-release').exists()


@pytest.fixture(params=('receipts-v1', 'whisper-v5-manifest-v1'))
def assistant_retained(request, tmp_path):
    # Reuse the actual accepted runner fixtures: synthetic children, exact
    # configuration/model identities, real receipts and the one ASR job store.
    helpers = runpy.run_path(str(Path(__file__).parents[1] /
                                'assistant' / 'test_library_transcription.py'))
    prepared = helpers['prepared'].__wrapped__(SimpleNamespace(param=request.param), tmp_path)
    if request.param == 'whisper-v5-manifest-v1':
        # Test-only run counting belongs in disposable runtime state. The
        # retained cache uses the reviewed worker's exact hashed filename shape.
        native, worker, models = prepared[2], prepared[7], prepared[8]
        configuration = native.configuration()
        source = worker.read_text(encoding='utf-8')
        replacements = {
            "history_path = job / 'fixture-runs.json'":
                "history_path = job / 'runtime-home' / 'fixture-runs.json'",
            "cache = job / 'logs' / 'cache_v5'":
                "cache = job / 'logs' / 'cache_v5' / 'aa'",
            "'synthetic-retained.txt'": "('a' * 64 + '.txt')",
        }
        for original, replacement in replacements.items():
            assert source.count(original) == 1
            source = source.replace(original, replacement)
        cache_marker = "(cache / ('a' * 64 + '.txt')).write_text('Synthetic cache; no inference.', encoding='utf-8')"
        assert source.count(cache_marker) == 1
        source = source.replace(cache_marker, cache_marker + "\n" +
            "(cache / ('a' * 64 + '.json')).write_text(json.dumps({'complete': False, "
            "'text': 'Unverified synthetic cache checkpoint.'}), encoding='utf-8')\n" +
            "(job / 'logs' / 'progress.log').write_text('Synthetic progress only.\\n', encoding='utf-8')\n" +
            "(job / 'logs' / 'failed.log').write_text('', encoding='utf-8')")
        worker.write_text(source, encoding='utf-8')
        native.configure(configuration['python'], worker, models, protocol=request.param)
    return prepared


@pytest.mark.parametrize('state', ('paused', 'complete'))
def test_shared_assistant_asr_backup_keeps_child_but_not_relocated_plan_authority(
        assistant_retained, tmp_path, monkeypatch, state):
    from carr_library import transcription
    from carr_library.assistant import AssistantService

    app, workspace, native, service, source, outputs, plan, worker, models = assistant_retained
    original_source = source.read_bytes()
    identity = service.submit(plan)

    def stopped_after_actual_child():
        # Metadata existence is permitted on Windows; never open live status.
        return state == 'paused' and native.jobs_root.exists() and any(
            (directory / 'job_status.json').exists() for directory in native.jobs_root.iterdir()
            if not directory.name.startswith('.'))

    result = service.run(identity, cancelled=stopped_after_actual_child)
    assert result.status == state, result
    saved = service.store.load(identity)
    intent = saved['steps'][0]['intent']
    child_id = intent['transcription_job']
    child = native.get(child_id)
    assert child['status'] == ('stopped' if state == 'paused' else 'completed')
    assert len(native.list()) == 1
    cached = {path.relative_to(native._path(child_id)): path.read_bytes()
              for path in (native._path(child_id) / 'logs').rglob('*') if path.is_file()}
    if child['protocol'] == 'whisper-v5-manifest-v1':
        assert len(cached) == 4
        assert json.loads(cached[Path('logs/cache_v5/aa') / ('a' * 64 + '.json')])['complete'] is False
    if state == 'complete':
        assert native.verify(child_id)['done'] == 1
        assert intent['transcription_export']
        if child['protocol'] == 'whisper-v5-manifest-v1':
            assert [row['attempt'] for row in child['effective_runs']] == ['original']
    else:
        assert 'transcription_export' not in intent
        assert child['receipts'] == []  # Stopped cache is not accepted output.
    destination = outputs / 'Approved transcript'
    exported = ({path.relative_to(outputs): path.read_bytes()
                 for path in destination.rglob('*') if path.is_file()}
                if destination.exists() else {})
    source.unlink()

    def forbidden(*args, **kwargs):
        pytest.fail('Passive backup or relocated assistant plan started another ASR child.')

    monkeypatch.setattr(transcription, 'run_process', forbidden)
    folder = tmp_path / 'backup'
    digest = create_backup(app, folder)
    target = restore_backup(folder, tmp_path / 'restored-assistant', digest)
    restored_app = LibraryApp(target)
    restored = AssistantService(restored_app)
    assert restored.store.load(identity) == saved
    recovered = restored_app.transcription(workspace)
    assert recovered.get(child_id) == child
    assert {relative: (recovered._path(child_id) / relative).read_bytes()
            for relative in cached} == cached
    retained_source = recovered._path(child_id) / 'input' / child['sources'][0]['filename']
    assert retained_source.read_bytes() == original_source
    if state == 'complete':
        assert recovered.verify(child_id)['receipts'] == child['receipts']
    # The plan's original data root/output scope is not rewritten by restore.
    with pytest.raises(PermissionError, match='another data directory'):
        restored.resume(identity)
    assert restored.store.load(identity) == saved
    assert len(recovered.list()) == 1
    assert recovered.get(child_id) == child
    assert not (target / 'Approved transcript').exists()
    assert ({path.relative_to(outputs): path.read_bytes()
             for path in destination.rglob('*') if path.is_file()}
            if destination.exists() else {}) == exported

    if state == 'complete' and child['protocol'] == 'whisper-v5-manifest-v1':
        # Preserve the real worker's named attempt, and reject a resealed
        # checkpoint with a number, boolean or unregistered label.
        relative = (native._path(child_id).relative_to(app.root) / 'job.json').as_posix()
        raw = (folder / relative).read_bytes()
        for index, invalid in enumerate((1, True, 'unregistered')):
            damaged = json.loads(raw)
            damaged['effective_runs'][0]['attempt'] = invalid
            checksum = _resign(folder, relative, json.dumps(damaged).encode())
            rejected = tmp_path / ('invalid-attempt-' + str(index))
            with pytest.raises(ValueError, match='attempt label'):
                restore_backup(folder, rejected, checksum)
            assert not rejected.exists()
        assert native.get(child_id) == child
        assert recovered.get(child_id) == child


def test_receipt_protocol_cannot_claim_v5_attempt_labels(retained, tmp_path):
    app, _workspace, service, identity, _source, _policy = retained
    path = service._path(identity) / 'job.json'
    job = json.loads(path.read_bytes())
    job['effective_runs'] = [{'source': job['sources'][0]['filename'],
                              'attempt': 'original', 'settings': job['settings']}]
    path.write_bytes(json.dumps(job).encode())
    target = tmp_path / 'wrong-protocol-attempt'
    with pytest.raises(ValueError, match='attempt label'):
        create_backup(app, target)
    assert not target.exists()
    assert json.loads(path.read_bytes()) == job


@pytest.mark.parametrize('relative', [
    'logs/cache_v5/bb/' + 'a' * 64 + '.json',
    'logs/cache_v5/aa/unbound.json', 'logs/unregistered.log', 'fixture-runs.json',
])
def test_transcription_cache_registration_does_not_admit_arbitrary_job_files(retained, tmp_path, relative):
    app, workspace, service, identity, source, policy = retained
    path = service._path(identity) / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"complete": false}\n', encoding='utf-8')
    folder = tmp_path / 'unregistered-cache'
    with pytest.raises(ValueError, match='backup adapter'):
        create_backup(app, folder)
    assert not folder.exists()

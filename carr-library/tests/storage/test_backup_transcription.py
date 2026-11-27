"""Accepted ASR and shared policy use the one typed private-state inventory."""
from hashlib import sha256
import json

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
    folder = tmp_path / 'backup'
    create_backup(app, folder)
    relative = (service._path(identity).relative_to(app.root) / 'job.json').as_posix()
    job = json.loads((folder / relative).read_bytes())
    if damage == 'workspace':
        job['workspace'] = app.create_workspace('Different valid library')
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

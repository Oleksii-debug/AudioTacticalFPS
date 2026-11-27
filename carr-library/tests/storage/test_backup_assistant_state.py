"""Real library/assistant snapshots preserve intent, scope and source history."""
from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant import AssistantService, Plan, Scope, Step
from carr_library.assistant.models import canonical
from carr_library.assistant.state import atomic_json
from carr_library import library_backup
from carr_library.ingest.writer_guard import WriterBusy, exclusive_writer
from carr_library.library_backup import create_backup, restore_backup


@pytest.fixture
def saved_job(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Джерела — точне читання')
    book = app.add_text(workspace, 'Source', 'Залізниця — точний оригінал.\r\nSecond line.')
    inputs, output = tmp_path / 'inputs', tmp_path / 'output'
    inputs.mkdir()
    output.mkdir()
    service = AssistantService(app)
    plan = Plan(Scope(workspace, (inputs,), output), (
        Step('library.search', {'query': 'Залізниця'}),
        Step('library.extract', {'edition': book['edition'], 'name': 'reading.txt'}),
    ))
    identity = service.submit(plan)
    assert service.run(identity).status == 'complete'
    service.store.control(identity, 'pause')
    return app, workspace, book, service, identity, inputs, output


def reseal(folder, relative, raw):
    path = folder / 'sidecars' / relative
    path.write_bytes(raw)
    manifest_path = folder / 'manifest.json'
    manifest = json.loads(manifest_path.read_bytes())
    entry = next(row for row in manifest['sidecars'] if row['path'] == relative)
    entry.update(size_bytes=len(raw), sha256=sha256(raw).hexdigest())
    manifest_raw = canonical(manifest).encode()
    manifest_path.write_bytes(manifest_raw)
    return sha256(manifest_raw).hexdigest()


def test_active_checkpoint_roundtrip_preserves_bytes_and_requires_new_scope(saved_job, tmp_path, monkeypatch):
    app, workspace, book, service, identity, inputs, output = saved_job
    record = service.store.load(identity)
    record['status'] = 'running'
    service.store.save(record)
    expected = {path.name: path.read_bytes() for path in service.store.directory.glob('*.json')}
    temporary = service.store.directory / ('.' + identity + '.json.' + 'a' * 32 + '.tmp')
    temporary.write_text('uncommitted bytes', encoding='utf-8')
    # An active execution lock is deliberately compatible with its own durable
    # checkpoint being copied. Only checkpoint publications use the root guard.
    with service.store.lock(identity):
        folder = tmp_path / 'backup'
        digest = create_backup(app, folder)
    manifest = json.loads((folder / 'manifest.json').read_bytes())
    assert manifest['version'] == 4
    assert {row['path'] for row in manifest['sidecars']} == {
        'state/assistant/' + name for name in expected}
    assert {path.name: path.read_bytes() for path in (folder / 'sidecars/state/assistant').iterdir()} == expected
    root = restore_backup(folder, tmp_path / 'restored', digest)
    restored = LibraryApp(root)
    resumed = AssistantService(restored)
    shutil.rmtree(inputs)
    shutil.rmtree(output)
    # A relocated job must remain inspectable even if its original device is gone.
    with monkeypatch.context() as guarded:
        guarded.setattr('carr_library.assistant.models.local_path',
                        lambda *a, **k: pytest.fail('Read a restored job\'s old path.'))
        assert resumed.get(identity).status == 'attention'
        assert resumed.jobs()[0].id == identity
        with pytest.raises(PermissionError, match='review and submit'):
            resumed.resume(identity)
    assert {path.name: path.read_bytes() for path in resumed.store.directory.glob('*.json')} == expected
    assert resumed.store.load(identity)['plan'] == record['plan']
    assert 'scope' not in json.loads(resumed.review_instruction(identity))

    # A fresh explicit preview uses the newly selected directory. Nothing in the
    # old record is rewritten or treated as permission for the new data root.
    new_output = tmp_path / 'new-output'
    new_output.mkdir()
    candidate = resumed.preview(resumed.review_instruction(identity), Scope(workspace, output_root=new_output))
    new_identity = resumed.submit(candidate)
    assert new_identity != identity and resumed.run(new_identity).status == 'complete'
    assert (new_output / 'reading.txt').read_bytes().startswith('Залізниця'.encode())
    assert resumed.store.path(identity).read_bytes() == expected[identity + '.json']


def test_assistant_backup_includes_its_running_checkpoint_without_self_deadlock(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    output = tmp_path / 'output'
    output.mkdir()
    service = AssistantService(app)
    scope = Scope(output_root=output, allow_full_backup=True, max_bytes=4 * 1024**2)
    identity = service.submit(service.preview('Backup "snapshot"', scope))
    report = service.run(identity)
    assert report.status == 'complete', report
    saved = json.loads((output / 'snapshot/sidecars/state/assistant' / (identity + '.json')).read_bytes())
    assert saved['status'] == saved['steps'][0]['status'] == 'running'
    assert saved['steps'][0]['result'] is None
    assert service.resume(identity).status == 'complete'


def test_snapshot_and_checkpoint_writers_use_the_same_guard(saved_job, tmp_path, monkeypatch):
    app, _, _, service, identity, _, _ = saved_job
    path = service.store.path(identity)
    before = path.read_bytes()
    control = path.with_suffix('.control.json')
    before_control = control.read_bytes()
    original = library_backup._copy_sidecar
    observed = []

    def copy(root, relative, target, metadata, cancelled):
        if relative.endswith(identity + '.json'):
            value = service.store.load(identity)
            timestamp = value['updated']
            with pytest.raises(WriterBusy):
                service.store.save(value)
            with pytest.raises(WriterBusy):
                service.store.control(identity, 'run')
            assert value['updated'] == timestamp
            observed.append(True)
        return original(root, relative, target, metadata, cancelled)

    monkeypatch.setattr(library_backup, '_copy_sidecar', copy)
    create_backup(app, tmp_path / 'backup')
    assert observed and path.read_bytes() == before and control.read_bytes() == before_control
    service.store.control(identity, 'run')
    assert service.store.control(identity) == 'run'


@pytest.mark.parametrize('damage', ['json', 'duplicate', 'plan_hash', 'scope', 'source', 'artifact', 'intent', 'control'])
@pytest.mark.parametrize('phase', ['backup', 'restore'])
def test_corrupt_or_rebound_checkpoint_is_never_published(saved_job, tmp_path, damage, phase):
    app, _, _, service, identity, _, output = saved_job
    relative = 'state/assistant/' + identity + ('.control.json' if damage == 'control' else '.json')
    folder = tmp_path / 'backup'
    if phase == 'restore':
        create_backup(app, folder)
        path = folder / 'sidecars' / relative
    else:
        path = app.root / relative
    value = json.loads(path.read_bytes())
    if damage == 'json':
        raw = b'{ broken'
    elif damage == 'duplicate':
        raw = (canonical(value)[:-1] + ',"status":"complete"}').encode()
    else:
        if damage == 'plan_hash':
            value['plan_sha256'] = '0' * 64
        elif damage == 'scope':
            value['plan']['scope']['workspace_id'] = value['workspace'] = 'not-a-library'
            value['plan_sha256'] = sha256(canonical(value['plan']).encode()).hexdigest()
        elif damage == 'source':
            value['steps'][0]['result']['results'][0]['revision'] = 'unknown-sealed-revision'
        elif damage == 'artifact':
            value['steps'][1]['result']['artifacts'][0]['path'] = str(output.parent / 'outside.txt')
        elif damage == 'intent':
            value['steps'][1]['intent']['shell'] = 'forbidden'
        elif damage == 'control':
            value['command'] = 'execute-anything'
        raw = canonical(value).encode()
    destination = tmp_path / 'rejected'
    if phase == 'restore':
        digest = reseal(folder, relative, raw)
        operation = lambda: restore_backup(folder, destination, digest)
    else:
        path.write_bytes(raw)
        operation = lambda: create_backup(app, destination)
    with pytest.raises((ValueError, OSError)):
        operation()
    assert not destination.exists()


@pytest.mark.parametrize('damage', ['orphan', 'unknown', 'directory', 'linked-job', 'linked-temp', 'hardlink'])
def test_unknown_or_linked_namespace_entries_fail_before_publication(saved_job, tmp_path, damage):
    app, _, _, service, identity, _, _ = saved_job
    directory = service.store.directory
    if damage == 'orphan':
        (directory / ('0' * 32 + '.control.json')).write_text('{"command":"pause"}')
    elif damage == 'unknown':
        (directory / 'providers.json').write_text('{"synthetic-secret":"exclude"}')
    elif damage == 'directory':
        (directory / ('0' * 32 + '.json')).mkdir()
    elif damage == 'hardlink':
        os.link(service.store.path(identity), tmp_path / 'external-alias')
    else:
        name = '0' * 32 + '.json' if damage == 'linked-job' else '.' + identity + '.json.' + '0' * 32 + '.tmp'
        try:
            (directory / name).symlink_to(service.store.path(identity))
        except OSError:
            pytest.skip('Host cannot create symbolic links.')
    with pytest.raises((ValueError, OSError)):
        create_backup(app, tmp_path / 'rejected')
    assert not (tmp_path / 'rejected').exists()


def test_shared_sidecar_budgets_and_assistant_specific_budget(saved_job, tmp_path, monkeypatch):
    app, _, _, service, identity, _, _ = saved_job
    with pytest.raises(ValueError, match='budget'):
        create_backup(app, tmp_path / 'small-count', max_sidecar_files=1)
    with pytest.raises(ValueError, match='budget'):
        create_backup(app, tmp_path / 'small-bytes', max_sidecar_bytes=16)
    import carr_library.assistant.snapshots as snapshots
    monkeypatch.setattr(snapshots, 'MAX_SNAPSHOT_BYTES', 16)
    with pytest.raises(ValueError, match='budget'):
        create_backup(app, tmp_path / 'assistant-budget')
    assert all(not (tmp_path / name).exists() for name in ('small-count', 'small-bytes', 'assistant-budget'))


def test_archived_and_historical_source_results_are_kept(saved_job, tmp_path):
    app, workspace, book, service, identity, _, _ = saved_job
    before = service.store.path(identity).read_bytes()
    saved_revision = service.store.load(identity)['steps'][1]['result']['source_revision']
    assert app.update_metadata(workspace, book['edition'], title='New metadata') != saved_revision
    app.archive_workspace(workspace)
    folder = tmp_path / 'backup'
    digest = create_backup(app, folder)
    root = restore_backup(folder, tmp_path / 'restored', digest)
    assert (root / 'state/assistant' / (identity + '.json')).read_bytes() == before


def test_unresolved_failed_request_is_preserved_without_becoming_a_source_claim(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('References')
    output = tmp_path / 'output'
    output.mkdir()
    service = AssistantService(app)
    identity = service.submit(Plan(Scope(workspace, output_root=output), (
        Step('library.extract', {'edition': 'unresolved-request', 'name': 'reading.txt'}),)))
    assert service.run(identity).status in ('failed', 'attention')
    folder = tmp_path / 'backup'
    digest = create_backup(app, folder)
    root = restore_backup(folder, tmp_path / 'restored', digest)
    restored = AssistantService(LibraryApp(root))
    assert restored.get(identity).status == 'attention'
    assert restored.store.load(identity)['plan']['steps'][0]['arguments']['edition'] == 'unresolved-request'

"""Real full/state restore of inactive declarations; no target or engine access."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from carr_library.application import LibraryApp
from carr_library import incremental_backup, local_schedules, state_backup
from carr_library import backup_local_schedules as passive
from carr_library.durable_state import encode
from carr_library.library_backup import restore_backup, verify_backup


def saved(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Scheduled backup scope')
    app.add_text(workspace, 'Original', 'A retained backup original.')
    watch = tmp_path / 'external-watch'
    watch.mkdir()
    repository = tmp_path / 'external-vault'
    incremental_backup.initialize_repository(repository)
    store = local_schedules.ScheduleStore(app)
    when = datetime.now(timezone.utc) + timedelta(minutes=5)
    first = store.create('watch', 'Explicit watched folder', watch,
                         first_due=when, workspace=workspace)
    second = store.create('backup', 'Explicit backup folder', repository,
                          first_due=when)
    return app, workspace, watch, repository, store, (first, second)


def deny_live(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Backup validation must not execute or arm a local schedule.')
    monkeypatch.setattr(local_schedules.ScheduleSession, 'arm', forbidden)
    monkeypatch.setattr(local_schedules.ScheduleSession, 'tick', forbidden)
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    monkeypatch.setattr(incremental_backup, 'create_snapshot', forbidden)


@pytest.mark.parametrize('same_path', [False, True])
def test_actual_full_restore_preserves_bytes_but_never_arms_or_opens_old_targets(tmp_path, monkeypatch, same_path):
    app, workspace, watch, repository, store, views = saved(tmp_path)
    originals = {view.id: (store.directory / (view.id + '.json')).read_bytes() for view in views}
    backup = tmp_path / 'full'
    digest = app.backup(backup)
    oldroot = app.root
    shutil.rmtree(oldroot)
    shutil.rmtree(watch)
    shutil.rmtree(repository)
    target = oldroot if same_path else tmp_path / 'restored'
    with monkeypatch.context() as patch:
        deny_live(patch)
        verify_backup(backup, digest)
        restore_backup(backup, target, expected_manifest_sha256=digest)
    restored = LibraryApp(target)
    assert len(restored.browse(workspace)) == 1
    reopened = local_schedules.ScheduleStore(restored)
    assert {view.id for view in reopened.list()} == set(originals)
    for identity, raw in originals.items():
        assert (reopened.directory / (identity + '.json')).read_bytes() == raw
    session = local_schedules.ScheduleSession(reopened)
    assert session.armed == () and session.tick() is None
    for view in reopened.list():
        with pytest.raises((OSError, ValueError)):
            session.arm(view.id, expected=view.sha256)
    assert not watch.exists() and not repository.exists()


def test_actual_state_only_restore_retains_inert_policies_with_deleted_external_folders(tmp_path, monkeypatch):
    app, workspace, watch, repository, store, views = saved(tmp_path)
    source = tmp_path / 'state-copy'
    entries = state_backup.snapshot_state(app.root / 'state', source)
    shutil.rmtree(app.root)
    shutil.rmtree(watch)
    shutil.rmtree(repository)
    target = tmp_path / 'restored-state'
    with monkeypatch.context() as patch:
        deny_live(patch)
        state_backup.restore_state(source, target, entries)
    for view in views:
        raw = (target / 'local-schedules' / (view.id + '.json')).read_bytes()
        assert json.loads(raw) == view.record
    assert not repository.exists() and not watch.exists()


@pytest.mark.parametrize('mutation', [
    'unknown-field', 'enabled-authority', 'bad-id', 'external-url',
    'invalid-config', 'forged-complete', 'bool-count', 'duplicate-key',
])
def test_resigned_invalid_schedule_state_refuses_before_target_creation(tmp_path, mutation):
    app, _, _, _, store, views = saved(tmp_path)
    source = tmp_path / 'state-copy'
    entries = state_backup.snapshot_state(app.root / 'state', source)
    relative = 'local-schedules/' + views[0].id + '.json'
    path = source / relative
    value = json.loads(path.read_bytes())
    if mutation == 'unknown-field':
        value['execute'] = 'shell'
    elif mutation == 'enabled-authority':
        value['armed'] = True
    elif mutation == 'bad-id':
        value['id'] = 'f' * 32 if value['id'] != 'f' * 32 else 'e' * 32
    elif mutation == 'external-url':
        value['target'] = 'https://example.invalid/private'
    elif mutation == 'invalid-config':
        value['config']['timeout'] = True
    elif mutation == 'forged-complete':
        value['sequence'] = 1
        value['last'] = dict(sequence=1, status='complete', started=value['created'],
            finished=value['created'], batch_id=None, result=None, message='')
    elif mutation == 'bool-count':
        value['sequence'] = True
    raw = encode(value)
    if mutation == 'duplicate-key':
        raw = raw[:-2] + b',"disabled":false}\n'
    path.write_bytes(raw)
    changed = deepcopy(entries)
    entry = next(row for row in changed if row['filename'] == relative)
    entry.update(size_bytes=len(raw), sha256=sha256(raw).hexdigest())
    target = tmp_path / 'refused'
    with pytest.raises((ValueError, OSError)):
        state_backup.restore_state(source, target, changed)
    assert not target.exists()


@pytest.mark.parametrize('name', [
    'local-schedules/UNKNOWN.json',
    'LOCAL-SCHEDULES/' + 'a' * 32 + '.json',
    'local-schedules/nested/' + 'a' * 32 + '.json',
    'local-schedules/.pending-abcdefgh',
    'local-schedules/.catalog.lock',
])
def test_incoming_reserved_namespace_rejects_aliases_temporaries_and_unlisted_shapes(tmp_path, name):
    app, _, _, _, store, views = saved(tmp_path)
    source = tmp_path / 'copied'
    path = source / name
    path.parent.mkdir(parents=True)
    raw = encode(views[0].record)
    path.write_bytes(raw)
    entries = [dict(filename=name, size_bytes=len(raw), sha256=sha256(raw).hexdigest())]
    target = tmp_path / 'refused'
    with pytest.raises(ValueError):
        state_backup.restore_state(source, target, entries)
    assert not target.exists()


def test_snapshot_omits_only_its_exact_transient_lock_not_unknown_lock_named_state(tmp_path):
    app, _, _, _, store, views = saved(tmp_path)
    (store.directory / '.catalog.lock').write_bytes(b'')
    first = tmp_path / 'good'
    entries = state_backup.snapshot_state(app.root / 'state', first)
    assert all(not row['filename'].endswith('.catalog.lock') for row in entries)
    (store.directory / 'foreign.lock').write_bytes(b'unknown user state')
    with pytest.raises(ValueError):
        state_backup.snapshot_state(app.root / 'state', tmp_path / 'bad')


def test_actual_32_rule_collection_bound_refuses_a_33rd_copied_policy(tmp_path):
    app, _, _, _, store, views = saved(tmp_path)
    copied = tmp_path / 'copied-state'
    directory = copied / 'local-schedules'
    directory.mkdir(parents=True)
    entries = []
    for number in range(33):
        identity = f'{number:032x}'
        value = deepcopy(views[0].record)
        value['id'] = identity
        raw = encode(value)
        relative = 'local-schedules/' + identity + '.json'
        (copied / relative).write_bytes(raw)
        entries.append(dict(filename=relative, size_bytes=len(raw), sha256=sha256(raw).hexdigest()))
    passive.validate_collection(copied, entries[:32])
    target = tmp_path / 'refused'
    with pytest.raises(ValueError, match='allowance'):
        state_backup.restore_state(copied, target, entries)
    assert not target.exists()


def test_actual_copied_byte_tampering_is_not_trusted_from_a_recorded_digest(tmp_path):
    app, _, _, _, store, views = saved(tmp_path)
    copied = tmp_path / 'copied'
    entries = state_backup.snapshot_state(app.root / 'state', copied)
    relative = 'local-schedules/' + views[0].id + '.json'
    path = copied / relative
    value = json.loads(path.read_bytes())
    value['title'] = 'Same valid shape, changed bytes'
    path.write_bytes(encode(value))
    with pytest.raises(ValueError, match='bytes'):
        passive.validate_collection(copied, entries)
    with pytest.raises(ValueError):
        state_backup.restore_state(copied, tmp_path / 'refused', entries)


def test_copied_schedule_cancellation_never_reads_external_target(tmp_path, monkeypatch):
    app, _, watch, repository, store, views = saved(tmp_path)
    copied = tmp_path / 'copied'
    entries = state_backup.snapshot_state(app.root / 'state', copied)
    monkeypatch.setattr(local_schedules, 'directory_identity',
                        lambda *args: pytest.fail('Passive validation resolved a live schedule target.'))
    with pytest.raises(InterruptedError):
        passive.validate_collection(copied, entries, cancelled=lambda: True)
    passive.validate_collection(copied, entries)


def test_common_backup_validator_requires_copied_workspace_but_accepts_archived_scope(tmp_path):
    app, workspace, _, _, store, views = saved(tmp_path)
    name = 'state/local-schedules/' + views[0].id + '.json'
    app.archive_workspace(workspace)
    with app.read() as db:
        passive.validate_record(name, views[0].record, db=db)
        invalid = deepcopy(views[0].record)
        invalid['workspace'] = 'missing-saved-workspace'
        with pytest.raises(ValueError, match='copied library'):
            passive.validate_record(name, invalid, db=db)


def test_exact_64k_policy_boundary_and_one_extra_byte_are_enforced_on_copied_bytes(tmp_path):
    _, _, _, _, _, views = saved(tmp_path)
    state = tmp_path / 'copied-state'
    path = state / 'local-schedules' / (views[0].id + '.json')
    path.parent.mkdir(parents=True)
    raw = encode(views[0].record)
    padded = raw + b' ' * (passive.MAX_BYTES - len(raw))
    path.write_bytes(padded)
    entry = dict(filename=path.relative_to(state).as_posix(), size_bytes=len(padded),
                 sha256=sha256(padded).hexdigest())
    passive.validate_collection(state, [entry])
    path.write_bytes(padded + b' ')
    expanded = {**entry, 'size_bytes': len(padded) + 1, 'sha256': sha256(padded + b' ').hexdigest()}
    with pytest.raises(ValueError):
        passive.validate_collection(state, [expanded])


def test_actual_hardlinked_copied_policy_is_rejected_before_restore(tmp_path):
    import os
    app, _, _, _, _, _ = saved(tmp_path)
    copied = tmp_path / 'copied'
    entries = state_backup.snapshot_state(app.root / 'state', copied)
    entry = next(row for row in entries if row['filename'].startswith('local-schedules/'))
    os.link(copied / entry['filename'], tmp_path / 'alias.json')
    target = tmp_path / 'refused'
    with pytest.raises((ValueError, OSError)):
        state_backup.restore_state(copied, target, entries)
    assert not target.exists()

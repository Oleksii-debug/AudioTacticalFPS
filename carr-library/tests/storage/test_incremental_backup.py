"""Actual canonical snapshots, immutable reuse, hostile input and commit boundaries."""
from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

import pytest

from carr_library.application import LibraryApp
from carr_library import incremental_backup as incremental
from carr_library.sync import vault as transport
from carr_library.library_backup import verify_backup
from carr_library.progress_preferences import ProgressPreferences, ProgressPreferenceStore


def _library(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Інкрементальна бібліотека')
    app.add_text(workspace, 'First original', 'Railway original retained unchanged.')
    repository = tmp_path / 'repository'
    incremental.initialize_repository(repository)
    return app, workspace, repository


@pytest.fixture
def saved(tmp_path):
    app, workspace, repository = _library(tmp_path)
    result = incremental.create_snapshot(app, repository)
    return app, workspace, repository, result


def _descriptor(repository, identity):
    return json.loads((repository / 'snapshots' / (identity + '.json')).read_bytes())


def _object(repository, digest):
    return repository / 'chunks' / digest[:2] / digest[2:]


def _put(repository, raw):
    digest = sha256(raw).hexdigest()
    target = _object(repository, digest)
    target.parent.mkdir(exist_ok=True)
    if not target.exists():
        with target.open('xb') as stream:
            stream.write(raw)
    return digest


def _seal(repository, value, *, committed=False):
    raw = transport._canonical(value)
    digest = sha256(raw).hexdigest()
    (repository / 'snapshots' / (digest + '.json')).write_bytes(raw)
    return digest


def _restore(repository, identity, target):
    result = incremental.restore_snapshot(repository, identity, target)
    assert result['restored'] is True and not result['staging_cleanup_pending']
    assert Path(result['path']) == target
    return LibraryApp(target)


def test_explicit_initialization_refuses_existing_and_missing_repositories(tmp_path):
    existing = tmp_path / 'existing'
    existing.mkdir()
    (existing / 'keep.txt').write_bytes(b'keep')
    with pytest.raises(FileExistsError):
        incremental.initialize_repository(existing)
    assert (existing / 'keep.txt').read_bytes() == b'keep'
    with pytest.raises((OSError, ValueError)):
        incremental.list_snapshots(tmp_path / 'missing')
    assert not (tmp_path / 'missing').exists()
    app = LibraryApp(tmp_path / 'library')
    with pytest.raises(ValueError, match='outside'):
        incremental.initialize_repository(app.root / 'nested-repository')
    assert not (app.root / 'nested-repository').exists()


def test_unchanged_payloads_are_reused_without_rewriting_or_hardlinking(saved):
    app, _, repository, first = saved
    rows = _descriptor(repository, first['snapshot_sha256'])['files']
    original = next(row for row in rows if row['path'].startswith('originals/'))
    path = _object(repository, original['sha256'])
    before = path.stat()
    content = path.read_bytes()
    second = incremental.create_snapshot(app, repository, parent=first['snapshot_sha256'])
    after = path.stat()
    assert second['published'] and second['durability_confirmed']
    assert second['reused_chunks'] >= 1 and second['reused_bytes'] >= len(content)
    assert second['snapshot_sha256'] != first['snapshot_sha256']
    assert (before.st_dev, before.st_ino, before.st_mtime_ns) == (
        after.st_dev, after.st_ino, after.st_mtime_ns)
    assert after.st_nlink == 1 and path.read_bytes() == content
    assert all(p.stat().st_nlink == 1 for p in (repository / 'chunks').glob('*/*'))
    assert incremental.verify_snapshot(repository, first['snapshot_sha256'])['payloads_verified']
    assert incremental.verify_snapshot(repository, second['snapshot_sha256'])['payloads_verified']


def test_changed_and_deleted_sidecars_restore_three_independent_actual_revisions(tmp_path):
    app, workspace, repository = _library(tmp_path)
    settings = app.root / 'progress-preferences.json'
    ProgressPreferenceStore(settings).save(ProgressPreferences(mode='beep', volume=23))
    first = incremental.create_snapshot(app, repository)
    ProgressPreferenceStore(settings).save(ProgressPreferences(mode='beep', volume=47))
    app.add_text(workspace, 'Second original', 'Railway later addition.')
    second = incremental.create_snapshot(app, repository, parent=first['snapshot_sha256'])
    settings.unlink()
    third = incremental.create_snapshot(app, repository, parent=second['snapshot_sha256'])
    assert second['new_chunks'] > 0 and second['reused_chunks'] > 0
    shutil.rmtree(app.root)
    a = _restore(repository, first['snapshot_sha256'], tmp_path / 'restored-first')
    b = _restore(repository, second['snapshot_sha256'], tmp_path / 'restored-second')
    c = _restore(repository, third['snapshot_sha256'], tmp_path / 'restored-third')
    assert len(a.search(workspace, 'Railway')) == 1
    assert len(b.search(workspace, 'Railway')) == len(c.search(workspace, 'Railway')) == 2
    assert ProgressPreferenceStore(a.root / settings.name).load().volume == 23
    assert ProgressPreferenceStore(b.root / settings.name).load().volume == 47
    assert not (c.root / settings.name).exists()
    assert len(incremental.list_snapshots(repository)) == 3


def test_child_restores_without_parent_replay_but_new_missing_parent_is_refused(saved, tmp_path):
    app, workspace, repository, first = saved
    app.add_text(workspace, 'Second', 'Railway second revision.')
    second = incremental.create_snapshot(app, repository, parent=first['snapshot_sha256'])
    (repository / 'snapshots' / (first['snapshot_sha256'] + '.json')).unlink()
    before = set((repository / 'snapshots').iterdir())
    with pytest.raises((OSError, ValueError)):
        incremental.create_snapshot(app, repository, parent=first['snapshot_sha256'])
    assert set((repository / 'snapshots').iterdir()) == before
    restored = _restore(repository, second['snapshot_sha256'], tmp_path / 'restored')
    assert len(restored.search(workspace, 'Railway')) == 2


@pytest.mark.parametrize('damage', ['missing', 'same_size_corruption'])
def test_corrupt_reusable_payload_is_never_repaired_or_published(saved, tmp_path, damage):
    app, _, repository, first = saved
    row = next(row for row in _descriptor(repository, first['snapshot_sha256'])['files']
               if row['path'].startswith('originals/'))
    path = _object(repository, row['sha256'])
    if damage == 'missing':
        path.unlink()
    else:
        path.write_bytes(b'X' * row['size'])
    before = set((repository / 'snapshots').iterdir())
    with pytest.raises((OSError, ValueError)):
        incremental.create_snapshot(app, repository, parent=first['snapshot_sha256'])
    with pytest.raises((OSError, ValueError)):
        incremental.restore_snapshot(repository, first['snapshot_sha256'], tmp_path / 'restored')
    assert not (tmp_path / 'restored').exists()
    assert set((repository / 'snapshots').iterdir()) == before
    assert (not path.exists()) if damage == 'missing' else path.read_bytes() == b'X' * row['size']


def test_listing_is_not_payload_verification_and_unmarked_descriptors_are_not_snapshots(saved):
    _, _, repository, first = saved
    value = deepcopy(_descriptor(repository, first['snapshot_sha256']))
    value['created_at'] = '2000-01-01T00:00:00.000001Z'
    orphan = _seal(repository, value, committed=False)
    entries = incremental.list_snapshots(repository)
    assert len(entries) == 1 and entries[0]['snapshot_sha256'] == first['snapshot_sha256']
    assert entries[0]['payloads_verified'] is False
    # A separately reviewed orphan token may recover its complete bytes, but
    # this committed-history listing never promotes it to a saved generation.
    assert incremental.verify_snapshot(repository, orphan)['payloads_verified']
    original = next(row for row in value['files'] if row['path'].startswith('originals/'))
    _object(repository, original['sha256']).write_bytes(b'X' * original['size'])
    assert len(incremental.list_snapshots(repository)) == 1
    with pytest.raises(ValueError):
        incremental.verify_snapshot(repository, first['snapshot_sha256'])


@pytest.mark.parametrize('attack', ['traversal', 'backslash', 'drive', 'case_alias',
                                    'unicode_alias', 'ancestor_file', 'local_origin'])
def test_resealed_descriptor_cannot_introduce_hostile_paths(saved, tmp_path, attack):
    _, _, repository, first = saved
    value = deepcopy(_descriptor(repository, first['snapshot_sha256']))
    row = deepcopy(value['files'][0])
    if attack == 'case_alias':
        value['directories'].append('ORIGINALS')
    elif attack == 'unicode_alias':
        value['directories'] += ['é', 'e\u0301']
    else:
        row['path'] = {'traversal': '../escaped', 'backslash': 'originals\\escaped',
                           'drive': 'C:/escaped', 'ancestor_file': 'originals',
                           'local_origin': 'state/studio/.LOCAL-ORIGIN.JSON'}[attack]
        value['files'].append(row)
        if attack == 'local_origin':
            value['directories'] += ['state', 'state/studio']
    value['files'].sort(key=lambda item: item['path'])
    value['directories'].sort()
    value['total_bytes'] = sum(item['size'] for item in value['files'])
    forged = _seal(repository, value)
    with pytest.raises((OSError, ValueError)):
        incremental.restore_snapshot(repository, forged, tmp_path / 'restored')
    assert not (tmp_path / 'restored').exists()
    assert not (tmp_path / 'escaped').exists()


def test_full_canonical_namespace_and_database_validation_are_authoritative(saved, tmp_path):
    _, _, repository, first = saved
    value = deepcopy(_descriptor(repository, first['snapshot_sha256']))
    row = next(row for row in value['files'] if row['path'] == 'library.sqlite')
    bogus = b'Fully hashed bytes are not a valid SQLite library.'
    digest = _put(repository, bogus)
    row.update(size=len(bogus), sha256=digest, chunks=[digest])
    manifest_row = next(row for row in value['files'] if row['path'] == 'manifest.json')
    manifest = json.loads(_object(repository, manifest_row['sha256']).read_bytes())
    manifest['database'] = dict(filename='library.sqlite', size_bytes=len(bogus), sha256=digest)
    raw = transport._canonical(manifest)
    digest = _put(repository, raw)
    manifest_row.update(size=len(raw), sha256=digest, chunks=[digest])
    value['total_bytes'] = sum(item['size'] for item in value['files'])
    forged = _seal(repository, value)
    with pytest.raises((ValueError, sqlite3.DatabaseError)):
        incremental.restore_snapshot(repository, forged, tmp_path / 'restored')
    assert not (tmp_path / 'restored').exists()
    valid = incremental.verify_snapshot(repository, first['snapshot_sha256'])
    assert valid['payloads_verified']


def test_extra_hashed_payload_is_not_a_permitted_canonical_sidecar(saved, tmp_path):
    _, _, repository, first = saved
    value = deepcopy(_descriptor(repository, first['snapshot_sha256']))
    raw = b'{}'
    digest = _put(repository, raw)
    value['files'].append({'path': 'state/unknown/data.json',
                          'sha256': digest, 'size': len(raw), 'chunks': [digest]})
    value['total_bytes'] += len(raw)
    value['files'].sort(key=lambda item: item['path'])
    value['directories'] = sorted(set(value['directories']) | {'state', 'state/unknown'})
    forged = _seal(repository, value)
    with pytest.raises(ValueError):
        incremental.materialize_snapshot(repository, forged, tmp_path / 'full')
    assert not (tmp_path / 'full').exists()


@pytest.mark.parametrize('kind', ['object_hardlink', 'object_symlink', 'root_symlink', 'manifest_hardlink'])
def test_hostile_links_are_rejected_without_writing_destination(saved, tmp_path, kind):
    _, _, repository, first = saved
    selected = repository
    if kind in ('object_hardlink', 'object_symlink'):
        row = next(row for row in _descriptor(repository, first['snapshot_sha256'])['files']
                   if row['path'].startswith('originals/'))
        path = _object(repository, row['sha256'])
        outside = tmp_path / 'outside-original'
        outside.write_bytes(path.read_bytes())
        if kind == 'object_hardlink':
            outside.unlink()
            os.link(path, outside)
        else:
            path.unlink()
            path.symlink_to(outside)
    elif kind == 'root_symlink':
        selected = tmp_path / 'repository-link'
        selected.symlink_to(repository, target_is_directory=True)
    else:
        os.link(repository / 'snapshots' / (first['snapshot_sha256'] + '.json'),
                tmp_path / 'manifest-alias')
    with pytest.raises((OSError, ValueError)):
        incremental.restore_snapshot(selected, first['snapshot_sha256'], tmp_path / 'restored')
    assert not (tmp_path / 'restored').exists()


def test_complete_materialization_is_independent_single_link_and_no_clobber(saved, tmp_path):
    _, workspace, repository, first = saved
    full = tmp_path / 'ordinary-full'
    result = incremental.materialize_snapshot(repository, first['snapshot_sha256'], full)
    assert result['published'] and result['durability_confirmed']
    assert verify_backup(full, first['backup_manifest_sha256'])['version'] == 4
    assert sha256((full / 'manifest.json').read_bytes()).hexdigest() == first['backup_manifest_sha256']
    assert all(p.stat().st_nlink == 1 for p in full.rglob('*') if p.is_file())
    before = (full / 'manifest.json').read_bytes()
    with pytest.raises(FileExistsError):
        incremental.materialize_snapshot(repository, first['snapshot_sha256'], full)
    assert (full / 'manifest.json').read_bytes() == before
    restored = _restore(repository, first['snapshot_sha256'], tmp_path / 'restored')
    with pytest.raises(FileExistsError):
        incremental.restore_snapshot(repository, first['snapshot_sha256'], restored.root)
    assert restored.search(workspace, 'Railway')[0].quote == 'Railway'
    with pytest.raises(ValueError):
        incremental.materialize_snapshot(repository, first['snapshot_sha256'], repository / 'nested')


def test_cancellation_before_commit_keeps_previous_snapshot_and_no_completed_successor(saved, monkeypatch):
    app, workspace, repository, first = saved
    app.add_text(workspace, 'Second', 'Railway changed content.')
    store = transport.Vault._put_chunk
    stopped = False
    def stop_after_payload(self, *args, **kwargs):
        nonlocal stopped
        digest, reused = store(self, *args, **kwargs)
        if not reused:
            stopped = True
        return digest, reused
    monkeypatch.setattr(transport.Vault, '_put_chunk', stop_after_payload)
    with pytest.raises(InterruptedError):
        incremental.create_snapshot(app, repository, parent=first['snapshot_sha256'],
                                    cancelled=lambda: stopped)
    assert [row['snapshot_sha256'] for row in incremental.list_snapshots(repository)] == [
        first['snapshot_sha256']]
    assert incremental.verify_snapshot(repository, first['snapshot_sha256'])['payloads_verified']

def test_late_cancel_after_commit_keeps_published_identity(saved, monkeypatch):
    app, _, repository, first = saved
    stopped = False
    sync = transport._sync_directory
    def cancel_at_final_sync(path):
        nonlocal stopped
        if Path(path) == repository and transport.read_head_for_test(repository) != first['snapshot_sha256']:
            stopped = True
        return sync(path)
    # Read the existing checked shape without altering the publication mechanism.
    monkeypatch.setattr(transport, 'read_head_for_test',
        lambda root: json.loads((root / 'head.json').read_bytes())['snapshot'], raising=False)
    monkeypatch.setattr(transport, '_sync_directory', cancel_at_final_sync)
    second = incremental.create_snapshot(app, repository, parent=first['snapshot_sha256'],
                                         cancelled=lambda: stopped)
    assert stopped and second['published'] and second['durability_confirmed']
    assert incremental.verify_snapshot(repository, second['snapshot_sha256'])['payloads_verified']

def test_sync_failure_after_visible_commit_reports_published_with_unconfirmed_durability(saved, monkeypatch):
    app, _, repository, first = saved
    sync = transport._sync_directory
    def fail_final_sync(path):
        if Path(path) == repository and json.loads((repository / 'head.json').read_bytes())['snapshot'] != first['snapshot_sha256']:
            raise OSError('Actual final directory sync unavailable')
        return sync(path)
    monkeypatch.setattr(transport, '_sync_directory', fail_final_sync)
    second = incremental.create_snapshot(app, repository, parent=first['snapshot_sha256'])
    assert second['published'] and second['durability_confirmed'] is False
    assert incremental.verify_snapshot(repository, second['snapshot_sha256'])['payloads_verified']

def test_history_budget_does_not_prevent_explicit_token_recovery(saved, monkeypatch):
    app, _, repository, first = saved
    second = incremental.create_snapshot(app, repository, parent=first['snapshot_sha256'])
    monkeypatch.setattr(incremental, 'MAX_HISTORY', 1)
    with pytest.raises(ValueError, match='allowance'):
        incremental.list_snapshots(repository)
    assert incremental.verify_snapshot(repository, first['snapshot_sha256'])['payloads_verified']
    assert incremental.verify_snapshot(repository, second['snapshot_sha256'])['payloads_verified']


def test_canonical_budget_refuses_successor_without_mutating_old_history(saved):
    app, _, repository, first = saved
    before = (repository / 'head.json').read_bytes()
    with pytest.raises(ValueError):
        incremental.create_snapshot(app, repository, parent=first['snapshot_sha256'], max_bytes=1)
    assert (repository / 'head.json').read_bytes() == before
    assert incremental.verify_snapshot(repository, first['snapshot_sha256'])['payloads_verified']


_CRASH_CHILD = r"""
import os
from pathlib import Path
import sys
from carr_library.application import LibraryApp
from carr_library import incremental_backup as m
from carr_library.sync import vault as transport
from carr_library import file_publication
root, repository, parent, phase, expected_origin, expected_vault = sys.argv[1:]
assert Path(m.__file__).resolve() == Path(expected_origin).resolve(), 'Wrong child adapter origin'
assert Path(transport.__file__).resolve() == Path(expected_vault).resolve(), 'Wrong child Vault origin'
app = LibraryApp(root)
exclusive = file_publication.publish_directory_no_replace
replace = os.replace

def publish_or_die(source, target):
    target = Path(target)
    if phase == 'chunk-before' and target.parent.parent.name == 'chunks':
        os._exit(70)
    result = exclusive(source, target)
    if phase == 'chunk' and target.parent.parent.name == 'chunks':
        os._exit(71)
    if phase == 'descriptor' and target.parent.name == 'snapshots':
        os._exit(72)
    return result

def replace_or_die(source, target, *args, **kwargs):
    result = replace(source, target, *args, **kwargs)
    if phase == 'head' and Path(target).name == 'head.json':
        os._exit(73)
    return result

file_publication.publish_directory_no_replace = publish_or_die
os.replace = replace_or_die
m.create_snapshot(app, repository, parent=parent)
raise SystemExit('Expected interruption boundary was not reached.')
"""


@pytest.mark.parametrize('phase,exit_code', [
    ('chunk-before', 70), ('chunk', 71), ('descriptor', 72), ('head', 73)])
def test_actual_process_death_preserves_older_snapshot_and_commit_boundary(saved, phase, exit_code):
    app, workspace, repository, first = saved
    app.add_text(workspace, 'Second', 'Railway changed child data.')
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    child = subprocess.run([sys.executable, '-B', '-c', _CRASH_CHILD, str(app.root),
                            str(repository), first['snapshot_sha256'], phase,
                            str(Path(incremental.__file__).resolve()),
                            str(Path(transport.__file__).resolve())],
                           capture_output=True, text=True, timeout=45, env=environment)
    assert child.returncode == exit_code, (child.stdout, child.stderr)
    assert incremental.verify_snapshot(repository, first['snapshot_sha256'])['payloads_verified']
    committed = incremental.list_snapshots(repository)
    assert len(committed) == (2 if phase == 'head' else 1)
    if phase == 'head':
        current = next(row for row in committed if row['snapshot_sha256'] != first['snapshot_sha256'])
        assert incremental.verify_snapshot(repository, current['snapshot_sha256'])['payloads_verified']
    if phase == 'descriptor':
        # Legacy informational enumeration may expose a complete orphan, while
        # this adapter's completed-history listing remains bound to HEAD.
        assert len(transport.Vault.open_existing(repository).list_snapshots()) == 2
    assert all(path.stat().st_nlink == 1 for path in (repository / 'chunks').glob('*/*'))
    assert all(path.stat().st_nlink == 1 for path in (repository / 'snapshots').iterdir())


def test_reusable_object_is_rehashed_even_when_size_and_timestamp_are_preserved(saved):
    app, _, repository, first = saved
    row = next(row for row in _descriptor(repository, first['snapshot_sha256'])['files']
               if row['path'].startswith('originals/'))
    path = _object(repository, row['sha256'])
    stamp = path.stat()
    raw = path.read_bytes()
    path.write_bytes(bytes([raw[0] ^ 1]) + raw[1:])
    os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    with pytest.raises(ValueError, match='integrity'):
        incremental.create_snapshot(app, repository, parent=first['snapshot_sha256'])
    assert len(incremental.list_snapshots(repository)) == 1


def test_payload_mutation_during_opened_read_is_rejected(saved, tmp_path, monkeypatch):
    _, _, repository, first = saved
    row = next(row for row in _descriptor(repository, first['snapshot_sha256'])['files']
               if row['path'].startswith('originals/'))
    source = _object(repository, row['chunks'][0])
    inode = source.stat().st_ino
    fdopen = os.fdopen
    changed = False
    class ChangedRead:
        def __init__(self, stream):
            self.stream = stream
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.stream.close()
        def fileno(self):
            return self.stream.fileno()
        def read(self, size=-1):
            nonlocal changed
            if not changed:
                raw = self.stream.read()
                with source.open('r+b') as writer:
                    writer.write(bytes([raw[0] ^ 1]) + raw[1:])
                    writer.flush()
                self.stream.seek(0)
                changed = True
            return self.stream.read(size)
    def opening(fd, *args, **kwargs):
        selected = os.fstat(fd).st_ino == inode
        stream = fdopen(fd, *args, **kwargs)
        return ChangedRead(stream) if selected else stream
    monkeypatch.setattr(os, 'fdopen', opening)
    with pytest.raises(ValueError):
        incremental.materialize_snapshot(repository, first['snapshot_sha256'], tmp_path / 'full')
    assert changed and not (tmp_path / 'full').exists()


def test_existing_repository_writer_blocks_successor_without_damage(saved):
    app, _, repository, first = saved
    from carr_library.ingest.writer_guard import exclusive_writer
    with exclusive_writer(repository):
        with pytest.raises(RuntimeError):
            incremental.create_snapshot(app, repository)
    assert incremental.verify_snapshot(repository, first['snapshot_sha256'])['payloads_verified']


def test_prepared_pending_source_restores_without_external_original(tmp_path):
    from uuid import uuid4
    from carr_library.catalog.intake import snapshot
    from carr_library.extract.page_ocr import Config
    from carr_library.ingest.worker import load_request, prepare_request
    app, workspace, repository = _library(tmp_path)
    original = tmp_path / 'queued.txt'
    original.write_text('Queued original survives incremental relocation.', encoding='utf-8')
    metadata = dict(title='Queued book', authors=[], language=None, edition=None, volume=None,
                    collections=[], work_id=uuid4().hex, edition_id=uuid4().hex)
    planned = snapshot(original.parent, original.name).manifest()
    request = prepare_request(app.root, workspace, original, metadata, Config(), expected_manifest=planned)
    saved = incremental.create_snapshot(app, repository)
    original.unlink()
    restored = _restore(repository, saved['snapshot_sha256'], tmp_path / 'restored')
    rebound = load_request(restored.root, request['request_id'])
    assert rebound['data_dir'] == str(restored.root)
    assert rebound['expected_manifest'] == planned
    retained = Path(rebound['source'])
    assert retained.is_relative_to(restored.root)
    assert retained.read_bytes() == b'Queued original survives incremental relocation.'
    assert restored.resume_import(workspace, request['request_id'])['state'] == 'complete'
    assert restored.search(workspace, 'relocation')[0].quote == 'relocation'


@pytest.mark.parametrize('same_path', [False, True])
def test_managed_clips_restore_bytes_without_old_output_authority(tmp_path, monkeypatch, same_path):
    from carr_library.speech import VoiceRegistry
    from carr_library.studio.project import Cue, Project, Scene
    from carr_library.studio.render import ProductionService
    app, _, repository = _library(tmp_path)
    root = app.root
    studio = root / 'state/studio'
    raw = b'Retained portable source; not an inference fixture.'
    relative = 'inputs/' + sha256(raw).hexdigest() + '.audio'
    selected = studio / relative
    selected.parent.mkdir(parents=True)
    selected.write_bytes(raw)
    project = Project('portable', 'Portable', (), (Scene('scene', 'Scene', (),
        cues=(Cue('selected', relative),)),), schema=2)
    def forbidden_runtime(*args, **kwargs):
        pytest.fail('Backup/restore must not activate a runtime.')
    service = ProductionService(studio, voices=VoiceRegistry([]), runtime_factory=forbidden_runtime)
    old_output = tmp_path / 'old-output'
    old_output.mkdir()
    identity = service.submit(project, old_output, operation_id='retained-pending')
    original_job = (service.jobs_root / identity / 'job.json').read_bytes()
    old_origin = json.loads((studio / '.local-origin.json').read_bytes())
    snapshot = incremental.create_snapshot(app, repository)
    assert not any('.local-origin' in row['path'] for row in
                   _descriptor(repository, snapshot['snapshot_sha256'])['files'])
    shutil.rmtree(root)
    old_output.rmdir()
    destination = root if same_path else tmp_path / 'restored'
    result = incremental.restore_snapshot(repository, snapshot['snapshot_sha256'], destination)
    assert result['restored']
    restored_studio = destination / 'state/studio'
    assert not (restored_studio / '.local-origin.json').exists()
    assert (restored_studio / relative).read_bytes() == raw
    restored = service if same_path else ProductionService(restored_studio,
        voices=VoiceRegistry([]), runtime_factory=forbidden_runtime)
    actual_open, actual_stat = Path.open, Path.stat
    def guarded_open(path, *args, **kwargs):
        assert not Path(path).is_relative_to(old_output), 'Opened old external output'
        return actual_open(path, *args, **kwargs)
    def guarded_stat(path, *args, **kwargs):
        assert not Path(path).is_relative_to(old_output), 'Inspected old external output'
        return actual_stat(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', guarded_open)
    monkeypatch.setattr(Path, 'stat', guarded_stat)
    assert restored.get(identity)['recovery']['requires_output_directory']
    with pytest.raises(ValueError, match='newly selected output'):
        restored.run(identity)
    assert (restored.jobs_root / identity / 'job.json').read_bytes() == original_job
    assert json.loads((restored_studio / '.local-origin.json').read_bytes())['nonce'] != old_origin['nonce']


def test_source_bound_translation_remains_passive_and_exports_after_incremental_restore(tmp_path, monkeypatch):
    import runpy
    from types import SimpleNamespace
    from carr_library.assistant.providers import Gateway, UsageLedger
    from carr_library.assistant.translation import TranslationService
    helpers = runpy.run_path(str(Path(__file__).parents[1] / 'assistant/test_translation_advanced_source.py'))
    case = helpers['advanced_source'].__wrapped__(tmp_path, monkeypatch)
    identity, _ = helpers['submit'](case, helpers['GLOSSARY'])
    assert case.service.resume(identity)['status'] == 'complete'
    job = case.service.jobs.load(identity)
    calls = len(case.calls)
    repository = tmp_path / 'repository'
    incremental.initialize_repository(repository)
    with monkeypatch.context() as inactive:
        inactive.setattr(Gateway, '__init__', lambda *a, **k: pytest.fail('Backup constructed a gateway'))
        inactive.setattr(Gateway, 'request', lambda *a, **k: pytest.fail('Backup contacted a provider'))
        inactive.setattr(subprocess, 'Popen', lambda *a, **k: pytest.fail('Backup activated a process'))
        saved = incremental.create_snapshot(case.app, repository)
        restored = _restore(repository, saved['snapshot_sha256'], tmp_path / 'restored')
        directory = restored.root / 'state/translation'
        service = TranslationService(directory, library=restored,
            gateway=SimpleNamespace(ledger=UsageLedger(directory / 'provider/usage')))
        assert service.jobs.load(identity) == job
        output = tmp_path / 'offline-export'
        assert service.export(identity, output)['status'] == 'complete'
        assert (output / 'original.txt').read_text(encoding='utf-8') == case.source.text
    assert len(case.calls) == calls


def test_materialization_competing_complete_directory_is_not_claimed_as_ours(saved, tmp_path, monkeypatch):
    _, _, repository, first = saved
    target = tmp_path / 'conflicted-full'
    publish = incremental.publish_directory_no_replace
    created = []
    def competing_create(stage, destination):
        if Path(destination) == target:
            shutil.copytree(stage, destination)
            created.append((target / 'manifest.json').read_bytes())
        return publish(stage, destination)
    monkeypatch.setattr(incremental, 'publish_directory_no_replace', competing_create)
    with pytest.raises(FileExistsError):
        incremental.materialize_snapshot(repository, first['snapshot_sha256'], target)
    assert len(created) == 1 and (target / 'manifest.json').read_bytes() == created[0]
    assert verify_backup(target, first['backup_manifest_sha256'])['version'] == 4

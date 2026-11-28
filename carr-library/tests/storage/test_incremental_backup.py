"""Real complete-backup snapshots, integrity boundaries and commit semantics."""
from dataclasses import asdict
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess

import pytest

from carr_library.application import LibraryApp
from carr_library import incremental_backup as snapshots
from carr_library.progress import FILENAME, ProgressPreferences, save_preferences, load_preferences
from carr_library.reader.reading_desk import ReadingDesk
from carr_library.reader.reading_state import ReadingState


@pytest.fixture
def populated(tmp_path):
    app = LibraryApp(tmp_path / 'data')
    workspace = app.create_workspace('Synthetic snapshot library')
    other = app.create_workspace('Separate snapshot library')
    imported = app.add_text(workspace, 'Exact original', 'Точна цитата. Railway archive.\r\nSecond line.',
                            authors=('Synthetic author',))
    app.add_text(other, 'Other original', 'Separate workspace exact text.')
    hit, = app.search(workspace, 'Точна')
    topic = app.create_topic(workspace, 'Evidence')
    evidence = app.save_hit(workspace, topic, hit.ref, hit.revision, 'Separate preserved note')
    location = ReadingDesk(app, workspace, imported['edition']).page(1).location
    state = ReadingState(app.root)
    state.remember(location)
    state.add_bookmark(location, 'Повернутися')
    save_preferences(app.root, ProgressPreferences(mode='beep', volume=.31))
    return app, workspace, other, topic, evidence


def _capture(populated, tmp_path):
    app, *_ = populated
    repository = tmp_path / 'snapshots'
    receipt = snapshots.create_snapshot(app, repository)
    return repository, receipt


def _manifest(repository, receipt):
    digest = receipt.snapshot.snapshot_sha256
    return json.loads((repository / snapshots.SNAPSHOTS / (digest + '.json')).read_bytes())


def _reseal(repository, value):
    value['file_count'] = len(value['files'])
    value['total_bytes'] = sum(row['size_bytes'] for row in value['files'])
    raw = snapshots._encode(value)
    digest = sha256(raw).hexdigest()
    (repository / snapshots.SNAPSHOTS / (digest + '.json')).write_bytes(raw)
    return digest


def _object(repository, raw):
    digest = sha256(raw).hexdigest()
    target = repository / snapshots.OBJECTS / digest
    if not target.exists():
        target.write_bytes(raw)
    return digest


def test_complete_snapshot_restores_exact_originals_evidence_and_typed_preferences(populated, tmp_path):
    app, workspace, other, topic, _ = populated
    before = app.evidence(workspace, topic)[0]
    repository, receipt = _capture(populated, tmp_path)
    assert receipt.new_objects > 0 and receipt.new_bytes > 0 and receipt.cleanup_warning is None
    assert snapshots.verify_snapshot(repository, receipt.snapshot.snapshot_sha256) == receipt.snapshot
    restored_receipt = snapshots.restore_snapshot(repository, receipt.snapshot.snapshot_sha256,
                                                  tmp_path / 'restored')
    assert restored_receipt.cleanup_warning is None
    restored = LibraryApp(restored_receipt.path)
    after = restored.evidence(workspace, topic)[0]
    assert (after.anchor, after.note) == (before.anchor, before.note)
    hit, = restored.search(workspace, 'Точна')
    assert restored.copy(workspace, hit.ref, hit.revision, 'quote') == before.anchor.quote
    assert not restored.search(other, 'Railway')
    assert restored.search(other, 'Separate workspace')[0].quote == 'Separate workspace'
    assert load_preferences(restored.root) == load_preferences(app.root)
    assert (restored.root / 'reading-state.json').read_bytes() == (app.root / 'reading-state.json').read_bytes()
    assert all(path.stat().st_nlink == 1 for path in (restored.root / 'originals').iterdir())
    assert len(snapshots.list_snapshots(repository)) == 1


def test_unchanged_file_objects_are_reused_without_rewriting_and_rehashed(populated, tmp_path, monkeypatch):
    app, *_ = populated
    repository, first = _capture(populated, tmp_path)
    before = {p.name: (p.stat().st_mtime_ns, p.read_bytes()) for p in (repository / snapshots.OBJECTS).iterdir()}
    visited, original = [], snapshots._stream
    def observed(path, **options):
        if Path(path).parent == repository / snapshots.OBJECTS:
            visited.append(Path(path).name)
        return original(path, **options)
    monkeypatch.setattr(snapshots, '_stream', observed)
    second = snapshots.create_snapshot(app, repository)
    assert second.snapshot.snapshot_sha256 != first.snapshot.snapshot_sha256
    assert second.reused_files > 0 and second.reused_bytes > 0
    common = set(before) & {row['sha256'] for row in _manifest(repository, second)['files']}
    assert common and common <= set(visited)
    for name in common:
        path = repository / snapshots.OBJECTS / name
        assert (path.stat().st_mtime_ns, path.read_bytes()) == before[name]
    assert all(path.stat().st_nlink == 1 for path in (repository / snapshots.OBJECTS).iterdir())


def test_new_snapshot_is_independent_of_prior_manifest_and_preserves_old_snapshot(populated, tmp_path):
    app, workspace, *_ = populated
    repository, first = _capture(populated, tmp_path)
    app.add_text(workspace, 'Added later', 'Only the second snapshot contains this sentence.')
    second = snapshots.create_snapshot(app, repository)
    old = snapshots.restore_snapshot(repository, first.snapshot.snapshot_sha256, tmp_path / 'old')
    assert not LibraryApp(old.path).search(workspace, 'Only the second')
    first_manifest = repository / snapshots.SNAPSHOTS / (first.snapshot.snapshot_sha256 + '.json')
    first_manifest.unlink()
    newer = snapshots.restore_snapshot(repository, second.snapshot.snapshot_sha256, tmp_path / 'newer')
    assert len(LibraryApp(newer.path).search(workspace, 'Only the second')) == 1
    assert len(LibraryApp(newer.path).search(workspace, 'Railway')) == 1


@pytest.mark.parametrize('damage', ['same-size', 'truncated', 'missing', 'hardlink'])
def test_bad_content_object_blocks_verify_restore_and_reuse(populated, tmp_path, damage):
    app, *_ = populated
    repository, receipt = _capture(populated, tmp_path)
    manifest = _manifest(repository, receipt)
    row = next(row for row in manifest['files'] if row['path'].startswith('originals/'))
    path = repository / snapshots.OBJECTS / row['sha256']
    before = path.read_bytes()
    if damage == 'same-size':
        metadata = path.stat()
        path.write_bytes(b'X' * len(before))
        os.utime(path, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
    elif damage == 'truncated':
        path.write_bytes(before[:-1])
    elif damage == 'missing':
        path.unlink()
    else:
        os.link(path, tmp_path / 'unrelated-hardlink')
    # History explicitly validates metadata only, not objects.
    assert snapshots.list_snapshots(repository)[0] == receipt.snapshot
    with pytest.raises((ValueError, RuntimeError, OSError)):
        snapshots.verify_snapshot(repository, receipt.snapshot.snapshot_sha256)
    target = tmp_path / 'refused'
    with pytest.raises((ValueError, RuntimeError, OSError)):
        snapshots.restore_snapshot(repository, receipt.snapshot.snapshot_sha256, target)
    assert not target.exists()
    if damage != 'missing':
        with pytest.raises((ValueError, RuntimeError, OSError)):
            snapshots.create_snapshot(app, repository)
        assert len(tuple((repository / snapshots.SNAPSHOTS).iterdir())) == 1
        assert path.read_bytes() == (b'X' * len(before) if damage == 'same-size' else before[:-1] if damage == 'truncated' else before)


@pytest.mark.parametrize('field', ['checksum', 'duplicate-key', 'extra-key', 'boolean-count',
                                   'wrong-summary', 'manifest-binding', 'directory-alias', 'file-directory'])
def test_untrusted_snapshot_metadata_is_rejected_before_destination_publication(populated, tmp_path, field):
    repository, receipt = _capture(populated, tmp_path)
    value = _manifest(repository, receipt)
    digest = receipt.snapshot.snapshot_sha256
    if field == 'checksum':
        digest = '0' * 64
        path = repository / snapshots.SNAPSHOTS / (digest + '.json')
        path.write_bytes(snapshots._encode(value))
    elif field == 'duplicate-key':
        raw = snapshots._encode(value).replace(b'{"', b'{"version":1,"', 1)
        digest = sha256(raw).hexdigest()
        (repository / snapshots.SNAPSHOTS / (digest + '.json')).write_bytes(raw)
    else:
        if field == 'extra-key':
            value['command'] = 'never execute metadata'
        elif field == 'boolean-count':
            value['file_count'] = True
        elif field == 'wrong-summary':
            value['total_bytes'] += 1
        elif field == 'manifest-binding':
            value['backup_manifest_sha256'] = '0' * 64
        elif field == 'directory-alias':
            value['directories'].extend(['Case', 'case'])
            value['directories'].sort()
        else:
            value['directories'].append(value['files'][0]['path'])
            value['directories'].sort()
        raw = snapshots._encode(value)
        digest = sha256(raw).hexdigest()
        (repository / snapshots.SNAPSHOTS / (digest + '.json')).write_bytes(raw)
    target = tmp_path / 'refused'
    with pytest.raises(ValueError):
        snapshots.restore_snapshot(repository, digest, target)
    assert not target.exists()


@pytest.mark.parametrize('name', ['../outside', '/absolute', r'C:\outside', 'CON.txt',
                                  'unsafe:stream', 'trailing. ', 'a//b', 'a/../b'])
def test_rehashed_unsafe_relative_names_never_escape_reconstruction(populated, tmp_path, name):
    repository, receipt = _capture(populated, tmp_path)
    value = _manifest(repository, receipt)
    value['files'][0]['path'] = name
    value['files'].sort(key=lambda row: row['path'])
    digest = _reseal(repository, value)
    with pytest.raises(ValueError):
        snapshots.restore_snapshot(repository, digest, tmp_path / 'refused')
    assert not (tmp_path / 'refused').exists()
    assert not (tmp_path / 'outside').exists()


def test_rehashed_outer_and_inner_manifests_still_require_typed_library_validation(populated, tmp_path):
    repository, receipt = _capture(populated, tmp_path)
    value = _manifest(repository, receipt)
    preference = next(row for row in value['files'] if row['path'].endswith(FILENAME))
    raw = (repository / snapshots.OBJECTS / preference['sha256']).read_bytes()
    broken = json.loads(raw)
    broken['preferences']['suppress_during_media'] = 'not a boolean'
    malformed = snapshots._encode(broken)
    preference.update(sha256=_object(repository, malformed), size_bytes=len(malformed))
    outer = next(row for row in value['files'] if row['path'] == 'manifest.json')
    full_backup = json.loads((repository / snapshots.OBJECTS / outer['sha256']).read_bytes())
    inner = next(row for row in full_backup['sidecars']
                 if row.get('path', row.get('filename')) == FILENAME)
    inner.update(sha256=preference['sha256'], size_bytes=preference['size_bytes'])
    full_raw = snapshots._encode(full_backup)
    outer.update(sha256=_object(repository, full_raw), size_bytes=len(full_raw))
    value['backup_manifest_sha256'] = outer['sha256']
    digest = _reseal(repository, value)
    with pytest.raises(ValueError, match='suppress_during_media'):
        snapshots.restore_snapshot(repository, digest, tmp_path / 'refused')
    assert not (tmp_path / 'refused').exists()
    assert snapshots.verify_snapshot(repository, receipt.snapshot.snapshot_sha256) == receipt.snapshot


@pytest.mark.parametrize('phase', ['first-object', 'before-manifest'])
def test_cancellation_never_publishes_completed_snapshot_and_can_retry(populated, tmp_path, monkeypatch, phase):
    app, *_ = populated
    repository, stopped = tmp_path / 'snapshots', [False]
    original_publish, original_write = snapshots._publish, snapshots._write
    def publish(source, target):
        result = original_publish(source, target)
        if phase == 'first-object' and target.parent.name == snapshots.OBJECTS:
            stopped[0] = True
        return result
    def write(path, raw):
        original_write(path, raw)
        if phase == 'before-manifest' and path.name == 'snapshot.json':
            stopped[0] = True
    monkeypatch.setattr(snapshots, '_publish', publish)
    monkeypatch.setattr(snapshots, '_write', write)
    with pytest.raises(InterruptedError):
        snapshots.create_snapshot(app, repository, cancelled=lambda: stopped[0])
    assert not tuple((repository / snapshots.SNAPSHOTS).iterdir())
    assert not tuple((repository / snapshots.STAGING).iterdir())
    monkeypatch.setattr(snapshots, '_publish', original_publish)
    monkeypatch.setattr(snapshots, '_write', original_write)
    retried = snapshots.create_snapshot(app, repository)
    assert snapshots.verify_snapshot(repository, retried.snapshot.snapshot_sha256) == retried.snapshot


def test_cancellation_after_snapshot_commit_keeps_success_receipt(populated, tmp_path, monkeypatch):
    app, *_ = populated
    stopped, original = [False], snapshots._publish
    def publish(source, target):
        result = original(source, target)
        if target.parent.name == snapshots.SNAPSHOTS:
            stopped[0] = True
        return result
    monkeypatch.setattr(snapshots, '_publish', publish)
    repository = tmp_path / 'snapshots'
    receipt = snapshots.create_snapshot(app, repository, cancelled=lambda: stopped[0])
    assert stopped[0] and receipt.snapshot.file_count >= 2
    from carr_library.progress import ProgressHub, finish_result
    reporter = ProgressHub().start('Snapshot')
    finish_result(reporter, receipt, cancelled=True)
    assert reporter.event.state == 'complete' and reporter.event.verified
    assert snapshots.verify_snapshot(repository, receipt.snapshot.snapshot_sha256) == receipt.snapshot


@pytest.mark.parametrize('operation', ['create', 'restore'])
def test_postcommit_private_cleanup_error_keeps_completed_receipt(populated, tmp_path, monkeypatch, operation):
    app, *_ = populated
    repository, prior = _capture(populated, tmp_path)
    factory = snapshots.TemporaryDirectory
    def temporary(*args, **kwargs):
        instance = factory(*args, **kwargs)
        cleanup = instance.cleanup
        if kwargs.get('prefix') == ('capture-' if operation == 'create' else '.carr-snapshot-read-'):
            def fail_after_cleanup():
                cleanup()
                raise OSError('synthetic private cleanup error')
            instance.cleanup = fail_after_cleanup
        return instance
    monkeypatch.setattr(snapshots, 'TemporaryDirectory', temporary)
    if operation == 'create':
        result = snapshots.create_snapshot(app, repository)
        assert result.cleanup_warning and 'saved' in result.cleanup_warning
        assert (repository / snapshots.SNAPSHOTS / (result.snapshot.snapshot_sha256 + '.json')).is_file()
    else:
        result = snapshots.restore_snapshot(repository, prior.snapshot.snapshot_sha256, tmp_path / 'restored')
        assert result.cleanup_warning and 'restored' in result.cleanup_warning
        assert len(LibraryApp(result.path).workspaces()) == 2


def test_restore_cancellation_before_publication_keeps_source_and_destination_absent(populated, tmp_path):
    repository, receipt = _capture(populated, tmp_path)
    calls = [0]
    def stopped():
        calls[0] += 1
        return calls[0] > 4
    with pytest.raises(InterruptedError):
        snapshots.restore_snapshot(repository, receipt.snapshot.snapshot_sha256, tmp_path / 'refused',
                                   cancelled=stopped)
    assert not (tmp_path / 'refused').exists()
    assert not tuple(tmp_path.glob('.carr-snapshot-read-*'))
    assert snapshots.verify_snapshot(repository, receipt.snapshot.snapshot_sha256) == receipt.snapshot


@pytest.mark.parametrize('kind', ['source-child', 'source-parent', 'existing-restore',
                                  'repository-child', 'unowned-repository'])
def test_containment_and_existing_data_are_never_overwritten(populated, tmp_path, kind):
    app, *_ = populated
    repository, receipt = _capture(populated, tmp_path)
    keep = tmp_path / 'unowned'
    keep.mkdir()
    (keep / 'keep.txt').write_bytes(b'preserve')
    with pytest.raises((ValueError, FileExistsError)):
        if kind == 'source-child':
            snapshots.create_snapshot(app, app.root / 'snapshots')
        elif kind == 'source-parent':
            snapshots.create_snapshot(app, app.root.parent)
        elif kind == 'existing-restore':
            snapshots.restore_snapshot(repository, receipt.snapshot.snapshot_sha256, keep)
        elif kind == 'repository-child':
            snapshots.restore_snapshot(repository, receipt.snapshot.snapshot_sha256, repository / 'restored')
        else:
            snapshots.create_snapshot(app, keep)
    assert (keep / 'keep.txt').read_bytes() == b'preserve'
    assert len(tuple((repository / snapshots.SNAPSHOTS).iterdir())) == 1


def test_repository_parent_redirection_is_rejected(populated, tmp_path):
    repository, receipt = _capture(populated, tmp_path)
    alias = tmp_path / 'alias'
    if os.name == 'nt':
        subprocess.run(['cmd', '/c', 'mklink', '/J', str(alias), str(repository)],
                       check=True, capture_output=True)
    else:
        alias.symlink_to(repository, target_is_directory=True)
    with pytest.raises(ValueError):
        snapshots.verify_snapshot(alias, receipt.snapshot.snapshot_sha256)
    assert snapshots.verify_snapshot(repository, receipt.snapshot.snapshot_sha256) == receipt.snapshot


@pytest.mark.parametrize('options', [{'max_files': True}, {'max_files': 0},
                                    {'max_bytes': True}, {'max_bytes': 0},
                                    {'max_bytes': 1}, {'max_files': 1}])
def test_explicit_budgets_reject_without_completed_snapshot(populated, tmp_path, options):
    app, *_ = populated
    repository = tmp_path / 'bounded'
    with pytest.raises(ValueError):
        snapshots.create_snapshot(app, repository, **options)
    assert not (repository / snapshots.SNAPSHOTS).exists() or not tuple((repository / snapshots.SNAPSHOTS).iterdir())


def test_empty_library_preserves_empty_original_directory(tmp_path):
    app = LibraryApp(tmp_path / 'empty')
    app.create_workspace('Empty workspace')
    repository = tmp_path / 'snapshots'
    receipt = snapshots.create_snapshot(app, repository)
    value = _manifest(repository, receipt)
    assert 'originals' in value['directories']
    restored = snapshots.restore_snapshot(repository, receipt.snapshot.snapshot_sha256, tmp_path / 'restored')
    assert (restored.path / 'originals').is_dir()
    assert not tuple((restored.path / 'originals').iterdir())


def test_snapshot_history_budget_and_invalid_entries_fail_closed(populated, tmp_path, monkeypatch):
    app, *_ = populated
    repository, receipt = _capture(populated, tmp_path)
    monkeypatch.setattr(snapshots, 'MAX_SNAPSHOTS', 1)
    with pytest.raises(ValueError, match='full'):
        snapshots.create_snapshot(app, repository)
    assert snapshots.verify_snapshot(repository, receipt.snapshot.snapshot_sha256) == receipt.snapshot
    (repository / snapshots.SNAPSHOTS / 'untrusted.txt').write_bytes(b'preserve')
    with pytest.raises(ValueError, match='Unexpected'):
        snapshots.list_snapshots(repository)
    assert (repository / snapshots.SNAPSHOTS / 'untrusted.txt').read_bytes() == b'preserve'

"""Verify the whole persisted artifact, not just its small manifest file."""
from hashlib import sha256
from pathlib import Path
import os

import pytest

from carr_library.assistant.backup_receipt import (
    BackupReceipt, capture_backup_receipt, verify_backup_receipt,
)


def backup(tmp_path):
    path = tmp_path / 'backup'
    path.mkdir()
    manifest = b'{"synthetic":true}'
    (path / 'manifest.json').write_bytes(manifest)
    (path / 'library.sqlite').write_bytes(b'synthetic database bytes' * 200)
    (path / 'originals').mkdir()
    (path / 'originals' / 'source.txt').write_text('Original text.', encoding='utf-8')
    return path, sha256(manifest).hexdigest()


def capture(path, digest, **kwargs):
    return capture_backup_receipt(path, digest, max_bytes=kwargs.pop('max_bytes', 10000),
                                  max_files=kwargs.pop('max_files', 10), **kwargs)


def test_whole_database_counts_towards_approved_budget(tmp_path):
    path, digest = backup(tmp_path)
    assert (path / 'manifest.json').stat().st_size < 4096
    with pytest.raises(ValueError, match='budget'):
        capture(path, digest, max_bytes=4096)
    assert (path / 'library.sqlite').is_file()  # Verification never deletes artifacts.


def test_receipt_roundtrip_and_deterministic_complete_tree(tmp_path):
    path, digest = backup(tmp_path)
    receipt = capture(path, digest)
    assert receipt.files == 3 and receipt.directories == 1
    assert receipt.total_bytes == sum(p.stat().st_size for p in path.rglob('*') if p.is_file())
    assert BackupReceipt.from_dict(receipt.to_dict()) == receipt
    assert verify_backup_receipt(path, receipt.to_dict(), max_bytes=10000, max_files=10) == receipt


@pytest.mark.parametrize('name', ['library.sqlite', 'originals/source.txt'])
def test_unchanged_manifest_cannot_hide_same_size_corruption(tmp_path, name):
    path, digest = backup(tmp_path)
    receipt = capture(path, digest)
    target = path / name
    raw = target.read_bytes()
    target.write_bytes(bytes([raw[0] ^ 1]) + raw[1:])
    assert sha256((path / 'manifest.json').read_bytes()).hexdigest() == digest
    with pytest.raises(ValueError, match='completed backup changed'):
        verify_backup_receipt(path, receipt, max_bytes=10000, max_files=10)


@pytest.mark.parametrize('change', ['extra_file', 'empty_directory', 'rename', 'delete'])
def test_unexpected_tree_changes_are_not_accepted_on_resume(tmp_path, change):
    path, digest = backup(tmp_path)
    receipt = capture(path, digest)
    if change == 'extra_file':
        (path / 'extra.txt').write_text('x')
    elif change == 'empty_directory':
        (path / 'extra').mkdir()
    elif change == 'rename':
        (path / 'originals/source.txt').rename(path / 'originals/renamed.txt')
    else:
        (path / 'originals/source.txt').unlink()
    with pytest.raises(ValueError, match='completed backup changed'):
        verify_backup_receipt(path, receipt, max_bytes=10000, max_files=10)


def test_cancel_and_file_limit_apply_before_complete_receipt(tmp_path):
    path, digest = backup(tmp_path)
    with pytest.raises(InterruptedError):
        capture(path, digest, cancelled=lambda: True)
    with pytest.raises(ValueError, match='budget'):
        capture(path, digest, max_files=2)


def test_producer_manifest_hash_is_required(tmp_path):
    path, _ = backup(tmp_path)
    with pytest.raises(ValueError, match='producer receipt'):
        capture(path, '0' * 64)


def test_receipt_rejects_extra_fields_and_boolean_counts(tmp_path):
    path, digest = backup(tmp_path)
    raw = capture(path, digest).to_dict()
    with pytest.raises(ValueError):
        BackupReceipt.from_dict({**raw, 'command': 'anything'})
    with pytest.raises(ValueError):
        BackupReceipt.from_dict({**raw, 'total_bytes': True})


def test_real_library_backup_receipt_detects_database_tamper(tmp_path):
    from carr_library.application import LibraryApp
    app = LibraryApp(tmp_path / 'data')
    app.create_workspace('Synthetic receipt acceptance')
    directory = tmp_path / 'complete'
    checksum = app.backup(directory)
    with pytest.raises(ValueError, match='budget'):
        capture(directory, checksum, max_bytes=4096)
    receipt = capture(directory, checksum, max_bytes=4 * 1024 * 1024)
    with (directory / 'library.sqlite').open('r+b') as stream:
        stream.seek(100)
        byte = stream.read(1)
        stream.seek(100)
        stream.write(bytes([byte[0] ^ 1]))
    with pytest.raises(ValueError, match='completed backup changed'):
        verify_backup_receipt(directory, receipt, max_bytes=4 * 1024 * 1024, max_files=10)


@pytest.mark.parametrize('operation', ['capture', 'verify', 'nested'])
def test_receipt_rejects_change_after_earlier_file_scan(tmp_path, monkeypatch, operation):
    import sqlite3
    from carr_library.assistant import backup_receipt as receipt

    root = tmp_path / 'backup'
    root.mkdir()
    with sqlite3.connect(root / 'library.sqlite') as db:
        db.execute('create table t(v text)')
        db.execute("insert into t values ('old')")
    (root / 'manifest.json').write_bytes(b'{"synthetic":true}')
    (root / 'originals').mkdir()
    (root / 'originals/source.txt').write_bytes(b'original')
    (root / 'z-last.txt').write_bytes(b'last')
    digest = sha256((root / 'manifest.json').read_bytes()).hexdigest()
    budget = dict(max_bytes=1000000, max_files=10)
    old = receipt.capture_backup_receipt(root, digest, **budget)
    plain = receipt._plain
    changed = []

    def hook(path, *, directory=False):
        trigger = root / ('z-last.txt' if operation == 'nested' else 'manifest.json')
        if path == trigger and not changed:
            if operation == 'nested':
                (root / 'originals/source.txt').write_bytes(b'modified')
            else:
                with sqlite3.connect(root / 'library.sqlite') as db:
                    # A real in-place writer, without a sibling journal whose
                    # directory timestamp would incidentally expose the edit.
                    db.execute('pragma journal_mode=off')
                    db.execute("update t set v='new'")
            changed.append(True)
        return plain(path, directory=directory)

    monkeypatch.setattr(receipt, '_plain', hook)
    with pytest.raises(ValueError, match='changed during verification'):
        if operation == 'capture':
            receipt.capture_backup_receipt(root, digest, **budget)
        else:
            receipt.verify_backup_receipt(root, old, **budget)
    assert changed


@pytest.mark.parametrize('kind', ['file', 'directory'])
def test_receipt_rechecks_names_after_an_earlier_directory_scan(tmp_path, monkeypatch, kind):
    from carr_library.assistant import backup_receipt as receipt

    root, digest = backup(tmp_path)
    (root / 'z-last.txt').write_bytes(b'last')
    parent = root / 'originals'
    before = parent.stat()
    plain = receipt._plain
    changed = []

    def hook(path, *, directory=False):
        if path == root / 'z-last.txt' and not changed:
            added = parent / 'added-after-scan'
            if kind == 'file':
                added.write_bytes(b'new unobserved bytes')
            else:
                added.mkdir()
            # On Windows a directory's ctime denotes creation and can remain
            # unchanged; preserving mtime must not hide a new child name.
            os.utime(parent, ns=(before.st_atime_ns, before.st_mtime_ns))
            changed.append(True)
        return plain(path, directory=directory)

    monkeypatch.setattr(receipt, '_plain', hook)
    with pytest.raises(ValueError, match='changed during verification'):
        capture(root, digest)
    assert changed


@pytest.mark.parametrize('phase', ['file', 'directory'])
@pytest.mark.parametrize('failure', [InterruptedError, TimeoutError])
def test_second_pass_preserves_cancellation_and_deadline(tmp_path, monkeypatch, phase, failure):
    from carr_library.assistant import backup_receipt as receipt

    root, digest = backup(tmp_path)
    source = root / 'library.sqlite'
    original = source.read_bytes()
    entered, ready = [], []
    if phase == 'file':
        actual = receipt.os.open

        def open_file(path, *args, **kwargs):
            result = actual(path, *args, **kwargs)
            if Path(path) == source:
                entered.append(True)
                if len(entered) == 2:
                    ready.append(True)
            return result
        monkeypatch.setattr(receipt.os, 'open', open_file)
    else:
        actual = receipt.os.scandir

        def scan_directory(path):
            result = actual(path)
            if Path(path) == root / 'originals':
                entered.append(True)
                if len(entered) == 2:
                    ready.append(True)
            return result
        monkeypatch.setattr(receipt.os, 'scandir', scan_directory)

    def cancelled():
        if ready:
            raise failure('Synthetic cancellation or deadline during re-attestation.')
        return False

    with pytest.raises(failure, match='Synthetic cancellation or deadline'):
        capture(root, digest, cancelled=cancelled)
    assert ready and len(entered) == 2
    assert source.read_bytes() == original


def _assistant_backup(tmp_path, **limits):
    from carr_library.application import LibraryApp
    from carr_library.assistant import AssistantService, Scope

    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Real backup replay')
    source = app.add_text(workspace, 'Retained original', 'Exact original Україна.\r\nSecond line.')
    output = tmp_path / 'outputs'
    output.mkdir()
    service = AssistantService(app)
    scope = Scope(workspace_id=workspace, output_root=output, allow_full_backup=True,
                  **limits)
    identity = service.submit(service.preview('Backup "complete"', scope))
    return app, service, identity, output / 'complete', source


@pytest.mark.parametrize('change', ['database', 'original', 'extra', 'empty-directory', 'rename', 'delete'])
def test_actual_assistant_replay_rejects_changed_backup_with_unchanged_manifest(tmp_path, change):
    app, service, identity, destination, source = _assistant_backup(tmp_path)
    result = service.run(identity)
    assert result.status == 'complete', result
    manifest = (destination / 'manifest.json').read_bytes()
    original = destination / 'originals' / app.assets.resolve(source['asset']).name
    if change in ('database', 'original'):
        target = destination / 'library.sqlite' if change == 'database' else original
        previous = target.stat()
        raw = target.read_bytes()
        target.write_bytes(bytes([raw[0] ^ 1]) + raw[1:])
        os.utime(target, ns=(previous.st_atime_ns, previous.st_mtime_ns))
    elif change == 'extra':
        (destination / 'unexpected.txt').write_bytes(b'new bytes')
    elif change == 'empty-directory':
        (destination / 'unexpected').mkdir()
    elif change == 'rename':
        original.rename(original.with_name('renamed.txt'))
    else:
        original.unlink()
    after = {p.relative_to(destination).as_posix(): p.read_bytes()
             for p in destination.rglob('*') if p.is_file()}
    reopened = type(service)(type(app)(app.root))
    report = reopened.resume(identity)
    assert report.status == 'failed', report
    assert 'backup' in report.message.lower() and 'changed' in report.message.lower()
    assert (destination / 'manifest.json').read_bytes() == manifest
    assert {p.relative_to(destination).as_posix(): p.read_bytes()
            for p in destination.rglob('*') if p.is_file()} == after


def test_actual_assistant_unchanged_backup_reuses_exact_tree_without_republication(tmp_path, monkeypatch):
    app, service, identity, destination, _ = _assistant_backup(tmp_path)
    first = service.run(identity)
    assert first.status == 'complete', first
    receipt = first.results[0]['backup_receipt']
    assert receipt['total_bytes'] == sum(p.stat().st_size for p in destination.rglob('*') if p.is_file())
    before = {p.relative_to(destination).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
              for p in destination.rglob('*') if p.is_file()}
    def unexpected(*args, **kwargs):
        raise AssertionError('A completed backup must be verified, never recreated.')
    monkeypatch.setattr(app, 'backup', unexpected)
    replay = service.resume(identity)
    assert replay.status == 'complete' and replay.results == first.results
    assert {p.relative_to(destination).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in destination.rglob('*') if p.is_file()} == before


def test_actual_legacy_assistant_backup_requires_review_without_silent_recreation(tmp_path, monkeypatch):
    app, service, identity, destination, _ = _assistant_backup(tmp_path)
    assert service.run(identity).status == 'complete'
    record = service.store.load(identity)
    record['steps'][0]['result'].pop('backup_receipt', None)
    service.store.save(record)
    def unexpected(*args, **kwargs):
        raise AssertionError('An old completed result is not permission for another backup.')
    monkeypatch.setattr(app, 'backup', unexpected)
    report = service.resume(identity)
    assert report.status == 'failed' and 'full verification' in report.message
    assert (destination / 'manifest.json').is_file()


@pytest.mark.parametrize('limits', [{'max_bytes': 4096}, {'max_files': 1}])
def test_actual_assistant_backup_budget_fails_before_destination_publication(tmp_path, limits):
    app, service, identity, destination, source = _assistant_backup(tmp_path, **limits)
    original = app.assets.resolve(source['asset']).read_bytes()
    result = service.run(identity)
    assert result.status == 'failed' and 'budget' in result.message.lower(), result
    assert not destination.exists()
    assert not tuple(destination.parent.glob('.carr-library-backup-*'))
    assert app.assets.resolve(source['asset']).read_bytes() == original


def test_actual_assistant_cancellation_reaches_backup_producer_before_publication(tmp_path):
    app, service, identity, destination, source = _assistant_backup(tmp_path)
    entered = []
    def cancelled():
        if any((stage / 'library.sqlite').is_file()
               for stage in destination.parent.glob('.carr-library-backup-*')):
            entered.append(True)
        return bool(entered)
    result = service.run(identity, cancelled=cancelled)
    assert entered and result.status == 'paused', result
    assert not destination.exists()
    assert not tuple(destination.parent.glob('.carr-library-backup-*'))
    assert app.assets.resolve(source['asset']).is_file()


@pytest.mark.parametrize('dimension', ['bytes', 'files'])
@pytest.mark.parametrize('remaining', [-1, 0])
def test_actual_complete_budget_includes_manifest_reading_pending_and_retained_cues(
        tmp_path, dimension, remaining):
    import json
    import wave

    from carr_library.application import LibraryApp
    from carr_library.durable_state import atomic_json
    from carr_library.library_backup import restore_backup
    from carr_library.reader.reading_desk import ReadingDesk
    from carr_library.reader.reading_state import ReadingState
    from carr_library.studio.project import Cue, Project, ProjectStore, Scene

    app = LibraryApp(tmp_path / 'source')
    workspace = app.create_workspace('Complete budget')
    book = app.add_text(workspace, 'Original', 'Preserved original quotation.')
    page = ReadingDesk(app, workspace, book['edition']).page(1)
    mark = ReadingState(app.root).add_bookmark(page.location, 'Exact personal location')
    atomic_json(app.root / 'state/speech/settings.json', {'voice': 'explicit preference'})
    cue_path = tmp_path / 'selected-cue.wav'
    with wave.open(str(cue_path), 'wb') as output:
        output.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
        output.writeframes(b'\0\0' * 240)
    project = Project('retained-cue', 'Retained input', (),
        (Scene('scene-1', 'Exact cue', (), cues=(Cue('cue-1', str(cue_path)),)),))
    ProjectStore(app.root / 'state/studio/projects').save(project)
    pending = tmp_path / 'pending.html'
    pending.write_text('<html><body>Retained pending HTML.</body></html>', encoding='utf-8')
    batch = app.plan_import_batch(workspace, (pending,), recursive=False)

    reference = tmp_path / 'reference'
    checksum = app.backup(reference)
    manifest = json.loads((reference / 'manifest.json').read_bytes())
    assert manifest['version'] == 5 and manifest['pending']['files']
    assert any(entry['filename'].startswith('studio/inputs/') for entry in manifest['state'])
    paths = [path for path in reference.rglob('*') if path.is_file()]
    total = sum(path.stat().st_size for path in paths)
    limits = {'max_bytes': total, 'max_files': len(paths)}
    limits['max_' + dimension] += remaining
    target = tmp_path / 'limited'
    if remaining < 0:
        with pytest.raises(ValueError, match='budget'):
            app.backup(target, **limits)
        assert not target.exists()
        assert not tuple(tmp_path.glob('.carr-library-backup-*'))
        return

    assert app.backup(target, **limits) == checksum
    receipt = capture_backup_receipt(target, checksum, max_bytes=total, max_files=len(paths))
    assert receipt.total_bytes == total and receipt.files == len(paths)
    restored = LibraryApp(restore_backup(target, tmp_path / 'restored', checksum))
    assert ReadingState(restored.root).bookmarks(workspace, book['edition']) == (mark,)
    rebound = ProjectStore(restored.root / 'state/studio/projects').load(project.id)
    assert Path(rebound.scenes[0].cues[0].path).read_bytes() == cue_path.read_bytes()
    assert restored.import_batch(workspace, batch['batch_id'])['state'] == batch['state']
    assert restored.search(workspace, 'quotation')[0].quote == 'quotation'


@pytest.mark.parametrize('replace_handle', [False, True])
def test_supported_cmd_sidecar_keeps_identity_when_windows_handle_mode_differs(
        tmp_path, monkeypatch, replace_handle):
    import stat
    from types import SimpleNamespace

    from carr_library.application import LibraryApp
    from carr_library.assistant import backup_receipt as receipts

    app = LibraryApp(tmp_path / 'source')
    app.create_workspace('Ordinary sidecars')
    source = app.root / 'state' / 'retained-note.cmd'
    source.parent.mkdir()
    source.write_bytes(b'Ordinary retained bytes, never executed.\r\n')
    destination = tmp_path / 'backup'
    checksum = app.backup(destination)
    copied = destination / 'state' / source.name
    assert copied.read_bytes() == source.read_bytes()
    # Windows path stat synthesizes executable bits from this extension;
    # fstat does not. Reproduce that presentation difference on POSIX too.
    copied.chmod(copied.stat().st_mode | stat.S_IXUSR)
    other = tmp_path / 'other.cmd'
    other.write_bytes(copied.read_bytes())
    stamp = copied.stat()
    os.utime(other, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    assert copied.stat().st_ino != other.stat().st_ino
    opened = []

    def open_file(path, *args, **kwargs):
        if Path(path) == copied:
            opened.append(True)
            if replace_handle:
                path = other
        return os.open(path, *args, **kwargs)

    def handle_stat(fd):
        value = os.fstat(fd)
        return SimpleNamespace(st_dev=value.st_dev, st_ino=value.st_ino,
            st_mode=value.st_mode & ~0o111, st_size=value.st_size,
            st_mtime_ns=value.st_mtime_ns, st_ctime_ns=value.st_ctime_ns)

    monkeypatch.setattr(receipts, 'os', SimpleNamespace(**{
        **vars(os), 'open': open_file, 'fstat': handle_stat}))
    budget = dict(max_bytes=4 * 1024 * 1024, max_files=20)
    if replace_handle:
        with pytest.raises(ValueError, match='changed during verification'):
            receipts.capture_backup_receipt(destination, checksum, **budget)
    else:
        receipt = receipts.capture_backup_receipt(destination, checksum, **budget)
        assert receipts.verify_backup_receipt(destination, receipt, **budget) == receipt
    assert opened and source.read_bytes() == copied.read_bytes() == other.read_bytes()

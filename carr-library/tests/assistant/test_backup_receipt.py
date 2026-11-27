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


@pytest.mark.parametrize('trigger', ['later-file', 'last-callback'])
@pytest.mark.parametrize('change', ['earlier-payload', 'new-file', 'new-directory'])
def test_final_quiet_native_pins_reject_mutations_after_second_content_pass(
        tmp_path, monkeypatch, trigger, change):
    from contextlib import contextmanager
    from carr_library.assistant import backup_receipt as receipt

    root, digest = backup(tmp_path)
    (root / 'z-last.txt').write_bytes(b'Last bounded payload.')
    target = root / 'library.sqlite'
    original = target.read_bytes()
    arm, changed, visits = [], [], []
    actual_open, actual_scandir = receipt.os.open, receipt.os.scandir

    if trigger == 'later-file':
        def open_file(path, *args, **kwargs):
            fd = actual_open(path, *args, **kwargs)
            if Path(path) == root / 'z-last.txt':
                visits.append(True)
                if len(visits) == 2:
                    arm.append(True)
            return fd
        monkeypatch.setattr(receipt.os, 'open', open_file)
    else:
        @contextmanager
        def scan(path):
            with actual_scandir(path) as entries:
                yield entries
            if Path(path) == root:
                visits.append(True)
                if len(visits) == 2:
                    arm.append(True)
        monkeypatch.setattr(receipt.os, 'scandir', scan)

    def cancelled():
        if arm and not changed:
            if change == 'earlier-payload':
                stamp = target.stat()
                target.write_bytes(bytes([original[0] ^ 1]) + original[1:])
                os.utime(target, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
                assert target.stat().st_size == stamp.st_size
                assert target.stat().st_mtime_ns == stamp.st_mtime_ns
            else:
                parent = root / 'originals'
                stamp = parent.stat()
                extra = parent / 'after-second-pass'
                if change == 'new-file':
                    extra.write_bytes(b'Late new payload.')
                else:
                    extra.mkdir()
                os.utime(parent, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
            changed.append(change)
        return False

    with pytest.raises(ValueError, match='changed during verification'):
        capture(root, digest, cancelled=cancelled)
    assert arm and changed == [change]
    assert (root / 'manifest.json').is_file()


def test_no_external_callback_runs_after_the_final_native_pins_begin(tmp_path, monkeypatch):
    from carr_library.assistant import backup_receipt as receipt
    root, digest = backup(tmp_path)
    actual = receipt._native_pin
    entered = []
    def pin(path):
        entered.append(path)
        return actual(path)
    def cancelled():
        if entered:
            raise AssertionError('A late observer can mutate an already checked final payload.')
        return False
    monkeypatch.setattr(receipt, '_native_pin', pin)
    assert capture(root, digest, cancelled=cancelled).files == 3
    assert len(entered) == 3

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

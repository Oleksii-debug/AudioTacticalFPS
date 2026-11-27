"""Cross-candidate export guards on the canonical native metadata/receipt API."""
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from carr_library.research import export_file as e


@pytest.fixture(params=('text', 'binary'))
def publication(request, tmp_path):
    target = tmp_path / 'export'
    private = tmp_path / 'private'
    payload = 'Цитата\r\nOriginal' if request.param == 'text' else b'\x00Original\r\n'
    writer = e.write_export if isinstance(payload, str) else e.write_binary_export
    return target, private, lambda **kw: writer(target, payload, private_root=private, **kw)


@pytest.mark.parametrize('phase', ('explicit', 'approved-absence', 'after-staging'))
def test_unapproved_file_is_not_opened(publication, monkeypatch, phase):
    target, private, publish = publication
    previous = b'Independent work'
    approval = e.capture_export_destination(target, private_root=private)
    if phase != 'after-staging':
        target.write_bytes(previous)
    else:
        fsync = os.fsync
        def competing_writer(fd):
            fsync(fd)
            target.write_bytes(previous)
        monkeypatch.setattr(e.os, 'fsync', competing_writer)
    opened = []
    real_open = os.open
    def observe(path, *args, **kw):
        if Path(path) == target:
            opened.append(path)
        return real_open(path, *args, **kw)
    monkeypatch.setattr(e.os, 'open', observe)
    if phase == 'explicit':
        with pytest.raises(FileExistsError):
            publish(require_absent=True)
    else:
        with pytest.raises(ValueError, match='destination changed'):
            publish(approval=approval)
    assert not opened
    assert target.read_bytes() == previous
    assert not list(target.parent.glob('.research-export-*'))


def test_default_creation_does_not_clobber_at_publication(publication, monkeypatch):
    target, _, publish = publication
    previous = b'Independent work'
    original = e._destination
    count = 0
    def race(*args):
        nonlocal count
        state = original(*args)
        count += 1
        if count == 2:
            target.write_bytes(previous)
        return state
    monkeypatch.setattr(e, '_destination', race)
    with pytest.raises(FileExistsError):
        publish()
    assert target.read_bytes() == previous
    assert not list(target.parent.glob('.research-export-*'))


@pytest.mark.parametrize('operation', ('capture', 'write'))
def test_hash_cancellation_stops_after_first_real_block(tmp_path, monkeypatch, operation):
    target = tmp_path / 'export'
    previous = b'A' * (3 * 128 * 1024 + 7)
    target.write_bytes(previous)
    private = tmp_path / 'private'
    observed = []
    real_read = os.read
    def read(fd, amount):
        block = real_read(fd, amount)
        observed.append((fd, len(block)))
        return block
    monkeypatch.setattr(e.os, 'read', read)
    cancelled = lambda: bool(observed)
    with pytest.raises(TimeoutError, match='cancelled'):
        if operation == 'capture':
            e.capture_export_destination(target, private_root=private, cancelled=cancelled)
        else:
            e.write_export(target, 'Unpublished', private_root=private, cancelled=cancelled)
    assert len(observed) == 1 and observed[0][1] == 128 * 1024
    with pytest.raises(OSError):
        os.fstat(observed[0][0])
    assert target.read_bytes() == previous
    assert not list(tmp_path.glob('.research-export-*'))


def test_capture_cancelled_before_open(tmp_path, monkeypatch):
    target = tmp_path / 'export'
    target.write_bytes(b'Original')
    def forbidden(*args, **kw):
        pytest.fail('Cancelled capture must not open the destination')
    monkeypatch.setattr(e.os, 'open', forbidden)
    with pytest.raises(TimeoutError, match='cancelled'):
        e.capture_export_destination(target, private_root=tmp_path / 'private', cancelled=lambda: True)
    assert target.read_bytes() == b'Original'


def test_receipt_and_native_digest_shape_are_preserved(tmp_path):
    target = tmp_path / 'export'
    target.write_bytes(b'Original')
    private = tmp_path / 'private'
    approval = e.capture_export_destination(target, private_root=private)
    assert isinstance(approval.state[-1], bytes) and len(approval.state[-1]) == 32
    if os.name == 'nt':
        assert len(approval.state) == 8 and approval.state[-2] > 0
    receipt = e.write_export(target, 'Цитата\r\nOriginal', private_root=private, approval=approval)
    assert receipt.path == target and receipt.cleanup_warning is None
    assert target.read_bytes() == 'Цитата\r\nOriginal'.encode()


@pytest.mark.parametrize('binary', [False, True])
@pytest.mark.parametrize('mismatch', ['path', 'private_root'])
def test_mismatched_existing_approval_rejected_before_open(tmp_path, monkeypatch, binary, mismatch):
    first = tmp_path / 'first.txt'
    target = tmp_path / 'second.txt' if mismatch == 'path' else first
    first.write_bytes(b'Approved input')
    target.write_bytes(b'Independent work')
    private = tmp_path / 'private'
    approval = e.capture_export_destination(first, private_root=private)
    actual_private = tmp_path / 'other-private' if mismatch == 'private_root' else private
    opened, inspected = [], []
    real_open, real_destination = os.open, e._destination
    def inspect(path, private_root):
        inspected.append(path)
        return real_destination(path, private_root)
    monkeypatch.setattr(e, '_destination', inspect)
    def observed(path, *args, **kwargs):
        if Path(path) == target:
            opened.append(path.name)
        return real_open(path, *args, **kwargs)
    monkeypatch.setattr(e.os, 'open', observed)
    writer = e.write_binary_export if binary else e.write_export
    with pytest.raises(ValueError, match='destination changed'):
        writer(target, b'new' if binary else 'new', private_root=actual_private, approval=approval)
    assert target.read_bytes() == b'Independent work'
    assert not list(tmp_path.glob('.research-export-*'))
    assert not opened
    assert not inspected


@pytest.mark.parametrize('phase', ('before-write', 'after-staging'))
def test_replaced_existing_destination_not_opened(publication, monkeypatch, phase):
    target, private, publish = publication
    target.write_bytes(b'Approved')
    approval = e.capture_export_destination(target, private_root=private)
    replacement = target.with_name('independent')
    unrelated = b'New independent work of different length'
    replacement.write_bytes(unrelated)
    changed = False

    def replace_destination():
        nonlocal changed
        os.replace(replacement, target)
        changed = True

    if phase == 'before-write':
        replace_destination()
    else:
        fsync = os.fsync
        def competing_writer(fd):
            fsync(fd)
            replace_destination()
        monkeypatch.setattr(e.os, 'fsync', competing_writer)

    opened, native_queries = [], []
    real_open, real_native = os.open, e._windows_change_time
    def observe(path, *args, **kwargs):
        if changed and Path(path) == target:
            opened.append(path)
        return real_open(path, *args, **kwargs)
    def native_metadata(path):
        if changed and Path(path) == target:
            native_queries.append(path)
        return real_native(path)
    monkeypatch.setattr(e.os, 'open', observe)
    monkeypatch.setattr(e, '_windows_change_time', native_metadata)
    with pytest.raises(ValueError, match='destination changed'):
        publish(approval=approval)
    assert changed
    assert not opened and not native_queries
    assert target.read_bytes() == unrelated
    assert not list(target.parent.glob('.research-export-*'))


@pytest.mark.parametrize('phase', ('explicit', 'approved-absence', 'before-write', 'after-staging'))
def test_unapproved_windows_native_metadata_is_not_queried(publication, monkeypatch, phase):
    target, private, publish = publication
    windows_os = SimpleNamespace(**vars(os))
    windows_os.name = 'nt'
    monkeypatch.setattr(e, 'os', windows_os)
    changed = False
    native_queries = []

    def query(path):
        if changed:
            pytest.fail('A known unapproved destination must not open a native metadata handle.')
        native_queries.append(path)
        return 12345678

    monkeypatch.setattr(e, '_windows_change_time', query)
    approval = None
    if phase in ('before-write', 'after-staging'):
        target.write_bytes(b'Approved original')
        approval = e.capture_export_destination(target, private_root=private)
        assert native_queries
    elif phase == 'approved-absence':
        approval = e.capture_export_destination(target, private_root=private)
    replacement = target.with_name('unrelated')
    unrelated = b'Independent replacement of different length'
    replacement.write_bytes(unrelated)

    def replace_destination():
        nonlocal changed
        os.replace(replacement, target)
        changed = True

    if phase == 'after-staging':
        fsync = os.fsync

        def competing_writer(fd):
            fsync(fd)
            replace_destination()

        monkeypatch.setattr(e.os, 'fsync', competing_writer)
    else:
        replace_destination()

    if phase == 'explicit':
        with pytest.raises(FileExistsError):
            publish(require_absent=True)
    else:
        with pytest.raises(ValueError, match='destination changed'):
            publish(approval=approval)
    assert changed and target.read_bytes() == unrelated
    assert not list(target.parent.glob('.research-export-*'))

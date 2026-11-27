"""Export approval binds bytes even when filesystem timestamps do not change.

Real files, serializers and publication are used. Freezing only mtime/ctime in
the existing metadata projection reproduces coarse Windows file timestamps;
identity, mode, link count and size remain real. No digest or writer is mocked.
"""
from hashlib import sha256
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from carr_library.research import export_file


MIB = 1024 * 1024
ORIGINAL = b'Previously approved export: original bytes.\n'
COMPETING = b'Previously approved export: modified bytes.\n'
assert len(ORIGINAL) == len(COMPETING)


@pytest.fixture(params=['text', 'binary'])
def export_case(request, tmp_path):
    private = tmp_path / 'private-library'
    private.mkdir()
    target = tmp_path / 'selected-export'
    binary = request.param == 'binary'
    payload = b'\x00\x01New binary document\r\n\xff' if binary else 'New export — Україна.\r\n'
    writer = export_file.write_binary_export if binary else export_file.write_export

    def publish(**kwargs):
        return writer(target, payload, private_root=private, **kwargs)

    return SimpleNamespace(target=target, private=private, publish=publish,
                           expected=payload if binary else payload.encode('utf-8'))


def freeze_timestamps(monkeypatch, target):
    actual = export_file._file_state
    captured_times = actual(target.stat())[-2:]
    monkeypatch.setattr(export_file, '_file_state',
                        lambda info: (*actual(info)[:-2], *captured_times))


def test_equal_length_edit_after_approval_with_unchanged_metadata_is_rejected(export_case, monkeypatch):
    h = export_case
    h.target.write_bytes(ORIGINAL)
    freeze_timestamps(monkeypatch, h.target)
    metadata = export_file._file_state(h.target.stat())
    approval = export_file.capture_export_destination(h.target, private_root=h.private)
    h.target.write_bytes(COMPETING)
    assert export_file._file_state(h.target.stat()) == metadata

    with pytest.raises(ValueError, match='destination changed'):
        h.publish(approval=approval)

    assert h.target.read_bytes() == COMPETING
    assert not list(h.target.parent.glob('.research-export-*'))


@pytest.mark.parametrize('with_approval', [False, True], ids=['direct-writer', 'captured-approval'])
def test_equal_length_edit_after_real_staging_is_rejected(export_case, monkeypatch, with_approval):
    h = export_case
    h.target.write_bytes(ORIGINAL)
    freeze_timestamps(monkeypatch, h.target)
    metadata = export_file._file_state(h.target.stat())
    kwargs = ({'approval': export_file.capture_export_destination(h.target, private_root=h.private)}
              if with_approval else {})
    actual_fsync = export_file.os.fsync
    interleaved = []

    def change_after_staging(descriptor):
        actual_fsync(descriptor)
        staging = list(h.target.parent.glob('.research-export-*'))
        if staging and not interleaved:
            assert len(staging) == 1 and staging[0].read_bytes() == h.expected
            h.target.write_bytes(COMPETING)
            assert export_file._file_state(h.target.stat()) == metadata
            interleaved.append(True)

    monkeypatch.setattr(export_file.os, 'fsync', change_after_staging)
    with pytest.raises(ValueError, match='destination changed'):
        h.publish(**kwargs)

    assert interleaved == [True]
    assert h.target.read_bytes() == COMPETING
    assert not list(h.target.parent.glob('.research-export-*'))


@pytest.mark.parametrize('with_approval', [False, True], ids=['direct-writer', 'captured-approval'])
@pytest.mark.parametrize('previous', [b'', ORIGINAL], ids=['empty-file', 'nonempty-file'])
def test_unchanged_existing_destination_still_publishes_exact_bytes(export_case, with_approval, previous):
    h = export_case
    h.target.write_bytes(previous)
    kwargs = {}
    if with_approval:
        approval = export_file.capture_export_destination(h.target, private_root=h.private)
        assert approval.content_sha256 == sha256(previous).hexdigest()
        kwargs['approval'] = approval

    h.publish(**kwargs)

    assert h.target.read_bytes() == h.expected
    assert h.target.stat().st_nlink == 1
    assert not list(h.target.parent.glob('.research-export-*'))


def observe_target_reads(monkeypatch, target):
    """Observe actual file reads without substituting their contents or digest."""
    original_open = export_file.os.fdopen
    info = target.stat()
    identity = info.st_dev, info.st_ino
    reads = []

    class Reader:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            self.stream.__enter__()
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def fileno(self):
            return self.stream.fileno()

        def read(self, size=-1):
            assert 0 < size <= MIB, 'Destination inspection must use bounded one-MiB reads.'
            value = self.stream.read(size)
            reads.append((size, len(value)))
            return value

    def open_file(descriptor, mode='r', *args, **kwargs):
        stream = original_open(descriptor, mode, *args, **kwargs)
        opened = export_file.os.fstat(descriptor)
        return Reader(stream) if (opened.st_dev, opened.st_ino) == identity and mode == 'rb' else stream

    monkeypatch.setattr(export_file.os, 'fdopen', open_file)
    return reads


def test_destination_digest_reads_actual_content_in_bounded_blocks(tmp_path, monkeypatch):
    target = tmp_path / 'existing.bin'
    content = b'A' * (2 * MIB + 37)
    target.write_bytes(content)
    with monkeypatch.context() as patch:
        reads = observe_target_reads(patch, target)
        approval = export_file.capture_export_destination(target, private_root=tmp_path / 'private')
    assert len(reads) >= 3
    assert sum(actual for _, actual in reads) == len(content)
    assert approval.content_sha256 == sha256(content).hexdigest()
    assert target.read_bytes() == content


@pytest.mark.parametrize('operation', ['capture', 'write'])
def test_destination_hash_cancellation_stops_between_real_read_blocks(tmp_path, monkeypatch, operation):
    target = tmp_path / 'existing.bin'
    content = b'A' * (2 * MIB + 37)
    target.write_bytes(content)
    private = tmp_path / 'private'
    with monkeypatch.context() as patch:
        reads = observe_target_reads(patch, target)
        cancelled = lambda: bool(reads)
        with pytest.raises(TimeoutError, match='cancelled'):
            if operation == 'capture':
                export_file.capture_export_destination(target, private_root=private, cancelled=cancelled)
            else:
                export_file.write_export(target, 'Unpublished replacement', private_root=private,
                                         cancelled=cancelled)
    assert reads == [(MIB, MIB)]
    assert target.read_bytes() == content
    assert not list(tmp_path.glob('.research-export-*'))


@pytest.mark.parametrize('operation', ['capture', 'write'])
def test_oversized_existing_destination_is_rejected_before_payload_read(tmp_path, monkeypatch, operation):
    target = tmp_path / 'too-large.bin'
    with target.open('wb') as stream:
        stream.truncate(64 * MIB + 1)
    original_open = export_file.os.open

    def forbid_payload_read(path, *args, **kwargs):
        if Path(path) == target:
            pytest.fail('An oversized destination must be rejected before opening its payload.')
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(export_file.os, 'open', forbid_payload_read)
    with pytest.raises(ValueError, match='64 MiB|verification budget'):
        if operation == 'capture':
            export_file.capture_export_destination(target, private_root=tmp_path / 'private')
        else:
            export_file.write_binary_export(target, b'Unpublished replacement', private_root=tmp_path / 'private')
    assert target.stat().st_size == 64 * MIB + 1
    assert not list(tmp_path.glob('.research-export-*'))


@pytest.mark.parametrize('existing', [False, True])
def test_cancelled_capture_preserves_absent_or_existing_destination(tmp_path, existing):
    target = tmp_path / 'selected-export'
    if existing:
        target.write_bytes(ORIGINAL)
    with pytest.raises(TimeoutError, match='cancelled'):
        export_file.capture_export_destination(target, private_root=tmp_path / 'private',
                                                cancelled=lambda: True)
    assert (target.read_bytes() if target.exists() else None) == (ORIGINAL if existing else None)
    assert not list(tmp_path.glob('.research-export-*'))


@pytest.fixture(params=('text', 'binary'))
def budget_export_case(request, tmp_path):
    private = tmp_path / 'private'
    private.mkdir()
    target = tmp_path / 'selected-export'
    binary = request.param == 'binary'
    writer = export_file.write_binary_export if binary else export_file.write_export
    payload = b'New binary export\x00' if binary else 'Новий експорт'

    def publish(**options):
        return writer(target, payload, private_root=private, **options)

    return target, private, publish


@pytest.mark.parametrize('phase', ('approval', 'initial', 'final'))
def test_slow_destination_inspection_stops_after_real_read(budget_export_case, monkeypatch, phase):
    target, private, publish = budget_export_case
    previous = b'A' * (2 * 1024 * 1024 + 37)
    target.write_bytes(previous)
    approval = export_file.capture_export_destination(target, private_root=private)
    clock, delayed, descriptors = [0.0], [], []
    monkeypatch.setattr(export_file, 'monotonic', lambda: clock[0], raising=False)
    original = os.fdopen

    class Observed:
        def __init__(self, source):
            self.source = source

        def __enter__(self):
            self.source.__enter__()
            return self

        def __exit__(self, *args):
            return self.source.__exit__(*args)

        def read(self, count):
            block = self.source.read(count)
            if phase != 'final' or list(target.parent.glob('.research-export-*')):
                clock[0] += 2.0
                delayed.append(len(block))
            return block

    def observe(descriptor, *args, **kwargs):
        source = original(descriptor, *args, **kwargs)
        descriptors.append((descriptor, source))
        return Observed(source)

    monkeypatch.setattr(os, 'fdopen', observe)
    with pytest.raises(TimeoutError, match='could not be verified in time'):
        if phase == 'approval':
            export_file.capture_export_destination(target, private_root=private)
        else:
            publish(approval=approval)
    assert delayed == [1024 * 1024]
    for descriptor, source in descriptors:
        assert source.closed
        with pytest.raises(OSError):
            os.fstat(descriptor)
    assert target.read_bytes() == previous
    assert not list(target.parent.glob('.research-export-*'))


@pytest.mark.parametrize('phase', ('explicit', 'approved-absence', 'after-staging'))
def test_create_only_does_not_read_unapproved_competing_file(budget_export_case, monkeypatch, phase):
    target, private, publish = budget_export_case
    previous = b'Other writer owns this output'
    approval = export_file.capture_export_destination(target, private_root=private)
    if phase != 'after-staging':
        target.write_bytes(previous)
    else:
        fsync = os.fsync

        def create_after_staging(descriptor):
            fsync(descriptor)
            target.write_bytes(previous)

        monkeypatch.setattr(os, 'fsync', create_after_staging)
    opened = os.open
    reads = []

    def watch_open(path, *args, **kwargs):
        if Path(path) == target:
            reads.append(path)
        return opened(path, *args, **kwargs)

    monkeypatch.setattr(os, 'open', watch_open)
    if phase == 'explicit':
        with pytest.raises(FileExistsError):
            publish(require_absent=True)
    else:
        with pytest.raises(ValueError, match='destination changed'):
            publish(approval=approval)
    assert not reads
    assert target.read_bytes() == previous
    assert not list(target.parent.glob('.research-export-*'))

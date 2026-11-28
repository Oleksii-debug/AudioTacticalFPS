"""Real >64 MiB source admission, bounded buffers and retained-byte identity."""
from hashlib import sha256
from pathlib import Path
import os
import runpy
import shutil
import tracemalloc

import pytest

from carr_library.catalog.intake import (
    AssetSnapshot, FileSnapshot, IntakeError, MAX_BYTES, MAX_STREAM_BYTES,
    TextSegments, file_snapshot, source_snapshot, text_segments,
)
from carr_library.ingest.retained_assets import RetainedAssets

helpers = runpy.run_path(str(Path(__file__).parents[1] / 'ingest' / 'large_source_fixtures.py'))
write_large_text = helpers['write_large_text']
write_large_pdf = helpers['write_large_pdf']
file_digest = helpers['file_digest']
MIB = helpers['MIB']


@pytest.fixture(scope='module')
def large_sources(tmp_path_factory):
    root = tmp_path_factory.mktemp('real-large-inputs')
    return write_large_text(root / 'Київ.txt'), write_large_pdf(root / 'Evidence.pdf')


def test_large_txt_manifest_and_exact_segments_use_bounded_python_buffers(large_sources):
    original, _ = large_sources
    path = original['path']
    tracemalloc.start()
    try:
        source = source_snapshot(path.parent, path.name)
        assert isinstance(source, FileSnapshot) and not hasattr(source, 'data')
        assert source.manifest() == dict(sha256=original['sha256'], mime='text/plain', size=original['size'])
        count = 0
        for count, text in enumerate(text_segments(source), 1):
            offset, byte_size, digest = original['segments'][count - 1]
            assert sha256(text.encode('utf-8')).hexdigest() == digest
            assert text.startswith(f'Largeanchor{count - 1:03d} original line. Київ.\r\n')
            assert len(text.encode('utf-8')) == byte_size
        assert count == len(original['segments']) == 20
        assert original['meaningful_bytes'] > MAX_BYTES
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    # A whole 80 MiB read cannot pass this measured buffer ceiling.
    assert 0 < peak < 40 * MIB, {'peak_python_bytes': peak, 'source_bytes': original['size']}


def test_real_pdf_exactly_at_profile_limit_is_screened_without_materializing_it(large_sources):
    _, original = large_sources
    path = original['path']
    tracemalloc.start()
    try:
        source = source_snapshot(path.parent, path.name)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert original['size'] == MAX_STREAM_BYTES == 128 * MIB
    assert original['embedded_size'] > MAX_BYTES
    assert isinstance(source, FileSnapshot)
    assert source.manifest() == dict(sha256=original['sha256'], mime='application/pdf', size=MAX_STREAM_BYTES)
    assert peak < 8 * MIB
    assert file_digest(path) == source.digest


def test_small_snapshot_keeps_immutable_bytes_and_original_identity(tmp_path):
    data = 'Київ\r\nOriginal\fSecond'.encode()
    path = tmp_path / 'small.txt'
    path.write_bytes(data)
    source = source_snapshot(tmp_path, path.name)
    assert isinstance(source, AssetSnapshot)
    assert source.data == data and source.digest == sha256(data).hexdigest()


def test_streamed_utf8_crosses_block_boundary_without_newline_or_bom_rewriting(tmp_path):
    raw = b'\xef\xbb\xbf' + b' ' * (MIB - 4) + 'Ї'.encode() + b'\r\n\f' + '\ufeffNext'.encode()
    path = tmp_path / 'exact.txt'
    path.write_bytes(raw)
    source = file_snapshot(tmp_path, path.name)
    assert tuple(text_segments(source)) == (raw.decode('utf-8-sig').split('\f')[0], '\ufeffNext')
    indexed = TextSegments(source)
    assert len(indexed) == 2
    assert indexed[0].endswith('Ї\r\n') and indexed[1] == '\ufeffNext'


def test_cancel_during_streamed_hash_never_returns_an_admitted_manifest(large_sources):
    original, _ = large_sources
    calls = 0
    def cancelled():
        nonlocal calls
        calls += 1
        return calls >= 4
    with pytest.raises(InterruptedError, match='cancelled'):
        source_snapshot(original['path'].parent, original['path'].name, cancelled=cancelled)
    assert calls == 4
    assert file_digest(original['path']) == original['sha256']


def test_source_changed_after_genuine_final_hash_is_rejected(tmp_path, monkeypatch):
    from carr_library.catalog import intake
    path = tmp_path / 'race.txt'
    path.write_text('Original exact source.', encoding='utf-8')
    real_hash = intake._hash_stream
    def mutate_after_hash(stream, **options):
        result = real_hash(stream, **options)
        with path.open('r+b') as replacement:
            replacement.write(b'Changed!')
            replacement.flush()
            os.fsync(replacement.fileno())
        return result
    monkeypatch.setattr(intake, '_hash_stream', mutate_after_hash)
    with pytest.raises(IntakeError, match='changed'):
        file_snapshot(tmp_path, path.name)


def test_saved_file_identity_rejects_same_bytes_path_replacement(tmp_path):
    path = tmp_path / 'source.txt'
    path.write_text('Exact original text.', encoding='utf-8')
    source = file_snapshot(tmp_path, path.name)
    replacement = tmp_path / 'other.txt'
    replacement.write_bytes(path.read_bytes())
    os.replace(replacement, path)
    with pytest.raises(IntakeError, match='changed'):
        source.verify()


def test_streamed_retention_cancellation_cleans_only_its_private_stage(tmp_path, large_sources):
    original, _ = large_sources
    source = source_snapshot(original['path'].parent, original['path'].name)
    directory = tmp_path / 'originals'
    directory.mkdir()
    store = RetainedAssets(directory)
    calls = 0
    def cancelled():
        nonlocal calls
        calls += 1
        return calls >= 4
    with pytest.raises(InterruptedError, match='cancelled'):
        store.retain_snapshot(source, cancelled=cancelled)
    assert tuple(directory.iterdir()) == ()
    assert file_digest(original['path']) == original['sha256']


def test_no_replace_retention_and_late_external_drift_remain_explicit(tmp_path, large_sources):
    original, _ = large_sources
    source = source_snapshot(original['path'].parent, original['path'].name)
    directory = tmp_path / 'originals'
    directory.mkdir()
    store = RetainedAssets(directory)
    retained = store.retain_snapshot(source)
    before = retained.stat()
    assert store.retain_snapshot(source) == retained
    assert retained.stat().st_ino == before.st_ino and retained.stat().st_nlink == 1
    assert file_digest(retained) == original['sha256']
    with retained.open('r+b') as stream:
        stream.write(b'Corrupted!')
    with pytest.raises(ValueError, match='digest'):
        store.resolve(source.digest)


def test_profile_does_not_raise_other_format_or_absolute_maximum(tmp_path, large_sources):
    _, original = large_sources
    source = tmp_path / 'over.pdf'
    shutil.copyfile(original['path'], source)
    with source.open('ab') as stream:
        stream.write(b' ')
    with pytest.raises(IntakeError, match='128 MiB'):
        source_snapshot(tmp_path, source.name)
    source.unlink()
    # A real >64 MiB RTF still uses its original bounded in-memory profile.
    rtf = tmp_path / 'large.rtf'
    with rtf.open('xb') as stream:
        stream.write(b'{\\rtf1 ')
        block = b' ' * MIB
        for _ in range(65):
            stream.write(block)
        stream.write(b'}')
    with pytest.raises(IntakeError, match='too large'):
        source_snapshot(tmp_path, rtf.name)


def test_large_txt_requires_original_bounded_logical_segments(tmp_path):
    path = tmp_path / 'unsegmented.txt'
    with path.open('xb') as stream:
        stream.write(b'Original single logical segment.\n')
        for _ in range(65):
            stream.write(b' ' * MIB)
    source = source_snapshot(tmp_path, path.name)
    with pytest.raises(IntakeError, match='logical segment exceeds 4 MiB'):
        TextSegments(source)


def test_held_source_allows_another_reader_and_keeps_its_exact_identity(tmp_path):
    path = tmp_path / 'shared-read.txt'
    original = b'Original selected bytes.'
    path.write_bytes(original)
    source = file_snapshot(tmp_path, path.name)
    with source.open() as first, source.open() as second:
        assert first.read() == second.read() == original
        assert source.manifest() == dict(sha256=sha256(original).hexdigest(),
                                        mime='text/plain', size=len(original))
    source.verify()
    assert path.read_bytes() == original


def test_held_source_excludes_windows_writes_or_rejects_portable_drift(tmp_path):
    path = tmp_path / 'held-source.txt'
    original = b'Original selected bytes.'
    path.write_bytes(original)
    source = file_snapshot(tmp_path, path.name)
    if os.name == 'nt':
        with source.open() as reader:
            with pytest.raises(PermissionError):
                with path.open('r+b') as writer:
                    writer.write(b'Changed!')
            assert reader.read() == original
        assert path.read_bytes() == original
        # The held descriptor is closed: an explicitly later edit can proceed.
        with path.open('r+b') as writer:
            writer.write(b'Changed!')
            writer.flush()
            os.fsync(writer.fileno())
        with pytest.raises(IntakeError, match='changed'):
            source.verify()
    else:
        with pytest.raises(IntakeError, match='changed'):
            with source.open() as reader:
                assert reader.read() == original
                with path.open('r+b') as writer:
                    writer.write(b'Changed!')
                    writer.flush()
                    os.fsync(writer.fileno())
    assert path.read_bytes() != original


def test_open_writer_is_never_admitted_as_a_stable_windows_source(tmp_path):
    path = tmp_path / 'already-open.txt'
    original = b'Original selected bytes.'
    path.write_bytes(original)
    with path.open('r+b') as writer:
        if os.name == 'nt':
            with pytest.raises(IntakeError, match='stable read'):
                file_snapshot(tmp_path, path.name)
        else:
            # POSIX retains its existing detection contract; this control
            # performs no write and makes no claim of a mandatory POSIX lock.
            assert file_snapshot(tmp_path, path.name).digest == sha256(original).hexdigest()
    assert file_snapshot(tmp_path, path.name).digest == sha256(original).hexdigest()
    assert path.read_bytes() == original


@pytest.mark.parametrize('failure', [InterruptedError, TimeoutError])
def test_held_source_cleanup_preserves_the_original_cancellation(tmp_path, failure):
    path = tmp_path / 'cancel-held.txt'
    path.write_bytes(b'Original selected bytes.')
    source = file_snapshot(tmp_path, path.name)
    with pytest.raises(failure, match='explicit cancellation'):
        with source.open():
            raise failure('explicit cancellation')
    # No retained read lock may survive cancellation.
    with path.open('r+b') as writer:
        writer.write(b'Changed!')
        writer.flush()
        os.fsync(writer.fileno())
    with pytest.raises(IntakeError, match='changed'):
        source.verify()

"""Real >64 MiB source admission, bounded buffers and retained-byte identity."""
from hashlib import sha256
from pathlib import Path
import os
import runpy
import shutil
import tracemalloc

import pytest

from carr_library.catalog.intake import (
    AssetSnapshot, FileSnapshot, IntakeError, MAX_BYTES,
    TextSegments, file_snapshot, source_snapshot, text_segments,
)
from carr_library.ingest.retained_assets import RetainedAssets

helpers = runpy.run_path(str(Path(__file__).parents[1] / 'ingest' / 'large_source_fixtures.py'))
write_large_text = helpers['write_large_text']
write_large_pdf = helpers['write_large_pdf']
file_digest = helpers['file_digest']
MIB = helpers['MIB']
REVIEWED_BYTES = 128 * MIB


@pytest.fixture(scope='module')
def large_sources(tmp_path_factory):
    root = tmp_path_factory.mktemp('real-large-inputs')
    return write_large_text(root / 'Київ.txt'), write_large_pdf(root / 'Evidence.pdf')


def test_large_txt_manifest_and_exact_segments_use_bounded_python_buffers(large_sources):
    original, _ = large_sources
    path = original['path']
    tracemalloc.start()
    try:
        source = source_snapshot(path.parent, path.name, max_bytes=REVIEWED_BYTES)
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
        source = source_snapshot(path.parent, path.name, max_bytes=REVIEWED_BYTES)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert original['size'] == REVIEWED_BYTES == 128 * MIB
    assert original['embedded_size'] > MAX_BYTES
    assert isinstance(source, FileSnapshot)
    assert source.manifest() == dict(sha256=original['sha256'], mime='application/pdf', size=REVIEWED_BYTES)
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
        source_snapshot(original['path'].parent, original['path'].name, max_bytes=REVIEWED_BYTES, cancelled=cancelled)
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
    source = source_snapshot(original['path'].parent, original['path'].name, max_bytes=REVIEWED_BYTES)
    directory = tmp_path / 'originals'
    directory.mkdir()
    store = RetainedAssets(directory)
    calls = 0
    def cancelled():
        nonlocal calls
        calls += 1
        return calls >= 4
    with pytest.raises(InterruptedError, match='cancelled'):
        store.retain_snapshot(source, cancelled=cancelled, max_bytes=REVIEWED_BYTES)
    assert tuple(directory.iterdir()) == ()
    assert file_digest(original['path']) == original['sha256']


def test_no_replace_retention_and_late_external_drift_remain_explicit(tmp_path, large_sources):
    original, _ = large_sources
    source = source_snapshot(original['path'].parent, original['path'].name, max_bytes=REVIEWED_BYTES)
    directory = tmp_path / 'originals'
    directory.mkdir()
    store = RetainedAssets(directory)
    retained = store.retain_snapshot(source, max_bytes=REVIEWED_BYTES)
    before = retained.stat()
    assert store.retain_snapshot(source, max_bytes=REVIEWED_BYTES) == retained
    assert retained.stat().st_ino == before.st_ino and retained.stat().st_nlink == 1
    assert file_digest(retained) == original['sha256']
    with retained.open('r+b') as stream:
        stream.write(b'Corrupted!')
    with pytest.raises(ValueError, match='digest'):
        store.resolve(source.digest, max_bytes=REVIEWED_BYTES)


def test_profile_does_not_raise_other_format_or_absolute_maximum(tmp_path, large_sources):
    _, original = large_sources
    source = tmp_path / 'over.pdf'
    shutil.copyfile(original['path'], source)
    with source.open('ab') as stream:
        stream.write(b' ')
    with pytest.raises(IntakeError, match='allowance'):
        source_snapshot(tmp_path, source.name, max_bytes=REVIEWED_BYTES)
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
    source = source_snapshot(tmp_path, path.name, max_bytes=REVIEWED_BYTES)
    with pytest.raises(IntakeError, match='segment exceeds'):
        TextSegments(source, max_segment_bytes=4 * MIB)


def test_new_source_receipt_rejects_post_hash_bytes_with_unchanged_metadata(tmp_path, monkeypatch):
    from carr_library.catalog import intake
    original = b'Original exact source.'
    changed = b'Changed!' + original[8:]
    path = tmp_path / 'same-metadata.txt'
    path.write_bytes(original)
    frozen = intake._stamp(path.stat())
    # Model a filesystem retaining the same metadata while an existing reader
    # is open; both hashes and the in-place write remain genuine byte I/O.
    monkeypatch.setattr(intake, '_stamp', lambda info: frozen)
    genuine_hash = intake._hash_stream
    observed = []
    def change_after_first_hash(stream, **options):
        result = genuine_hash(stream, **options)
        observed.append(result)
        if len(observed) == 1:
            with path.open('r+b') as writer:
                writer.write(changed)
                writer.flush()
                os.fsync(writer.fileno())
        return result
    monkeypatch.setattr(intake, '_hash_stream', change_after_first_hash)
    with pytest.raises(IntakeError, match='changed'):
        file_snapshot(tmp_path, path.name)
    assert observed == [(sha256(original).hexdigest(), len(original)),
                        (sha256(changed).hexdigest(), len(changed))]
    assert path.read_bytes() == changed and len(changed) == len(original)


def test_legacy_default_still_requires_explicit_large_source_review(large_sources):
    for original in large_sources:
        with pytest.raises(IntakeError, match='allowance'):
            source_snapshot(original['path'].parent, original['path'].name)
        assert file_digest(original['path']) == original['sha256']


def test_each_descriptor_read_is_capped_at_captured_size_plus_one(tmp_path, monkeypatch):
    from carr_library.catalog import intake
    path = tmp_path / 'finite.txt'
    original = b'Original UTF-8 source.\r\n'
    path.write_bytes(original)
    real_open = intake._opened
    observed = []
    from contextlib import contextmanager

    @contextmanager
    def opened(*args, **kwargs):
        with real_open(*args, **kwargs) as stream:
            class Reads:
                def fileno(self):
                    return stream.fileno()
                def read(self, size=-1):
                    observed.append(size)
                    assert 0 < size <= len(original) + 1
                    return stream.read(size)
            yield Reads()

    monkeypatch.setattr(intake, '_opened', opened)
    source = file_snapshot(tmp_path, path.name)
    assert source.digest == sha256(original).hexdigest()
    assert observed and max(observed) == len(original) + 1


def test_equal_byte_preopen_replacement_is_rejected_before_any_payload_read(tmp_path, monkeypatch):
    from carr_library import durable_state
    path = tmp_path / 'owned.txt'
    path.write_bytes(b'Original exact bytes.')
    foreign = tmp_path / 'foreign.txt'
    foreign.write_bytes(path.read_bytes())
    real_open = durable_state._open_json_file
    touched = []
    def opening(selected):
        os.replace(foreign, selected)
        stream = real_open(selected)
        class Held:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                stream.close()
            def fileno(self):
                return stream.fileno()
            def read(self, *args):
                touched.append(True)
                raise AssertionError('A substituted source must fail before payload access.')
        return Held()
    monkeypatch.setattr(durable_state, '_open_json_file', opening)
    with pytest.raises(IntakeError, match='changed while opening'):
        file_snapshot(tmp_path, path.name)
    assert not touched
    assert path.read_bytes() == b'Original exact bytes.'

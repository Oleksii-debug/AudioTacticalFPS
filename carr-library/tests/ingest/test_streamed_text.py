from hashlib import sha256
import os
from pathlib import Path
import tracemalloc

import pytest

from carr_library.ingest.retained_assets import RetainedAssets
from carr_library.ingest.streamed_text import (
    FRAGMENT_CHARS, MAX_BYTES, TextProfile, fragments, inspect_text,
    profile_from_config, profile_from_value,
)


def source(tmp_path, raw=b'first\r\nsecond\fthird'):
    path = tmp_path / 'original.txt'
    path.write_bytes(raw)
    return path


def test_explicit_profile_roundtrip_preserves_the_old_adapter_contract():
    profile = TextProfile(70 * 1024**2)
    assert profile_from_value(profile.to_dict()) == profile
    assert profile_from_config(profile.configuration()) == profile
    assert profile_from_config({'adapter': 'strict-utf8-v1', 'kind': 'logical_segment'}) is None
    for changed in (dict(profile.configuration(), fragment_chars=3),
                    dict(profile.configuration(), surprise=True)):
        with pytest.raises(ValueError):
            profile_from_config(changed)


@pytest.mark.parametrize('value', [True, 0, -1, 1.2, MAX_BYTES + 1, '100'])
def test_profile_cannot_disable_or_inflate_the_byte_bound(value):
    with pytest.raises(ValueError):
        TextProfile(value)


def test_incremental_unicode_bom_crlf_and_formfeed_offsets(tmp_path):
    first = 'x' * (FRAGMENT_CHARS - 4) + '\r\nЄ😀z'
    path = source(tmp_path, b'\xef\xbb\xbf' + (first + '\f\fLast\r\n').encode('utf-8'))
    items = list(fragments(path, TextProfile()))
    assert [(item.field, item.offset) for item in items] == [(1, 0), (1, FRAGMENT_CHARS), (2, 0), (3, 0)]
    assert ''.join(item.text for item in items if item.field == 1) == first
    assert items[-1].text == 'Last\r\n' and items[-2].text == ''
    assert max(len(item.text) for item in items) <= FRAGMENT_CHARS
    snapshot = inspect_text(path, TextProfile())
    assert snapshot.digest == sha256(path.read_bytes()).hexdigest()
    assert snapshot.fragments[1].sha256 == sha256(items[1].text.encode()).hexdigest()


@pytest.mark.parametrize('ending, lengths', [('', [FRAGMENT_CHARS]),
    ('\f', [FRAGMENT_CHARS, 0]), ('\fnext', [FRAGMENT_CHARS, 4])])
def test_exact_fragment_boundary_does_not_invent_an_empty_field(tmp_path, ending, lengths):
    path = source(tmp_path, ('x' * FRAGMENT_CHARS + ending).encode())
    assert [len(item.text) for item in fragments(path, TextProfile())] == lengths


@pytest.mark.parametrize('raw', [b'', b'  \r\n\t', b'ok\x00bad', b'ok\x0b',
    b'invalid\xff', b'last\xf0\x9f', b'%PDF-1.4\ntext', b'{\\rtf1 text}', b'MZ' + b'\0' * 300],
    ids=['empty', 'whitespace', 'nul', 'vertical-tab', 'encoding', 'truncated-utf8', 'pdf', 'rtf', 'exe'])
def test_invalid_or_mislabelled_text_fails_without_a_partial_snapshot(tmp_path, raw):
    with pytest.raises((ValueError, UnicodeError)):
        inspect_text(source(tmp_path, raw), TextProfile())


def test_streamed_limit_and_fragment_count_are_checked_before_acceptance(tmp_path):
    path = source(tmp_path, b'valid text')
    with pytest.raises(ValueError, match='byte limit'):
        inspect_text(path, TextProfile(3))
    path.write_bytes(b'x\f' * 8192)
    with pytest.raises(ValueError, match='fragment limit'):
        inspect_text(path, TextProfile())


def test_entire_source_hash_detects_a_same_size_late_edit(tmp_path):
    path = source(tmp_path, b'quoted beginning\f' + b'x' * (FRAGMENT_CHARS * 2))
    before = inspect_text(path, TextProfile())
    stamp = path.stat()
    with path.open('r+b') as stream:
        stream.seek(-1, os.SEEK_END)
        stream.write(b'y')
    os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    with pytest.raises(ValueError, match='checksum'):
        list(fragments(path, TextProfile(), expected_digest=before.digest))


def test_cancel_during_incremental_decode_does_not_return_a_snapshot(tmp_path):
    path = source(tmp_path, b'x' * (FRAGMENT_CHARS * 4))
    calls = 0
    def cancelled():
        nonlocal calls
        calls += 1
        return calls > 5
    with pytest.raises(InterruptedError):
        inspect_text(path, TextProfile(), cancelled=cancelled)


def test_real_over_64_mib_source_has_bounded_python_memory_and_exact_identity(tmp_path):
    path = tmp_path / 'large.txt'
    block = ('Резонанс\r\n' + 'research source line\n' * 100).encode('utf-8')
    hashed, size = sha256(), 0
    with path.open('xb') as stream:
        while size <= 64 * 1024**2:
            stream.write(block)
            hashed.update(block)
            size += len(block)
    profile = TextProfile(65 * 1024**2)
    # Start after imports; this measures Python allocations of this actual
    # scan, not total process/native-family RSS or a fabricated file size.
    tracemalloc.start()
    try:
        result = inspect_text(path, profile)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert result.size == size > 64 * 1024**2
    assert result.digest == hashed.hexdigest()
    assert len(result.fragments) > 1000
    assert peak < 24 * 1024**2


def test_streamed_retention_uses_immutable_manifest_and_never_changes_original(tmp_path):
    path = source(tmp_path)
    snapshot = inspect_text(path, TextProfile())
    original = path.read_bytes()
    directory = tmp_path / 'assets'
    directory.mkdir()
    store = RetainedAssets(directory)
    target = store.retain_file(path, snapshot.mime, snapshot.digest, snapshot.size,
                              profile=snapshot.profile, expected_identity=snapshot.identity)
    assert path.read_bytes() == target.read_bytes() == original
    assert store.resolve(snapshot.digest, profile=snapshot.profile) == target
    replacement = tmp_path / 'replacement.txt'
    replacement.write_bytes(original)
    os.replace(replacement, path)
    with pytest.raises(ValueError, match='changed after validation'):
        store.retain_file(path, snapshot.mime, snapshot.digest, snapshot.size,
                          profile=snapshot.profile, expected_identity=snapshot.identity)


def test_retention_cancellation_and_hash_failure_publish_no_partial_original(tmp_path):
    path = source(tmp_path, b'x' * (2 * 1024**2))
    snapshot = inspect_text(path, TextProfile())
    directory = tmp_path / 'assets'
    directory.mkdir()
    store = RetainedAssets(directory)
    calls = 0
    def cancelled():
        nonlocal calls
        calls += 1
        return calls >= 3
    with pytest.raises(InterruptedError):
        store.retain_file(path, 'text/plain', snapshot.digest, snapshot.size,
                          profile=snapshot.profile, cancelled=cancelled)
    assert tuple(directory.iterdir()) == ()
    with pytest.raises(ValueError, match='checksum'):
        store.retain_file(path, 'text/plain', 'a' * 64, snapshot.size, profile=snapshot.profile)
    assert tuple(directory.iterdir()) == ()


def test_retention_retries_directory_sync_after_a_completed_move(tmp_path, monkeypatch):
    from carr_library.audio import jobs
    path = source(tmp_path)
    snapshot = inspect_text(path, TextProfile())
    directory = tmp_path / 'assets'
    directory.mkdir()
    store = RetainedAssets(directory)
    sync = jobs._sync_directory
    attempts = []
    def fail(selected):
        attempts.append(Path(selected))
        raise OSError('injected directory sync failure')
    monkeypatch.setattr(jobs, '_sync_directory', fail)
    for _ in range(2):
        with pytest.raises(OSError, match='injected'):
            store.retain_file(path, 'text/plain', snapshot.digest, snapshot.size, profile=snapshot.profile)
    assert len(attempts) == 2
    assert (directory / (snapshot.digest + '.txt')).read_bytes() == path.read_bytes()
    monkeypatch.setattr(jobs, '_sync_directory', sync)
    assert store.retain_file(path, 'text/plain', snapshot.digest, snapshot.size, profile=snapshot.profile).exists()


def test_hardlinked_source_is_not_admitted_by_streamed_intake(tmp_path):
    path = source(tmp_path)
    os.link(path, tmp_path / 'alias.txt')
    with pytest.raises(ValueError, match='unaliased'):
        inspect_text(path, TextProfile())

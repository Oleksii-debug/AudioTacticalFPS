"""Real file, crash and storage regressions; no speech-quality claims."""
import errno
from contextlib import contextmanager
import os
from pathlib import Path
import stat
import struct
import subprocess
import sys

import pytest

from carr_library.audio import jobs
from carr_library.audio.ffmpeg import concatenate, silence, wav_info


def test_export_is_an_independent_copy_of_immutable_cache(tmp_path):
    source, destination = tmp_path / 'source', tmp_path / 'export'
    source.write_bytes(b'original sealed cache')
    source.chmod(0o444)
    jobs.publish_new(source, destination)
    assert source.stat().st_nlink == destination.stat().st_nlink == 1
    destination.write_bytes(b'user edited export')
    assert source.read_bytes() == b'original sealed cache'
    source.chmod(0o600)


def test_publication_uses_writable_flush_descriptor(tmp_path, monkeypatch):
    source, destination = tmp_path / 'source', tmp_path / 'export'
    source.write_bytes(b'bytes')
    original = os.fsync
    flushed = []

    def strict_fsync(descriptor):
        info = os.fstat(descriptor)
        if stat.S_ISREG(info.st_mode):
            if os.name == 'posix':
                import fcntl
                mode = fcntl.fcntl(descriptor, fcntl.F_GETFL) & os.O_ACCMODE
                if mode == os.O_RDONLY:
                    raise OSError(errno.EBADF, 'Windows rejects this descriptor')
            flushed.append((info.st_dev, info.st_ino))
        original(descriptor)

    monkeypatch.setattr(os, 'fsync', strict_fsync)
    jobs.publish_new(source, destination)
    info = destination.stat()
    assert (info.st_dev, info.st_ino) in flushed


def test_flush_failure_does_not_expose_partial_output(tmp_path, monkeypatch):
    source, destination = tmp_path / 'source', tmp_path / 'export'
    source.write_bytes(b'preserve')

    def fail(descriptor):
        raise OSError(errno.EIO, 'Synthetic flush failure')

    monkeypatch.setattr(os, 'fsync', fail)
    with pytest.raises(OSError, match='flush failure'):
        jobs.publish_new(source, destination)
    assert not destination.exists()
    assert source.read_bytes() == b'preserve'
    assert not list(tmp_path.glob('.publish-*'))


@pytest.mark.parametrize('directory', [False, True])
def test_atomic_move_never_replaces_existing_target(tmp_path, directory):
    source, destination = tmp_path / 'stage', tmp_path / 'output'
    if directory:
        source.mkdir()
        (source / 'complete').write_bytes(b'new')
        destination.mkdir()  # Even an empty directory must not be replaced.
    else:
        source.write_bytes(b'new')
        destination.write_bytes(b'keep')
    before = destination.stat()
    with pytest.raises(FileExistsError):
        jobs.publish_noreplace(source, destination)
    assert source.exists()
    assert destination.stat().st_ino == before.st_ino
    if not directory:
        assert destination.read_bytes() == b'keep'


def test_process_death_after_rename_leaves_no_alias(tmp_path):
    source, destination = tmp_path / 'сцена-stage.wav', tmp_path / 'готово.wav'
    source.write_bytes(b'complete data')
    code = """
import os, sys
from carr_library.audio import jobs
jobs._sync_directory = lambda path: os._exit(17)
jobs.publish_noreplace(sys.argv[1], sys.argv[2])
"""
    environment = dict(os.environ, PYTHONPATH=str(Path(jobs.__file__).parents[2]))
    result = subprocess.run([sys.executable, '-c', code, str(source), str(destination)],
                            env=environment, timeout=20, capture_output=True)
    assert result.returncode == 17, result.stderr.decode(errors='replace')
    assert not source.exists()
    assert destination.read_bytes() == b'complete data'
    assert destination.stat().st_nlink == 1


def test_cancelled_copy_retains_source_and_no_output(tmp_path):
    source, destination = tmp_path / 'source', tmp_path / 'export'
    source.write_bytes(b'a' * (2 * 1024 ** 2))
    with pytest.raises(jobs.RenderCancelled):
        jobs.publish_new(source, destination, cancelled=lambda: True)
    assert source.stat().st_size == 2 * 1024 ** 2
    assert not destination.exists()
    assert not list(tmp_path.glob('.publish-*'))


def test_checksum_rejects_same_size_mutation_during_read(tmp_path):
    source = tmp_path / 'input'
    source.write_bytes(b'old input')
    changed = False

    def mutate():
        nonlocal changed
        if not changed:
            changed = True
            source.write_bytes(b'new input')
        return False

    if os.name == 'nt':
        # A payload's retained native read handle prevents this write outright;
        # timestamp comparisons alone can miss rapid same-size NTFS writes.
        with pytest.raises(PermissionError) as error:
            jobs.sha256_file(source, cancelled=mutate)
        assert error.value.winerror == 32
        assert changed and source.read_bytes() == b'old input'
        source.write_bytes(b'new input')  # The exceptional exit released it.
    else:
        with pytest.raises(RuntimeError, match='changed'):
            jobs.sha256_file(source, cancelled=mutate)


def test_audio_payload_reader_keeps_content_available_to_other_readers(tmp_path):
    source = tmp_path / 'input'
    source.write_bytes(b'complete immutable input')
    with jobs.verified_reader(source) as first:
        with jobs.verified_reader(source) as second:
            assert first.read() == second.read() == b'complete immutable input'
        assert source.read_bytes() == b'complete immutable input'
    source.write_bytes(b'after the read interval')
    assert source.read_bytes() == b'after the read interval'


def test_audio_payload_read_interval_rejects_or_blocks_a_real_child_write(tmp_path):
    source = tmp_path / 'input'
    source.write_bytes(b'old input')
    command = [sys.executable, '-I', '-c',
               'from pathlib import Path; import sys\n'
               'try: Path(sys.argv[1]).write_bytes(b"new input")\n'
               'except PermissionError as error: print("blocked", error.winerror)\n', str(source)]

    def read_while_child_writes():
        with jobs.verified_reader(source) as stream:
            child = subprocess.run(command, capture_output=True, text=True, timeout=10, check=True)
            if os.name == 'nt':
                assert child.stdout.strip() == 'blocked 32'
                assert stream.read() == b'old input'
            else:
                assert child.stdout == '' and source.read_bytes() == b'new input'
    if os.name == 'nt':
        read_while_child_writes()
    else:
        with pytest.raises(RuntimeError, match='changed'):
            read_while_child_writes()
    source.write_bytes(b'released after validation')


def test_streamed_concat_binds_inputs_to_preflight_identity(tmp_path):
    source = silence(tmp_path / 'input.wav', 800, sample_rate=8000)
    info = wav_info(source)

    def mutate(message):
        with source.open('r+b') as stream:
            stream.seek(info.data_offset)
            stream.write(b'\x01\x00' * 1600)

    with pytest.raises(RuntimeError, match='changed'):
        concatenate([source], tmp_path / 'output.wav', on_status=mutate)
    assert not (tmp_path / 'output.wav').exists()


def test_companions_and_metadata_are_reserved_before_seal(tmp_path):
    source = silence(tmp_path / 'chunk.wav', 80, sample_rate=8000)
    source.with_suffix('.mid').write_bytes(b'm' * 4096)
    cache = jobs.ChunkCache(tmp_path / 'cache', disk_budget_bytes=1024)
    with pytest.raises(RuntimeError, match='disk budget'):
        cache.seal('a' * 64, source, companions=('.mid',))
    assert not list(cache.root.iterdir())


def test_cache_usage_includes_nested_quarantine(tmp_path):
    quarantine = tmp_path / 'cache' / 'quarantine'
    quarantine.mkdir(parents=True)
    (quarantine / 'invalid.wav').write_bytes(b'q' * 4096)
    cache = jobs.ChunkCache(quarantine.parent, disk_budget_bytes=1024)
    with pytest.raises(RuntimeError, match='disk budget'):
        cache.reserve(1)


def test_cache_usage_includes_complete_metadata_and_companions(tmp_path):
    source = silence(tmp_path / 'chunk.wav', 80, sample_rate=8000)
    source.with_suffix('.mid').write_bytes(b'm' * 512)
    cache = jobs.ChunkCache(tmp_path / 'cache', disk_budget_bytes=1024 ** 2)
    result, metadata = cache.seal('a' * 64, source, companions=('.mid',))
    assert cache.used == sum(path.stat().st_size for path in cache.root.iterdir())
    assert cache.used > result.stat().st_size + 512
    assert cache.get('a' * 64)[1] == metadata


def test_project_budget_counts_existing_renders_and_music_jobs(tmp_path):
    for name in ('renders', 'music-jobs'):
        directory = tmp_path / name / 'old'
        directory.mkdir(parents=True)
        (directory / 'retained.wav').write_bytes(b'x' * 700)
    cache = jobs.ChunkCache(tmp_path / 'cache', budget_root=tmp_path, disk_budget_bytes=1024)
    with pytest.raises(RuntimeError, match='disk budget'):
        cache.reserve(1)


def test_windows_mapped_network_drive_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs.sys, 'platform', 'win32')
    monkeypatch.setattr(jobs, '_windows_drive_type', lambda anchor: 4)
    with pytest.raises(ValueError, match='mapped network'):
        jobs.checked_directory(tmp_path)


def test_nonobject_cache_marker_is_quarantined(tmp_path):
    cache = jobs.ChunkCache(tmp_path / 'cache')
    (cache.root / ('a' * 64 + '.json')).write_text('[]', encoding='utf-8')
    assert cache.get('a' * 64) is None
    assert len(list((cache.root / 'quarantine').iterdir())) == 1


def test_inconsistent_rf64_sample_count_is_rejected(tmp_path):
    path = tmp_path / 'invalid-rf64.wav'
    fmt = struct.pack('<HHIIHH', 1, 2, 8000, 32000, 4, 16)
    path.write_bytes(b'RF64\xff\xff\xff\xffWAVEds64' +
        struct.pack('<IQQQI', 28, 80, 8, 999, 0) +
        b'fmt ' + struct.pack('<I', 16) + fmt + b'data\xff\xff\xff\xff' + b'\0' * 8)
    with pytest.raises(ValueError, match='RF64 frame count'):
        wav_info(path)


@pytest.mark.parametrize('chunk', ['data', 'fmt'])
def test_duplicate_pcm_chunks_cannot_change_ffmpeg_duration(tmp_path, chunk):
    fmt = b'fmt ' + struct.pack('<IHHIIHH', 16, 1, 1, 8000, 16000, 2, 16)
    data = b'data' + struct.pack('<I', 1600) + b'\0' * 1600
    body = fmt + data
    body += (b'data' + struct.pack('<I', 16000) + b'\0' * 16000) if chunk == 'data' else fmt
    path = tmp_path / 'duplicate.wav'
    path.write_bytes(b'RIFF' + struct.pack('<I', len(body) + 4) + b'WAVE' + body)
    with pytest.raises(ValueError, match='Duplicate'):
        wav_info(path)


def test_riff_size_must_describe_complete_container(tmp_path):
    path = silence(tmp_path / 'wrong-size.wav', 80, sample_rate=8000)
    with path.open('r+b') as stream:
        stream.seek(4)
        stream.write(struct.pack('<I', 36))
    with pytest.raises(ValueError, match='size declaration'):
        wav_info(path)


def test_json_read_stays_bounded_when_file_grows_after_stat(tmp_path, monkeypatch):
    path = tmp_path / 'checkpoint.json'
    path.write_bytes(b'{}')
    original = jobs._open_json_file
    observed = []

    @contextmanager
    def growing_reader(path, **kwargs):
        with original(path, **kwargs) as stream:
            class Reader:
                def fileno(self):
                    return stream.fileno()

                def read(self, count):
                    observed.append(count)
                    with Path(path).open('ab') as writer:
                        writer.write(b' ' * (1024 ** 2))
                    return stream.read(count)
            yield Reader()

    monkeypatch.setattr(jobs, '_open_json_file', growing_reader)
    with pytest.raises(ValueError, match='too large'):
        jobs.read_json(path, limit=128)
    assert observed == [129]


@pytest.mark.parametrize('content', ['{"key":1,"key":2}', '{"key":NaN}', '{"key":Infinity}'])
def test_ambiguous_json_is_rejected(tmp_path, content):
    path = tmp_path / 'checkpoint.json'
    path.write_text(content, encoding='utf-8')
    with pytest.raises(ValueError):
        jobs.read_json(path)

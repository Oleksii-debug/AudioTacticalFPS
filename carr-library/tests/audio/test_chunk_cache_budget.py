"""Retained corruption and complete sealed payloads obey the cache budget."""
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import wave

import pytest

from carr_library.audio import jobs
from carr_library.audio.jobs import ChunkCache, RenderCancelled


@pytest.mark.parametrize('marker', ['[]', 'null', '"scalar"', '42', 'true'])
def test_nonobject_checkpoint_is_quarantined_with_all_retained_bytes(tmp_path, marker):
    staged = source(tmp_path / 'staging', companions=True)
    cache = ChunkCache(tmp_path / 'cache')
    cache.seal(key(), staged, companions=('.mid', '.music.json'))
    checkpoint = cache.root / (key() + '.json')
    checkpoint.write_text(marker, encoding='utf-8')
    before = contents(cache.root)
    retained = sizes(cache.root)

    assert cache.get(key()) is None

    quarantined = contents(cache.root / 'quarantine')
    assert sorted(quarantined.values()) == sorted(before.values())
    assert len(quarantined) == 4
    assert not checkpoint.exists()
    assert cache.used == sizes(cache.root) == retained
    reopened = ChunkCache(cache.root, disk_budget_bytes=retained)
    assert reopened.used == retained
    with pytest.raises(RuntimeError, match='disk budget'):
        reopened.reserve(1)


def test_export_is_an_independent_copy_of_immutable_cache(tmp_path):
    source_path, destination = tmp_path / 'source', tmp_path / 'export'
    source_path.write_bytes(b'original sealed cache')
    source_path.chmod(0o444)
    try:
        jobs.publish_new(source_path, destination)
        assert source_path.stat().st_nlink == destination.stat().st_nlink == 1
        destination.write_bytes(b'user edited export')
        assert source_path.read_bytes() == b'original sealed cache'
    finally:
        source_path.chmod(0o600)


@pytest.mark.parametrize('race', [False, True])
def test_independent_publication_never_replaces_competing_output(tmp_path, monkeypatch, race):
    original, destination = tmp_path / 'source', tmp_path / 'export'
    original.write_bytes(b'complete new bytes')
    competing = b'Another program owns this destination'
    if race:
        publish = jobs.publish_noreplace

        def create_then_publish(source_path, output):
            output.write_bytes(competing)
            return publish(source_path, output)

        monkeypatch.setattr(jobs, 'publish_noreplace', create_then_publish)
    else:
        destination.write_bytes(competing)
    with pytest.raises(FileExistsError):
        jobs.publish_new(original, destination)
    assert destination.read_bytes() == competing
    assert original.read_bytes() == b'complete new bytes'
    assert destination.stat().st_nlink == original.stat().st_nlink == 1
    assert not list(tmp_path.glob('.publish-*'))


def test_cancelled_copy_retains_source_and_removes_partial_stage(tmp_path):
    original, destination = tmp_path / 'source', tmp_path / 'export'
    original.write_bytes(b'a' * (2 * 1024 ** 2))
    copied = []

    def cancelled():
        copied.extend(path.stat().st_size for path in tmp_path.glob('.publish-*'))
        return any(size >= 1024 ** 2 for size in copied)

    with pytest.raises(RenderCancelled):
        jobs.publish_new(original, destination, cancelled=cancelled)
    assert any(size >= 1024 ** 2 for size in copied)
    assert original.read_bytes() == b'a' * (2 * 1024 ** 2)
    assert not destination.exists()
    assert not list(tmp_path.glob('.publish-*'))


def test_copy_flush_failure_never_exposes_partial_output(tmp_path, monkeypatch):
    original, destination = tmp_path / 'source', tmp_path / 'export'
    original.write_bytes(b'preserve the source')

    def fail(_descriptor):
        raise OSError('Synthetic flush failure')

    monkeypatch.setattr(os, 'fsync', fail)
    with pytest.raises(OSError, match='flush failure'):
        jobs.publish_new(original, destination)
    assert original.read_bytes() == b'preserve the source'
    assert not destination.exists()
    assert not list(tmp_path.glob('.publish-*'))


def test_process_death_after_publication_leaves_no_source_alias(tmp_path):
    original, destination = tmp_path / 'сцена.wav', tmp_path / 'готово.wav'
    original.write_bytes(b'complete data')
    code = """
import os, sys
from carr_library.audio import jobs
jobs._sync_directory = lambda path: os._exit(17)
jobs.publish_new(sys.argv[1], sys.argv[2])
"""
    environment = dict(os.environ, PYTHONPATH=str(Path(jobs.__file__).parents[2]))
    result = subprocess.run([sys.executable, '-B', '-c', code, str(original), str(destination)],
                            env=environment, timeout=20, capture_output=True)
    assert result.returncode == 17, result.stderr.decode(errors='replace')
    assert destination.read_bytes() == b'complete data'
    assert original.stat().st_nlink == destination.stat().st_nlink == 1
    destination.write_bytes(b'edited after crash')
    assert original.read_bytes() == b'complete data'
    assert not list(tmp_path.glob('.publish-*'))


def test_project_budget_includes_retained_renders_and_music_jobs(tmp_path):
    for name in ('renders', 'music-jobs'):
        directory = tmp_path / name / 'old'
        directory.mkdir(parents=True)
        (directory / 'retained.wav').write_bytes(b'x' * 700)
    cache = ChunkCache(tmp_path / 'cache', budget_root=tmp_path, disk_budget_bytes=1024)
    with pytest.raises(RuntimeError, match='disk budget'):
        cache.reserve(1)
    assert cache.used == 1400


def test_copy_and_original_stage_share_one_project_budget_before_seal(tmp_path):
    staged = source(tmp_path / 'staging', frames=1600)
    retained = staged.stat().st_size
    cache = ChunkCache(tmp_path / 'cache', budget_root=tmp_path, disk_budget_bytes=retained + 1)
    with pytest.raises(RuntimeError, match='disk budget'):
        cache.seal(key(), staged)
    assert staged.stat().st_size == retained
    assert not list(cache.root.iterdir())


def source(directory, *, name='incoming', frames=1600, companions=False):
    directory.mkdir(parents=True, exist_ok=True)
    audio = directory / (name + '.wav')
    with wave.open(str(audio), 'wb') as stream:
        stream.setparams((1, 2, 8000, frames, 'NONE', 'not compressed'))
        stream.writeframes(b'\x01\x00' * frames)
    if companions:
        audio.with_suffix('.mid').write_bytes(b'Synthetic MIDI companion\n' * 20)
        audio.with_suffix('.music.json').write_text('{"fixture":"Synthetic original score"}', encoding='utf-8')
    return audio


def key(value='fixture'):
    return hashlib.sha256(value.encode()).hexdigest()


def sizes(root):
    return sum(path.lstat().st_size for path in root.rglob('*') if path.is_file())


def contents(root):
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob('*') if path.is_file()}


def unlink_staging(audio, companions=False):
    audio.unlink()
    if companions:
        audio.with_suffix('.mid').unlink()
        audio.with_suffix('.music.json').unlink()


def corrupt(path):
    with path.open('r+b') as stream:
        stream.seek(-1, 2)
        original = stream.read(1)
        stream.seek(-1, 2)
        stream.write(bytes((original[0] ^ 1,)))


def test_restart_never_releases_quarantined_payload_budget(tmp_path):
    """Exact reproduction: the fourth 320044-byte WAV used to exceed 1 MiB."""
    root, allowance = tmp_path / 'cache', 1024**2
    identity = key()
    for number in range(3):
        cache = ChunkCache(root, disk_budget_bytes=allowance)
        assert cache.get(identity) is None
        staged = source(tmp_path / 'staging', name=f'piece-{number}', frames=160000)
        accepted, _ = cache.seal(identity, staged)
        unlink_staging(staged)
        assert sizes(root) <= allowance
        corrupt(accepted)
    cache = ChunkCache(root, disk_budget_bytes=allowance)
    assert cache.get(identity) is None
    before = contents(root)
    assert len(before) == 6  # Three corrupt WAVs and their original checkpoints.
    staged = source(tmp_path / 'staging', name='fourth-piece', frames=160000)
    with pytest.raises(RuntimeError, match='disk budget'):
        cache.seal(identity, staged)
    assert contents(root) == before
    assert cache.used == sizes(root)
    assert sizes(root) <= allowance
    assert staged.is_file()
    assert not (root / (identity + '.wav')).exists()
    assert not (root / (identity + '.json')).exists()


def test_metadata_and_both_companions_have_an_exact_prepublication_budget(tmp_path):
    staged = source(tmp_path / 'staging', companions=True)
    provenance = {'fixture': 'Незмінна синтетична перевірка'}
    reference = ChunkCache(tmp_path / 'reference')
    reference.seal(key(), staged, provenance=provenance, companions=('.mid', '.music.json'))
    required = sizes(reference.root)
    assert reference.used == required

    too_small = ChunkCache(tmp_path / 'too-small', disk_budget_bytes=required - 1)
    with pytest.raises(RuntimeError, match='disk budget'):
        too_small.seal(key(), staged, provenance=provenance, companions=('.mid', '.music.json'))
    assert list(too_small.root.iterdir()) == []
    exact = ChunkCache(tmp_path / 'exact', disk_budget_bytes=required)
    exact.seal(key(), staged, provenance=provenance, companions=('.mid', '.music.json'))
    assert exact.used == sizes(exact.root) == required
    reopened = ChunkCache(exact.root, disk_budget_bytes=required)
    assert reopened.used == required
    assert reopened.get(key()) is not None
    with pytest.raises(RuntimeError, match='disk budget'):
        reopened.reserve(1)


def test_large_staged_companion_fails_before_any_cache_publication(tmp_path):
    staged = source(tmp_path / 'staging', frames=32)
    staged.with_suffix('.mid').write_bytes(b'fixture' * 1024)
    cache = ChunkCache(tmp_path / 'cache', disk_budget_bytes=4096)
    before = contents(staged.parent)
    with pytest.raises(RuntimeError, match='disk budget'):
        cache.seal(key(), staged, companions=('.mid',))
    assert list(cache.root.iterdir()) == []
    assert contents(staged.parent) == before


def test_corrupt_companion_remains_accounted_and_valid_replacement_can_resume(tmp_path):
    staged = source(tmp_path / 'first', companions=True)
    cache = ChunkCache(tmp_path / 'cache')
    accepted, metadata = cache.seal(key(), staged, companions=('.mid', '.music.json'))
    unlink_staging(staged, companions=True)
    before_size = sizes(cache.root)
    corrupt(accepted.with_suffix('.mid'))
    assert cache.get(key()) is None
    quarantine = contents(cache.root / 'quarantine')
    assert len(quarantine) == 4
    assert cache.used == sizes(cache.root) == before_size
    replacement = source(tmp_path / 'replacement', companions=True)
    resumed = ChunkCache(cache.root, disk_budget_bytes=2 * before_size)
    restored, current = resumed.seal(key(), replacement, companions=('.mid', '.music.json'))
    assert current == metadata
    assert resumed.used == sizes(cache.root) == 2 * before_size
    assert resumed.get(key())[0] == restored
    assert contents(cache.root / 'quarantine') == quarantine
    assert ChunkCache(cache.root).used == 2 * before_size


def test_cancellation_between_companion_publications_cleans_only_new_links(tmp_path, monkeypatch):
    staged = source(tmp_path / 'staging', companions=True)
    stopped = [False]
    cache = ChunkCache(tmp_path / 'cache', cancelled=lambda: stopped[0])
    publish = jobs.publish_new

    def first_then_cancel(source_path, destination, **options):
        publish(source_path, destination, **options)
        stopped[0] = True

    monkeypatch.setattr(jobs, 'publish_new', first_then_cancel)
    before = contents(staged.parent)
    with pytest.raises(RenderCancelled):
        cache.seal(key(), staged, companions=('.mid', '.music.json'))
    assert list(cache.root.iterdir()) == []
    assert cache.used == 0
    assert contents(staged.parent) == before
    monkeypatch.setattr(jobs, 'publish_new', publish)
    resumed = ChunkCache(cache.root)
    resumed.seal(key(), staged, companions=('.mid', '.music.json'))
    assert resumed.get(key()) is not None


def test_failed_companion_publication_does_not_charge_or_leave_partial_payload(tmp_path, monkeypatch):
    staged = source(tmp_path / 'staging', companions=True)
    cache = ChunkCache(tmp_path / 'cache')
    publish = jobs.publish_new
    calls = [0]

    def fail_second(source_path, destination, **options):
        calls[0] += 1
        if calls[0] == 2:
            raise OSError('Synthetic publication failure')
        return publish(source_path, destination, **options)

    monkeypatch.setattr(jobs, 'publish_new', fail_second)
    with pytest.raises(OSError, match='publication failure'):
        cache.seal(key(), staged, companions=('.mid', '.music.json'))
    assert list(cache.root.iterdir()) == []
    assert cache.used == 0


def test_failed_checkpoint_durability_still_accounts_retained_metadata(tmp_path, monkeypatch):
    staged = source(tmp_path / 'staging', companions=True)
    cache = ChunkCache(tmp_path / 'cache')
    write_json = jobs.atomic_json

    def write_then_fail(path, value):
        write_json(path, value)
        raise OSError('Synthetic checkpoint durability failure')

    monkeypatch.setattr(jobs, 'atomic_json', write_then_fail)
    with pytest.raises(OSError, match='durability failure'):
        cache.seal(key(), staged, companions=('.mid', '.music.json'))
    assert [p.name for p in cache.root.iterdir()] == [key() + '.json']
    assert cache.used == sizes(cache.root) > 0
    monkeypatch.setattr(jobs, 'atomic_json', write_json)
    assert cache.get(key()) is None
    preserved = contents(cache.root / 'quarantine')
    assert len(preserved) == 1
    cache.seal(key(), staged, companions=('.mid', '.music.json'))
    assert cache.get(key()) is not None
    assert contents(cache.root / 'quarantine') == preserved
    assert cache.used == sizes(cache.root)


@pytest.mark.parametrize('provenance', [{'fixture': 'x' * 65536}, {'invalid': float('nan')}])
def test_unreadable_metadata_is_rejected_before_publishing_any_payload(tmp_path, provenance):
    staged = source(tmp_path / 'staging', companions=True)
    cache = ChunkCache(tmp_path / 'cache')
    with pytest.raises(ValueError):
        cache.seal(key(), staged, provenance=provenance, companions=('.mid', '.music.json'))
    assert list(cache.root.iterdir()) == []


@pytest.mark.parametrize('allowance', [True, -1, 1.0, float('nan'), float('inf')])
def test_invalid_budget_is_rejected_before_creating_cache(tmp_path, allowance):
    root = tmp_path / 'not-created'
    with pytest.raises(ValueError, match='disk budget'):
        ChunkCache(root, disk_budget_bytes=allowance)
    assert not root.exists()


def test_growing_corruption_is_counted_before_regeneration(tmp_path):
    staged = source(tmp_path / 'staging', frames=128)
    cache = ChunkCache(tmp_path / 'cache', disk_budget_bytes=4096)
    accepted, _ = cache.seal(key(), staged)
    unlink_staging(staged)
    with accepted.open('ab') as stream:
        stream.write(b'corrupt-extra' * 500)
    assert cache.get(key()) is None
    assert cache.used == sizes(cache.root) > 4096
    preserved = contents(cache.root)
    with pytest.raises(RuntimeError, match='disk budget'):
        cache.reserve(1)
    assert contents(cache.root) == preserved

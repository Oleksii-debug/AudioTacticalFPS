"""Actual FFmpeg M4B metadata, source preservation and publication boundaries."""
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import shutil
import struct
import wave

import pytest

from carr_library.audio import AudioBudget, AudioRuntime, AudioCancelled, AudioError
from carr_library.audio.audiobook import inspect_chapters, line_lrc, metadata_bytes
from carr_library.audio.process import run_process


@pytest.fixture
def actual(tmp_path):
    assert shutil.which('ffmpeg') and shutil.which('ffprobe'), 'Actual FFmpeg is required.'
    source = tmp_path / 'original.wav'
    with wave.open(str(source), 'wb') as stream:
        stream.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
        stream.writeframes(b''.join(struct.pack('<h', (index % 100 - 50) * 100) for index in range(48000)))
    root = tmp_path / 'exports'; root.mkdir()
    runtime = AudioRuntime(root, input_roots=(tmp_path,), budget=AudioBudget(reserve_disk_bytes=0))
    chapters = [{'title': 'Початок #1; = \\ \n[CHAPTER]\n😀', 'start': 0, 'end': .75},
                {'title': 'Second [CHAPTER]', 'start': .75, 'end': 2}]
    return runtime, source, root, chapters


def test_actual_m4b_contains_exact_titles_measured_chapters_and_audio(actual):
    runtime, source, root, chapters = actual
    before = source.read_bytes()
    target = root / 'measured.m4b'
    info = runtime.audiobook(source, target, title='Review #1; = \\ \n😀', chapters=chapters)
    result = inspect_chapters(runtime, target)
    assert result['format']['tags']['title'] == 'Review #1; = \\ \n😀'
    assert [row['tags']['title'] for row in result['chapters']] == [row['title'] for row in chapters]
    assert [float(row['start_time']) for row in result['chapters']] == [0, .75]
    assert [float(row['end_time']) for row in result['chapters']] == [.75, 2]
    assert info.channels == 2 and info.sample_rate == 24000 and abs(info.duration - 2) < .01
    assert info.sha256 == sha256(target.read_bytes()).hexdigest()
    decoded = root / 'decoded.wav'
    runtime.convert(target, decoded, channels=1)
    with wave.open(str(decoded), 'rb') as stream:
        assert stream.getnframes() >= 48000 - 240
        assert any(stream.readframes(stream.getnframes()))
    assert source.read_bytes() == before
    assert not list(root.glob('.audiobook-*'))


@pytest.mark.parametrize('changes', [
    [{'title': 'Gap', 'start': .1, 'end': 2}],
    [{'title': 'Short', 'start': 0, 'end': 1.9}],
    [{'title': 'Long', 'start': 0, 'end': 2.1}],
    [{'title': 'Reverse', 'start': 1, 'end': 0}],
    [{'title': 'NaN', 'start': 0, 'end': float('nan')}],
    [{'title': 'First', 'start': 0, 'end': 1}, {'title': 'Overlap', 'start': .5, 'end': 2}],
    [{'title': 'Unexpected', 'start': 0, 'end': 2, 'url': 'https://invalid.example'}],
])
def test_invalid_chapter_plan_never_starts_encoder_or_publishes(actual, changes, monkeypatch):
    runtime, source, root, _ = actual
    monkeypatch.setattr(runtime, 'convert', lambda *a, **k: pytest.fail('Invalid plan reached the encoder'))
    target = root / 'invalid.m4b'
    with pytest.raises(ValueError):
        runtime.audiobook(source, target, title='Invalid plan', chapters=changes)
    assert not target.exists()


def test_existing_audiobook_is_not_overwritten(actual):
    runtime, source, root, chapters = actual
    target = root / 'existing.m4b'; target.write_bytes(b'keep original export')
    with pytest.raises(FileExistsError):
        runtime.audiobook(source, target, title='Reviewed', chapters=chapters)
    assert target.read_bytes() == b'keep original export'


def test_source_change_after_real_encoding_blocks_audiobook_publication(actual, monkeypatch):
    runtime, source, root, chapters = actual
    convert = runtime.convert
    def change_after_encoding(*args, **kwargs):
        result = convert(*args, **kwargs)
        with source.open('r+b') as stream:
            stream.seek(-2, 2); stream.write(b'\x12\x34')
        return result
    monkeypatch.setattr(runtime, 'convert', change_after_encoding)
    with pytest.raises(AudioError, match='source audio changed'):
        runtime.audiobook(source, root / 'changed.m4b', title='Reviewed', chapters=chapters)
    assert not (root / 'changed.m4b').exists() and not list(root.glob('.audiobook-*'))


def test_cancellation_after_real_encode_cleans_staging_without_publication(actual, monkeypatch):
    runtime, source, root, chapters = actual
    stopped = False
    convert = runtime.convert
    def cancel_after_encoding(*args, **kwargs):
        nonlocal stopped
        result = convert(*args, **kwargs); stopped = True
        return result
    monkeypatch.setattr(runtime, 'convert', cancel_after_encoding)
    with pytest.raises(AudioCancelled):
        runtime.audiobook(source, root / 'cancelled.m4b', title='Reviewed', chapters=chapters,
                          cancelled=lambda: stopped)
    assert not (root / 'cancelled.m4b').exists() and not list(root.glob('.audiobook-*'))


def test_embedded_metadata_mismatch_blocks_publication_after_actual_remux(actual, monkeypatch):
    runtime, source, root, chapters = actual
    import carr_library.audio.audiobook as module
    inspect = module.inspect_chapters
    def altered(*args, **kwargs):
        result = inspect(*args, **kwargs)
        result['chapters'][0]['tags']['title'] = 'Unexpected replacement'
        return result
    monkeypatch.setattr(module, 'inspect_chapters', altered)
    with pytest.raises(AudioError, match='embedded chapter names or times'):
        runtime.audiobook(source, root / 'wrong.m4b', title='Reviewed', chapters=chapters)
    assert not (root / 'wrong.m4b').exists()


def test_metadata_and_line_lrc_use_measured_boundaries_without_word_times():
    raw, rows = metadata_bytes([{'title': 'One\n[CHAPTER]', 'start': 0, 'end': 2}],
                               title='A=B#C;D\\E', duration=2, sample_rate=24000)
    assert b'A\\=B\\#C\\;D\\\\E' in raw and b'title=One\\\n[CHAPTER]\n' in raw
    assert rows[0]['end'] == 48000
    assert line_lrc([{'start': 59.999, 'end': 61, 'text': 'First\nSecond'},
                     {'start': 3600.01, 'end': 3601, 'text': 'Кінець 😀'}]) == (
                     '[01:00.00]First\n[01:00.00]Second\n[60:00.01]Кінець 😀\n')
    with pytest.raises(ValueError):
        line_lrc([{'start': 2, 'end': 1, 'text': 'Invalid'}])


@pytest.mark.parametrize(('original', 'expected_lyrics'), [
    ('[10:00.00]Synthetic literal bracket text.',
     ('\u2060[10:00.00]Synthetic literal bracket text.',)),
    (' \t[10:00.00] Leading whitespace stays.',
     ('\u2060 \t[10:00.00] Leading whitespace stays.',)),
    ('[ar:Literal metadata-looking text] Українська 😀',
     ('\u2060[ar:Literal metadata-looking text] Українська 😀',)),
    ('Plain [10:00.00] text\r\n[10:00.00]First bracket row.\n \t[11:00.00]Second bracket row.',
     ('Plain [10:00.00] text', '\u2060[10:00.00]First bracket row.',
      '\u2060 \t[11:00.00]Second bracket row.')),
])
def test_actual_lrc_demux_preserves_literal_brackets_without_extra_timestamps(
        actual, original, expected_lyrics):
    runtime, _, root, _ = actual
    lines = [{'start': 1.0, 'end': 2.0, 'text': original}]
    target = root / 'literal-brackets.lrc'
    raw = line_lrc(lines)
    target.write_text(raw, encoding='utf-8', newline='')
    result = run_process([runtime.ffprobe, '-v', 'error', '-protocol_whitelist', 'file',
        '-f', 'lrc', '-show_packets', '-show_data_hash', 'sha256', '-of', 'json', str(target)],
        budget=AudioBudget(timeout=15))
    packets = json.loads(result.stdout)['packets']
    # Exactly one packet per existing visual row; every row has the one actual
    # measured start. Literal [10:00.00]/[11:00.00] must never create events.
    assert len(packets) == len(expected_lyrics)
    assert [packet['pts_time'] for packet in packets] == ['1.000000'] * len(expected_lyrics)
    for packet, lyric in zip(packets, expected_lyrics):
        expected_bytes = (lyric + '\n').encode('utf-8')
        assert int(packet['size']) == len(expected_bytes)
        assert packet['data_hash'] == 'SHA256:' + sha256(expected_bytes).hexdigest()
    assert raw == ''.join('[00:01.00]' + lyric + '\n' for lyric in expected_lyrics)
    assert lines == [{'start': 1.0, 'end': 2.0, 'text': original}]


@pytest.mark.parametrize('masked', [False, True], ids=['direct-pending', 'masked-pending'])
def test_actual_remux_staging_survives_pending_cleanup(actual, monkeypatch, masked):
    """The real container remains owned while a modeled cleanup proof is pending."""
    import threading
    from carr_library.audio.cleanup import AudioCleanupPending, CleanupOwner
    import carr_library.audio.audiobook as module

    runtime, source, root, chapters = actual
    original = module.run_process
    allow = threading.Event()
    cleanup = CleanupOwner(lambda: allow.is_set())
    attempts = []

    def pending_remux(arguments, **options):
        result = original(arguments, **options)
        if '-map_chapters' in arguments:
            path = Path(arguments[-1])
            assert options['owned_tree'] is True and path.is_file()
            attempts.append(path)
            pending = AudioCleanupPending('Synthetic unconfirmed cleanup after actual remux',
                                          cleanup_owner=cleanup)
            if masked:
                raise OSError('A journal error masked the original native owner') from pending
            raise pending
        return result

    monkeypatch.setattr(module, 'run_process', pending_remux)
    output = root / 'pending.m4b'
    try:
        with pytest.raises(OSError if masked else AudioCleanupPending):
            runtime.audiobook(source, output, title='Reviewed', chapters=chapters)
        assert len(attempts) == 1 and not output.exists()
        stage = attempts[0].parent
        assert (stage / 'encoded.m4b').is_file() and attempts[0].is_file()
        embedded = inspect_chapters(runtime, attempts[0])
        assert [row['tags']['title'] for row in embedded['chapters']] == [
            row['title'] for row in chapters]
        assert cleanup.close() is False and stage.is_dir()
        allow.set()
        assert cleanup.close() is True
        assert not stage.exists() and source.is_file()
    finally:
        allow.set()
        cleanup.close()


def test_source_stamp_change_with_identical_bytes_blocks_actual_audiobook(actual, monkeypatch):
    import os
    runtime, source, root, chapters = actual
    original = source.read_bytes()
    convert = runtime.convert

    def touch_after_encoding(*args, **kwargs):
        result = convert(*args, **kwargs)
        before = source.stat()
        os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns + 2_000_000_000))
        assert source.read_bytes() == original
        return result

    monkeypatch.setattr(runtime, 'convert', touch_after_encoding)
    target = root / 'stamp-change.m4b'
    with pytest.raises(AudioError, match='source audio changed'):
        runtime.audiobook(source, target, title='Reviewed', chapters=chapters)
    assert not target.exists() and source.read_bytes() == original
    assert not list(root.glob('.audiobook-*'))


@pytest.mark.parametrize('change', ['parent', 'staging'])
def test_final_prelink_revalidation_rejects_changed_publication_identity(
        actual, monkeypatch, change):
    import os
    import carr_library.audio.audiobook as module

    runtime, source, root, chapters = actual
    original = source.read_bytes()
    publish = module.publish_file
    target = root / 'last-boundary.m4b'
    relocated = root.with_name('original-export-directory')
    observed = []

    def intervene(staged, output, *, cancelled):
        checks = 0

        def during_commit():
            nonlocal checks
            checks += 1
            if checks == 2:
                # Existing publish_file has completed fsync and closed the
                # writable descriptor. This is its last pre-link check.
                assert staged.is_file() and not output.exists()
                observed.append(staged)
                if change == 'parent':
                    root.rename(relocated)
                    root.mkdir()
                    (root / 'unrelated.txt').write_bytes(b'leave the replacement directory intact')
                else:
                    before = staged.stat()
                    with staged.open('r+b') as stream:
                        stream.seek(-1, 2)
                        byte = stream.read(1)
                        stream.seek(-1, 2)
                        stream.write(bytes((byte[0] ^ 1,)))
                    os.utime(staged, ns=(before.st_atime_ns, before.st_mtime_ns + 2_000_000_000))
            return cancelled()

        return publish(staged, output, cancelled=during_commit)

    monkeypatch.setattr(module, 'publish_file', intervene)
    with pytest.raises(AudioError, match='changed'):
        runtime.audiobook(source, target, title='Reviewed', chapters=chapters)
    assert len(observed) == 1 and not target.exists()
    assert source.read_bytes() == original
    if change == 'parent':
        assert (root / 'unrelated.txt').read_bytes() == b'leave the replacement directory intact'
        assert not (relocated / target.name).exists()
        # The moved, owned stage remains in the old directory; cleanup must
        # not mistake the replacement parent for the captured destination.
        assert (relocated / observed[0].relative_to(root)).is_file()
    else:
        assert not list(root.glob('.audiobook-*'))


def test_cancel_reported_after_actual_link_preserves_verified_completed_m4b(actual, monkeypatch):
    import carr_library.audio.audiobook as module
    runtime, source, root, chapters = actual
    original = source.read_bytes()
    publish = module.publish_file
    stopped = False
    target = root / 'completed-before-cancel.m4b'

    def commit_then_cancel(staged, output, **options):
        nonlocal stopped
        publish(staged, output, **options)
        assert output.is_file() and staged.stat().st_nlink == 2
        stopped = True
        raise AudioCancelled('Synthetic late cancellation after the real commit')

    monkeypatch.setattr(module, 'publish_file', commit_then_cancel)
    result = runtime.audiobook(source, target, title='Reviewed', chapters=chapters,
                              cancelled=lambda: stopped)
    assert stopped and result.path == target and result.sha256 == sha256(target.read_bytes()).hexdigest()
    assert [row['tags']['title'] for row in inspect_chapters(runtime, target)['chapters']] == [
        row['title'] for row in chapters]
    assert source.read_bytes() == original and not list(root.glob('.audiobook-*'))


def test_postcommit_error_reports_verified_retained_m4b_without_relabeling_cancel(actual, monkeypatch):
    import carr_library.audio.audiobook as module
    runtime, source, root, chapters = actual
    publish = module.publish_file
    target = root / 'committed-with-warning.m4b'

    def commit_then_fail(staged, output, **options):
        publish(staged, output, **options)
        raise OSError('Synthetic directory-finalization failure after real link')

    monkeypatch.setattr(module, 'publish_file', commit_then_fail)
    with pytest.raises(AudioError, match='was published and its exact bytes were verified') as failure:
        runtime.audiobook(source, target, title='Reviewed', chapters=chapters)
    assert not isinstance(failure.value, AudioCancelled)
    assert target.is_file() and runtime.inspect(target).duration == pytest.approx(2, abs=.01)
    assert not list(root.glob('.audiobook-*'))


def test_changed_committed_bytes_are_not_accepted_as_verified_output(actual, monkeypatch):
    import carr_library.audio.audiobook as module
    runtime, source, root, chapters = actual
    publish = module.publish_file
    target = root / 'unconfirmed-commit.m4b'
    published = []

    def commit_then_mutate(staged, output, **options):
        publish(staged, output, **options)
        published.append(output.read_bytes())
        with output.open('r+b') as stream:
            stream.seek(-1, 2)
            byte = stream.read(1)
            stream.seek(-1, 2)
            stream.write(bytes((byte[0] ^ 1,)))

    monkeypatch.setattr(module, 'publish_file', commit_then_mutate)
    with pytest.raises(AudioError, match='published, but its final identity could not be confirmed'):
        runtime.audiobook(source, target, title='Reviewed', chapters=chapters)
    assert len(published) == 1 and target.is_file() and target.read_bytes() != published[0]
    assert not list(root.glob('.audiobook-*'))


@pytest.mark.parametrize('cached', [False, True], ids=['fresh-inventory', 'cached-inventory'])
def test_capability_inspection_preserves_cancellation_for_existing_callers(actual, monkeypatch, cached):
    import carr_library.audio.dsp as module
    runtime, _, _, _ = actual
    if cached:
        assert runtime.capabilities()['encoders']
    original = module.run_process
    calls = []

    def inspected(*arguments, **options):
        calls.append(options)
        return original(*arguments, **options)

    monkeypatch.setattr(module, 'run_process', inspected)
    with pytest.raises(AudioCancelled, match='capability'):
        runtime.capabilities(cancelled=lambda: True)
    assert not calls

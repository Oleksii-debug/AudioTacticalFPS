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

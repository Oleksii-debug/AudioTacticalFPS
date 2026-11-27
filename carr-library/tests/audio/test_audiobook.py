"""Actual FFmpeg M4B metadata, source preservation and publication boundaries."""
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
import json
import os
import subprocess
import sys
import threading
import shutil
import struct
import wave

import pytest

from carr_library.audio import AudioBudget, AudioRuntime, AudioCancelled, AudioError
from carr_library.audio.audiobook import inspect_chapters, line_lrc, metadata_bytes


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


@pytest.mark.parametrize('late_error', (OSError, KeyboardInterrupt))
def test_real_owned_m4b_link_survives_writer_interruption(actual, monkeypatch, late_error):
    import carr_library.audio.audiobook as module
    runtime, source, root, chapters = actual
    original, publish = source.read_bytes(), module.publish_file
    target, notices = root / 'committed.m4b', []
    observed = {}

    def committed_then_interrupted(staged, output, **options):
        publish(staged, output, **options)
        observed['staged'] = Path(staged)
        assert os.path.samefile(staged, output)
        observed['bytes'] = Path(output).read_bytes()
        raise late_error('Injected interruption after the actual hard link')

    monkeypatch.setattr(module, 'publish_file', committed_then_interrupted)
    result = runtime.audiobook(source, target, chapters=chapters, title='Committed',
                              warning=notices.append)
    assert result.path == target and result.sha256 == sha256(observed['bytes']).hexdigest()
    assert target.read_bytes() == observed['bytes'] and source.read_bytes() == original
    assert target.stat().st_nlink == 1 and not observed['staged'].exists()
    assert len(inspect_chapters(runtime, target)['chapters']) == len(chapters)
    assert any('atomically published' in message for message in notices)


def test_late_cancel_after_actual_m4b_link_does_not_reverse_verified_output(actual, monkeypatch):
    import carr_library.audio.audiobook as module
    runtime, source, root, chapters = actual
    stopped, publish = threading.Event(), module.publish_file

    def commit_then_cancel(staged, target, **options):
        publish(staged, target, **options)
        stopped.set()

    monkeypatch.setattr(module, 'publish_file', commit_then_cancel)
    target = root / 'late-cancel.m4b'
    result = runtime.audiobook(source, target, chapters=chapters, title='Committed',
                              cancelled=stopped.is_set)
    assert stopped.is_set() and result.sha256 == sha256(target.read_bytes()).hexdigest()
    assert not list(root.glob('.audiobook-*'))


@pytest.mark.parametrize('replacement', ('equal-byte-copy', 'changed-linked-bytes', 'third-link'))
def test_foreign_or_changed_publication_cannot_borrow_this_attempt_success(
        actual, monkeypatch, replacement):
    import carr_library.audio.audiobook as module
    runtime, source, root, chapters = actual
    original, publish = source.read_bytes(), module.publish_file
    target, foreign = root / 'foreign.m4b', root / 'unowned-alias.m4b'
    notices, observed = [], {}

    def replace_publication(staged, output, **options):
        if replacement == 'equal-byte-copy':
            Path(output).write_bytes(Path(staged).read_bytes())
            assert not os.path.samefile(staged, output)
        else:
            publish(staged, output, **options)
            if replacement == 'changed-linked-bytes':
                with Path(output).open('r+b') as stream:
                    stream.seek(-1, 2)
                    value = stream.read(1)
                    stream.seek(-1, 2)
                    stream.write(bytes([value[0] ^ 1]))
            else:
                os.link(output, foreign)
        observed['bytes'] = Path(output).read_bytes()
        raise OSError('Competing or changed publication')

    monkeypatch.setattr(module, 'publish_file', replace_publication)
    with pytest.raises(OSError, match='Competing or changed'):
        runtime.audiobook(source, target, chapters=chapters, title='Rejected', warning=notices.append)
    assert not notices and target.read_bytes() == observed['bytes']
    assert source.read_bytes() == original
    if replacement == 'third-link':
        assert foreign.exists() and os.path.samefile(target, foreign)
        assert foreign.read_bytes() == observed['bytes']


def test_cleanup_failure_is_visible_separately_from_verified_m4b(actual, monkeypatch):
    import carr_library.audio.cleanup as cleanup
    runtime, source, root, chapters = actual
    factory = cleanup.tempfile.TemporaryDirectory
    retained, notices = [], []

    class FailingCleanup:
        def __init__(self, temporary):
            self.name = temporary.name

        def cleanup(self):
            raise OSError('Injected temporary directory removal failure')

    def temporary(*args, **kwargs):
        result = factory(*args, **kwargs)
        if kwargs.get('prefix') == '.audiobook-':
            retained.append(result)
            return FailingCleanup(result)
        return result

    monkeypatch.setattr(cleanup.tempfile, 'TemporaryDirectory', temporary)
    try:
        target = root / 'cleanup-warning.m4b'
        result = runtime.audiobook(source, target, chapters=chapters, title='Committed',
                                  warning=notices.append)
        assert result.sha256 == sha256(target.read_bytes()).hexdigest()
        assert target.stat().st_nlink == 1
        assert retained and Path(retained[0].name).exists()
        assert not (Path(retained[0].name) / 'chapters.m4b').exists()
        assert any('cleanup needs attention' in message for message in notices)
    finally:
        for temporary in retained:
            temporary.cleanup()


@pytest.mark.parametrize('observer_error', (RuntimeError, KeyboardInterrupt))
def test_warning_observer_and_warning_as_error_cannot_erase_m4b_success(
        actual, monkeypatch, observer_error):
    import warnings
    import carr_library.audio.audiobook as module
    runtime, source, root, chapters = actual
    publish = module.publish_file
    calls = []

    def interrupted(staged, target, **options):
        publish(staged, target, **options)
        raise OSError('Writer interrupted after actual publication')

    def observer(message):
        calls.append(message)
        raise observer_error('Observer failed after publication')

    monkeypatch.setattr(module, 'publish_file', interrupted)
    target = root / 'observer.m4b'
    with warnings.catch_warnings():
        warnings.simplefilter('error', RuntimeWarning)
        result = runtime.audiobook(source, target, chapters=chapters, title='Committed',
                                  warning=observer)
    assert calls and result.sha256 == sha256(target.read_bytes()).hexdigest()
    assert target.stat().st_nlink == 1


def test_numeric_runtime_progress_is_not_replaced_with_text_warnings(actual, monkeypatch):
    import carr_library.audio.audiobook as module
    runtime, source, root, chapters = actual
    publish, progress, notices = module.publish_file, [], []

    def interrupted(staged, target, **options):
        publish(staged, target, **options)
        raise OSError('Late writer status')

    monkeypatch.setattr(module, 'publish_file', interrupted)
    runtime.audiobook(source, root / 'progress.m4b', chapters=chapters, title='Measured',
                      progress=progress.append, warning=notices.append)
    assert progress and all(type(value) in (int, float) for value in progress)
    assert notices and all(isinstance(value, str) for value in notices)


def test_unconfirmed_native_owner_retains_audiobook_staging_until_quiescent(actual, monkeypatch):
    from carr_library.audio.cleanup import CleanupOwner
    from carr_library.audio.models import AudioCleanupPending
    import carr_library.audio.audiobook as module
    runtime, source, root, chapters = actual
    original, permitted, seen = source.read_bytes(), threading.Event(), []
    owner = CleanupOwner(permitted.is_set)
    failure = AudioCleanupPending('Injected unconfirmed remux worker', cleanup_owner=owner)

    def uncertain_remux(args, **options):
        # Encoding has actually completed. Only the native remux failure is
        # injected; no successful encoded result or process is fabricated.
        tagged = Path(args[-1])
        assert (tagged.parent / 'encoded.m4b').is_file()
        tagged.write_bytes(b'The native remux owner still owns this staging path')
        seen.append(tagged)
        raise failure

    monkeypatch.setattr(module, 'run_process', uncertain_remux)
    target = root / 'pending.m4b'
    try:
        with pytest.raises(AudioCleanupPending) as caught:
            runtime.audiobook(source, target, chapters=chapters, title='Pending')
        assert caught.value is failure and caught.value.cleanup_owner is owner
        assert seen and seen[0].exists() and (seen[0].parent / 'encoded.m4b').exists()
        assert owner.close() is False and seen[0].exists() and not target.exists()
        assert source.read_bytes() == original
        permitted.set()
        thread = threading.Thread(target=owner.close)
        thread.start()
        thread.join(3)
        assert not thread.is_alive() and owner.quiescent
        assert not seen[0].parent.exists() and not target.exists()
    finally:
        permitted.set()
        owner.close()


@pytest.mark.parametrize('boundary', ('before-link', 'after-link'))
def test_actual_child_exit_preserves_m4b_boundary_without_adopting_residual_staging(
        actual, tmp_path, monkeypatch, boundary):
    import carr_library.audio.audiobook as module
    runtime, source, root, chapters = actual
    original = source.read_bytes()
    target, marker = root / 'child.m4b', tmp_path / 'audiobook-child.json'
    child = r'''
import json, os, sys
from pathlib import Path
from carr_library.audio import AudioRuntime, AudioBudget
from carr_library.audio import audiobook
source, root, output, marker, boundary, expected_origin, chapter_json = sys.argv[1:]
assert Path(audiobook.__file__).resolve() == Path(expected_origin).resolve()
runtime = AudioRuntime(root, input_roots=(Path(source).parent,),
                       budget=AudioBudget(reserve_disk_bytes=0))
publish = audiobook.publish_file
def die(staged, target, **options):
    if boundary == 'after-link':
        publish(staged, target, **options)
        assert os.path.samefile(staged, target)
    Path(marker).write_text(json.dumps({'staged': str(staged)}), encoding='utf-8')
    os._exit(73)
audiobook.publish_file = die
runtime.audiobook(source, output, title='Crash boundary', chapters=json.loads(chapter_json))
raise AssertionError('The actual audiobook publication boundary was not reached.')
'''
    result = subprocess.run([sys.executable, '-B', '-c', child, str(source), str(root),
        str(target), str(marker), boundary, str(Path(module.__file__).resolve()),
        json.dumps(chapters)], cwd=Path(module.__file__).resolve().parents[2],
        capture_output=True, text=True, encoding='utf-8', timeout=90, check=False)
    assert result.returncode == 73, (result.returncode, result.stdout, result.stderr)
    staged = Path(json.loads(marker.read_text(encoding='utf-8'))['staged'])
    assert staged.is_file() and source.read_bytes() == original
    residual = {path.name: sha256(path.read_bytes()).hexdigest()
                for path in staged.parent.iterdir() if path.is_file()}
    fresh = AudioRuntime(root, input_roots=(source.parent,), budget=AudioBudget(reserve_disk_bytes=0))
    if boundary == 'before-link':
        assert not target.exists()
    else:
        assert target.stat().st_nlink == 2 and os.path.samefile(staged, target)
        assert len(inspect_chapters(fresh, target)['chapters']) == len(chapters)
        before = target.read_bytes()
        with pytest.raises(FileExistsError):
            fresh.audiobook(source, target, title='Cannot replace', chapters=chapters)
        assert target.read_bytes() == before and os.path.samefile(staged, target)
    # Explicit new output is a new operation. No old staging is deleted or
    # promoted to a saved completion receipt merely because it is readable.
    other = root / 'explicit-new.m4b'
    completed = fresh.audiobook(source, other, title='Explicit new operation', chapters=chapters)
    assert completed.sha256 == sha256(other.read_bytes()).hexdigest()
    assert residual == {path.name: sha256(path.read_bytes()).hexdigest()
                        for path in staged.parent.iterdir() if path.is_file()}
    assert source.read_bytes() == original


def test_changed_container_after_real_chapter_inspection_is_not_published(actual, monkeypatch):
    import carr_library.audio.audiobook as module
    runtime, source, root, chapters = actual
    inspect = module.inspect_chapters
    calls = []

    def change_after_native_inspection(runtime, path, **options):
        result = inspect(runtime, path, **options)
        calls.append(Path(path))
        with Path(path).open('r+b') as stream:
            stream.seek(-1, 2)
            value = stream.read(1)
            stream.seek(-1, 2)
            stream.write(bytes([value[0] ^ 1]))
        return result

    monkeypatch.setattr(module, 'inspect_chapters', change_after_native_inspection)
    target = root / 'changed-after-inspection.m4b'
    with pytest.raises(AudioError, match='staged audiobook changed during native inspection'):
        runtime.audiobook(source, target, chapters=chapters, title='Changed')
    assert calls and not target.exists() and not list(root.glob('.audiobook-*'))

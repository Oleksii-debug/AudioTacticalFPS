"""Existing PCM reader and retained recording inventory compatibility."""
from pathlib import Path
import os
import wave

import pytest

from carr_library.audio import process
from carr_library.audio.models import AudioCancelled
from carr_library.audio.recording import RecordingService
from carr_library.speech.runtime import _wave_info


def test_shared_pcm_reader_can_cancel_during_actual_chunk_validation(tmp_path, monkeypatch):
    path = tmp_path / 'retained.wav'
    with wave.open(str(path), 'wb') as output:
        output.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
        output.writeframes(b'\x01\x00' * 140000)
    actual = wave.Wave_read.readframes
    reads = []

    def read_frames(self, count):
        block = actual(self, count)
        reads.append(len(block))
        return block

    monkeypatch.setattr(wave.Wave_read, 'readframes', read_frames)
    with pytest.raises(InterruptedError):
        _wave_info(path, cancelled=lambda: bool(reads), max_output_bytes=path.stat().st_size)
    assert reads == [65536 * 2]
    assert _wave_info(path) == (24000, 1, 140000, 2)
    with pytest.raises(ValueError, match='audio-output budget'):
        _wave_info(path, max_output_bytes=path.stat().st_size - 1)


def test_recording_inventory_does_not_hide_directory_enumeration_failure(tmp_path, monkeypatch):
    service = RecordingService(tmp_path)
    actual = os.scandir

    def denied(path):
        if Path(path) == service.root:
            raise PermissionError('Injected unreadable recording directory')
        return actual(path)

    monkeypatch.setattr(os, 'scandir', denied)
    with pytest.raises(PermissionError, match='unreadable recording'):
        service.list()


def test_recording_inventory_can_cancel_while_enumerating_non_receipt_files(tmp_path):
    service = RecordingService(tmp_path)
    (service.root / 'retained-without-receipt.wav').write_bytes(b'Unverified original')
    with pytest.raises(AudioCancelled, match='list cancelled'):
        service.list(cancelled=lambda: True)


def test_owned_windows_job_option_cannot_silently_select_another_platform(tmp_path, monkeypatch):
    if os.name == 'nt':
        return  # Native ownership is exercised by test_windows_audio_ownership.
    import sys

    def no_process(*args, **kwargs):
        pytest.fail('Unsupported ownership request launched a process')

    monkeypatch.setattr(process.subprocess, 'Popen', no_process)
    with pytest.raises(ValueError, match='require native Windows'):
        process.run_process([str(Path(sys.executable).resolve()), '-I', '-B', '-c', 'pass'],
                            owned_windows_job=True)



def test_recording_digest_stops_after_one_byte_beyond_the_original_size(tmp_path, monkeypatch):
    from carr_library.audio.recording import _digest
    source = tmp_path / 'growing-file'
    source.write_bytes(b'base')
    original_open = Path.open
    observed = []

    class Reader:
        def __init__(self, stream):
            self.stream = stream
        def __getattr__(self, name):
            return getattr(self.stream, name)
        def __enter__(self):
            self.stream.__enter__()
            return self
        def __exit__(self, *args):
            return self.stream.__exit__(*args)
        def read(self, count):
            block = self.stream.read(count)
            observed.append(len(block))
            if len(observed) == 1:
                with original_open(source, 'ab') as writer:
                    writer.write(b'x' * 131072)
            return block

    def opened(path, mode='r', *args, **kwargs):
        stream = original_open(path, mode, *args, **kwargs)
        return Reader(stream) if Path(path) == source and mode == 'rb' else stream

    monkeypatch.setattr(Path, 'open', opened)
    with pytest.raises(ValueError, match='grew'):
        _digest(source)
    assert sum(observed) == len(b'base') + 1

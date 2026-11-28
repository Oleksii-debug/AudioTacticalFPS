"""Real input mutation attempts at the native clip DSP read boundary."""
from contextlib import contextmanager
import os
from pathlib import Path
import struct
import subprocess
import sys
import wave

import pytest

from carr_library.audio import clip_audio
from carr_library.audio.models import Clip
from carr_library.audio.runtime import resolve_ffmpeg


@pytest.fixture
def ffmpeg():
    path = resolve_ffmpeg()
    if path is None:
        pytest.fail('This required input-ownership suite needs the prepared local FFmpeg runtime.')
    return path


def pcm(path, *, channels=2, frames=1600):
    with wave.open(str(path), 'wb') as stream:
        stream.setparams((channels, 2, 8000, frames, 'NONE', 'PCM'))
        stream.writeframes(struct.pack('<h', 2500) * frames * channels)
    return path


def operation(tmp_path, kind, ffmpeg):
    source = pcm(tmp_path / 'original.wav')
    output = tmp_path / 'result.wav'
    if kind == 'clip':
        return (source,), source, output, lambda: clip_audio.process_clip(
            source, output, Clip('one', source.name, gain_db=-3), ffmpeg=ffmpeg, sample_rate=8000)
    overlay = pcm(tmp_path / 'overlay.wav', frames=800)
    music = pcm(tmp_path / 'music.wav')
    selected = {'core': source, 'overlay': overlay, 'music': music}[kind]
    return (source, overlay, music), selected, output, lambda: clip_audio.mix_tracks(
        source, [(overlay, 0, 'sfx')], output, music=music, ffmpeg=ffmpeg)


@pytest.mark.parametrize('kind', ['clip', 'core', 'overlay', 'music'])
def test_actual_child_cannot_hide_same_size_restored_write_during_clip_dsp(tmp_path, ffmpeg, monkeypatch, kind):
    paths, selected, output, run = operation(tmp_path, kind, ffmpeg)
    before = {path: path.read_bytes() for path in paths}
    native = clip_audio.audio.run_ffmpeg
    observed = []
    command = [sys.executable, '-I', '-B', '-c',
        'from pathlib import Path\nimport os, sys\n'
        'p=Path(sys.argv[1]); stamp=p.stat(); raw=p.read_bytes()\n'
        'try:\n'
        ' with p.open("r+b") as stream:\n'
        '  stream.seek(50); stream.write(b"\\x01\\x00"); stream.flush(); os.fsync(stream.fileno())\n'
        '  stream.seek(0); stream.write(raw); stream.flush(); os.fsync(stream.fileno())\n'
        ' os.utime(p, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))\n'
        ' print("restored")\n'
        'except PermissionError:\n print("blocked")\n', str(selected)]

    def invoke(*args, **kwargs):
        # The separate writer runs at the actual read handoff, not a sleep.
        child = subprocess.run(command, capture_output=True, text=True, check=True, timeout=10)
        observed.append(child.stdout.strip())
        return native(*args, **kwargs)

    monkeypatch.setattr(clip_audio.audio, 'run_ffmpeg', invoke)
    if os.name == 'nt':
        run()
        assert observed == ['blocked'] and output.is_file()
    else:
        with pytest.raises(RuntimeError, match='changed'):
            run()
        assert observed == ['restored'] and not output.exists()
    assert {path: path.read_bytes() for path in paths} == before
    assert not list(tmp_path.glob('.clip-*'))
    # Both successful and exceptional exits release every input, including the
    # other independently held inputs when one final validator refuses.
    for path in paths:
        with path.open('r+b') as stream:
            stream.seek(50)
            stream.write(b'\x01\x00')


@pytest.mark.parametrize('kind', ['clip', 'core', 'overlay', 'music'])
def test_all_final_input_checks_precede_clip_derivative_publication(tmp_path, ffmpeg, monkeypatch, kind):
    paths, selected, output, run = operation(tmp_path, kind, ffmpeg)
    reader = clip_audio.verified_reader
    finished = []

    @contextmanager
    def checked(path, **kwargs):
        with reader(path, **kwargs) as stream:
            yield stream
        finished.append(Path(path))
        assert not output.exists()
        if Path(path) == selected:
            raise RuntimeError('Injected final clip input refusal')

    monkeypatch.setattr(clip_audio, 'verified_reader', checked)
    with pytest.raises(RuntimeError, match='final clip input refusal'):
        run()
    assert selected in finished and not output.exists()
    assert not list(tmp_path.glob('.clip-*'))
    for path in paths:
        with path.open('r+b') as stream:
            stream.seek(50)
            stream.write(b'\x01\x00')

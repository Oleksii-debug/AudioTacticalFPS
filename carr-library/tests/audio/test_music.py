import hashlib
import shutil
import struct
import threading

import pytest

from carr_library.audio.ffmpeg import AudioBackend,AudioCancelled
from carr_library.audio.music import compose_midi,engines,render_procedural,render_midi_with_fluidsynth


def test_midi_is_real_deterministic_standard_file(tmp_path):
    a=compose_midi(tmp_path/'a.mid',duration_seconds=65,tempo=80,mood='calm',seed=12)
    b=compose_midi(tmp_path/'b.mid',duration_seconds=65,tempo=80,mood='calm',seed=12)
    c=compose_midi(tmp_path/'c.mid',duration_seconds=65,tempo=80,mood='tense',seed=12)
    data=a.read_bytes()
    assert data==b.read_bytes()
    assert data!=c.read_bytes()
    assert data[:14]==b'MThd'+struct.pack('>IHHH',6,0,1,480)
    assert data[14:18]==b'MTrk'
    assert struct.unpack('>I',data[18:22])[0]==len(data)-22
    assert data.endswith(b'\xff\x2f\x00')
    assert b'\x90' in data and b'\x80' in data


def test_midi_resources_no_clobber_and_cancel(tmp_path):
    path=compose_midi(tmp_path/'keep.mid',duration_seconds=1)
    before=path.read_bytes()
    with pytest.raises(FileExistsError):
        compose_midi(path,duration_seconds=2)
    assert path.read_bytes()==before
    with pytest.raises(ValueError,match='note budget'):
        compose_midi(tmp_path/'huge.mid',duration_seconds=36000,max_notes=1)
    event=threading.Event();event.set()
    with pytest.raises(AudioCancelled):
        compose_midi(tmp_path/'cancel.mid',duration_seconds=1,cancel=event)
    assert not (tmp_path/'cancel.mid').exists()
    assert not (tmp_path/'huge.mid').exists()


@pytest.mark.parametrize('mood',['calm','tense','bright'])
def test_actual_local_procedural_audio_is_repeatable_and_frame_exact(tmp_path,mood):
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        pytest.skip('Actual FFmpeg synthesis unavailable; audio generation NOT_RUN')
    backend=AudioBackend()
    first=render_procedural(backend,tmp_path/'first.wav',duration_frames=49001,mood=mood,seed=4)
    second=render_procedural(backend,tmp_path/'second.wav',duration_frames=49001,mood=mood,seed=4)
    assert first.frames==second.frames==49001
    assert hashlib.sha256(first.path.read_bytes()).digest()==hashlib.sha256(second.path.read_bytes()).digest()
    measured=backend.measure(first.path)
    assert measured['true_peak_dbfs'] is not None and measured['true_peak_dbfs'] < -10


def test_engine_claims_and_soundfont_rights_are_explicit(tmp_path):
    available={engine['id']:engine for engine in engines()}
    assert available['musicgen']['available'] is False
    assert 'CC-BY-NC' in available['musicgen']['license_note']
    assert available['ace-step']['available'] is False
    assert available['midi']['available'] is True
    with pytest.raises(ValueError,match='rights'):
        render_midi_with_fluidsynth(None,tmp_path/'a.mid',tmp_path/'s.sf2',tmp_path/'out.wav')

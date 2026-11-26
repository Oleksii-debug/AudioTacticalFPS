"""Actual DSP measurements, not mocked claims about audio quality."""
from array import array
import hashlib
import math
import os
from pathlib import Path
import shutil
import threading
import time
import wave

import pytest

from carr_library.audio.ffmpeg import AudioBackend, AudioCancelled, AudioError, Effect, Track


@pytest.fixture
def backend():
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("Actual FFmpeg/ffprobe runtime is not installed; DSP acceptance NOT_RUN.")
    return AudioBackend(sample_rate=24000)


def tone(path, *, frequency=440, amplitude=.2, seconds=1, rate=24000):
    samples = array("h", (round(amplitude * 32767 * math.sin(2*math.pi*frequency*i/rate))
                          for i in range(round(seconds*rate))))
    with wave.open(str(path), "wb") as audio:
        audio.setparams((1, 2, rate, len(samples), "NONE", "not compressed"))
        audio.writeframes(samples.tobytes())
    return path


def samples(path):
    with wave.open(str(path), "rb") as audio:
        rate, channels = audio.getframerate(), audio.getnchannels()
        values = array("h", audio.readframes(audio.getnframes()))
    return rate, channels, [value/32768 for value in values]


def rms(values):
    return math.sqrt(sum(value*value for value in values)/max(1,len(values)))


def segment(values, rate, channel, start, end):
    return values[round(start*rate)*2+channel:round(end*rate)*2:2]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_real_channel_placement_exact_pause_and_unchanged_inputs(backend,tmp_path):
    first = tone(tmp_path/'ліве голос.wav',frequency=440,seconds=.5)
    second = tone(tmp_path/'праве голос.wav',frequency=700,seconds=.5)
    before = [digest(first),digest(second)]
    info = backend.mix([Track(first,start_frame=12000,pan=-1),
                        Track(second,start_frame=36000,pan=1)],
                       tmp_path/'сцена.wav',duration_frames=60000)
    rate, channels, values = samples(info.path)
    assert (rate,channels,info.frames)==(24000,2,60000)
    assert rms(segment(values,rate,0,.55,.9)) > .13
    assert rms(segment(values,rate,1,.55,.9)) < .0001
    assert rms(segment(values,rate,1,1.55,1.9)) > .13
    assert rms(segment(values,rate,0,1.55,1.9)) < .0001
    assert rms(segment(values,rate,0,1.05,1.45)) < .0001
    assert [digest(first),digest(second)]==before


def test_explicit_stereo_premix_preserves_left_and_right_roles(backend,tmp_path):
    source=tone(tmp_path/'mono.wav',seconds=.25)
    stereo=backend.mix([Track(source,pan=-1),Track(source,start_frame=12000,pan=1)],
                       tmp_path/'premix.wav',duration_frames=24000)
    result=backend.mix([Track(stereo.path,preserve_stereo=True)],tmp_path/'final.wav')
    assert samples(result.path)[2]==samples(stereo.path)[2]
    with pytest.raises(ValueError,match='zero additional pan'):
        backend.mix([Track(stereo.path,preserve_stereo=True,pan=.5)],tmp_path/'bad.wav')


def test_real_music_ducking_reduces_background_during_speech(backend,tmp_path):
    voice=tone(tmp_path/'voice.wav',frequency=800,seconds=1,amplitude=.6)
    music=tone(tmp_path/'music.wav',frequency=120,seconds=3,amplitude=.2)
    tracks=[Track(voice,start_frame=24000,pan=-1),Track(music,kind='music')]
    dry=backend.mix(tracks,tmp_path/'dry.wav',duration_frames=72000,duck_music=False)
    ducked=backend.mix(tracks,tmp_path/'ducked.wav',duration_frames=72000)
    rate,_,dry_values=samples(dry.path)
    _,_,duck_values=samples(ducked.path)
    # Speech is entirely left; right-channel measurements isolate music.
    before=rms(segment(dry_values,rate,1,1.3,1.8))
    after=rms(segment(duck_values,rate,1,1.3,1.8))
    assert after < before*.4
    assert rms(segment(duck_values,rate,1,.2,.7)) == pytest.approx(
        rms(segment(dry_values,rate,1,.2,.7)),abs=.0001)
    assert max(map(abs,duck_values)) <= .951


def test_real_equalizer_gate_compressor_delay_and_fades(backend,tmp_path):
    base=tone(tmp_path/'tone.wav',frequency=1000,amplitude=.1,seconds=1)
    low=tone(tmp_path/'quiet.wav',frequency=1000,amplitude=.001,seconds=1)
    loud=tone(tmp_path/'loud.wav',frequency=1000,amplitude=.7,seconds=1)
    plain=backend.mix([Track(base)],tmp_path/'plain.wav')
    equalized=backend.mix([Track(base,effects=(Effect('eq',{'frequency':1000,'gain':6,'q':1}),))],tmp_path/'eq.wav')
    gated=backend.mix([Track(low,effects=(Effect('gate',{'threshold':.02}),))],tmp_path/'gate.wav')
    compressed=backend.mix([Track(loud,effects=(Effect('compressor',{'threshold':.1,'ratio':8}),))],tmp_path/'comp.wav')
    faded=backend.mix([Track(base,effects=(Effect('fade_in',{'seconds':.5}),Effect('fade_out',{'seconds':.5})))],tmp_path/'fade.wav')
    _,_,a=samples(plain.path); _,_,b=samples(equalized.path)
    assert rms(b[10000:30000])/rms(a[10000:30000])==pytest.approx(2,rel=.03)
    assert rms(samples(gated.path)[2][10000:30000]) < .00003
    assert rms(samples(compressed.path)[2][10000:30000]) < .2
    fv=samples(faded.path)[2]
    assert rms(fv[:1000]) < rms(fv[22000:26000])*.1
    assert rms(fv[-1000:]) < rms(fv[22000:26000])*.1
    impulse=tmp_path/'impulse.wav'
    with wave.open(str(impulse),'wb') as wav:
        wav.setparams((1,2,24000,2400,'NONE','not compressed'))
        wav.writeframes(array('h',[8000]+[0]*2399).tobytes())
    delayed=backend.mix([Track(impulse,effects=(Effect('delay',{'delay_ms':200,'decay':.5}),))],tmp_path/'delay.wav')
    _,_,iv=samples(delayed.path)
    assert delayed.frames==7200
    assert abs(iv[0])>.15
    assert abs(iv[4800*2])==pytest.approx(abs(iv[0])*.5,abs=.0001)


def test_loudness_is_measured_from_actual_output(backend,tmp_path):
    original=tone(tmp_path/'source.wav',amplitude=.03,seconds=3)
    output=backend.mix([Track(original)],tmp_path/'normalized.wav',
        master_effects=(Effect('normalize',{'lufs':-18,'true_peak':-1.5}),))
    measurement=backend.measure(output.path)
    assert measurement['integrated_lufs']==pytest.approx(-18,abs=1)
    assert measurement['true_peak_dbfs'] <= -.1
    assert measurement['duration_seconds']==3


def test_silence_and_lossless_join_preserve_exact_frames(backend,tmp_path):
    silence=backend.mix([],tmp_path/'pause.wav',duration_frames=12345)
    source=tone(tmp_path/'voice.wav',seconds=.5)
    scene=backend.mix([Track(source)],tmp_path/'scene.wav')
    joined=backend.concatenate([scene.path,silence.path,scene.path],tmp_path/'joined.flac')
    assert joined.frames==scene.frames*2+12345
    assert joined.exact_frames
    assert not any(samples(silence.path)[2])


@pytest.mark.parametrize('format',['wav','flac','mp3','ogg','m4a','opus','aiff','aac','wma'])
def test_real_supported_export_formats(backend,tmp_path,format):
    if format not in backend.capabilities()['formats']:
        pytest.skip(f'{format} encoder absent from selected local build')
    source=tone(tmp_path/'input.wav',seconds=.5)
    exported=backend.convert(source,tmp_path/f'output.{format}',format=format)
    assert exported.channels==2
    assert abs(exported.duration_seconds-.5)<.15
    assert exported.path.is_file()


def test_no_overwrite_invalid_effect_and_no_network_inputs(backend,tmp_path):
    source=tone(tmp_path/'input.wav')
    output=tmp_path/'keep.wav'; output.write_bytes(b'preserve existing user data')
    with pytest.raises(FileExistsError):
        backend.mix([Track(source)],output)
    assert output.read_bytes()==b'preserve existing user data'
    with pytest.raises(ValueError):
        backend.mix([Track(source,effects=(Effect('eq',{'frequency':'1000;movie=http://invalid'}),))],tmp_path/'bad.wav')
    with pytest.raises(ValueError):
        backend.mix([Track(source,effects=(Effect('reverb',{}),))],tmp_path/'bad.wav')
    with pytest.raises((ValueError,FileNotFoundError)):
        backend.probe('https://invalid.example/audio.wav')
    with pytest.raises(ValueError):
        backend.probe('\\\\server\\share\\audio.wav')
    renamed_playlist=tmp_path/'playlist.wav'
    renamed_playlist.write_text('ffconcat version 1.0\nfile https://invalid.example/audio.wav\n')
    with pytest.raises(AudioError):
        backend.probe(renamed_playlist)
    assert not (tmp_path/'bad.wav').exists()


def test_symlinks_and_nonfinite_controls_rejected(backend,tmp_path):
    source=tone(tmp_path/'source.wav')
    link=tmp_path/'link.wav'
    try:
        link.symlink_to(source)
    except OSError:
        pytest.skip('This Windows account cannot create a symlink; remaining control checks have separate cases.')
    with pytest.raises(ValueError):
        backend.probe(link)
    for invalid in (math.nan,math.inf,True,'0'):
        with pytest.raises(ValueError):
            backend.mix([Track(source,pan=invalid)],tmp_path/'invalid.wav')


def test_windows_reparse_ancestor_rejected_before_input_or_output(monkeypatch,tmp_path):
    """Exercise the Python 3.11 lstat branch without claiming a native junction test."""
    import stat
    from types import SimpleNamespace
    from carr_library.audio.ffmpeg import _local
    source=tone(tmp_path/'source.wav')
    original=Path.lstat
    def with_reparse(path,*args,**kwargs):
        actual=original(path,*args,**kwargs)
        if path==tmp_path:
            return SimpleNamespace(st_mode=actual.st_mode,
                                   st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
        return actual
    monkeypatch.setattr(Path,'lstat',with_reparse)
    with pytest.raises(ValueError,match='reparse'):
        _local(source)
    with pytest.raises(ValueError,match='reparse'):
        _local(tmp_path/'future.wav',existing=False)


def test_cancellation_and_deadline_leave_no_partial_output(backend,tmp_path):
    event=threading.Event(); event.set()
    with pytest.raises(AudioCancelled):
        backend.mix([],tmp_path/'cancelled.wav',duration_frames=24000,cancel=event)
    with pytest.raises(AudioCancelled):
        backend.mix([],tmp_path/'deadline.wav',duration_frames=24000,deadline=time.monotonic()-1)
    assert not list(tmp_path.glob('*.wav'))
    assert not list(tmp_path.glob('.carr-audio-*'))
    event.clear()
    timer=threading.Timer(.2,event.set)
    timer.start()
    try:
        with pytest.raises(AudioCancelled):
            backend.mix([],tmp_path/'long.wav',duration_frames=24000*36000,cancel=event)
    finally:
        timer.cancel()
    assert not (tmp_path/'long.wav').exists()
    assert not list(tmp_path.glob('.carr-audio-*'))


def test_runtime_identity_and_effect_capabilities(backend):
    assert backend.identity.startswith('ffmpeg-v1:')
    assert len(backend.identity)==len('ffmpeg-v1:')+64
    assert backend.identity==backend.identity
    assert backend.effect_schema()['eq']['parameters']['gain']['unit']=='dB'
    assert backend.capabilities()['local_only'] is True


@pytest.mark.skipif(os.environ.get('CARR_AUDIO_LONG_TESTS')!='1',
                   reason='Explicit extended streaming fixture; not a full-length neural generation claim.')
@pytest.mark.parametrize('hours',[1,3,10])
def test_extended_actual_stream_assembly_of_repeated_synthetic_silence(tmp_path,hours):
    """Actual full output, clearly repeated synthetic content, tiny compressed disk."""
    import tracemalloc
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        pytest.skip('FFmpeg missing; extended audio acceptance NOT_RUN')
    backend=AudioBackend(sample_rate=8000)
    minute=backend.mix([],tmp_path/'minute.flac',duration_frames=8000*60)
    tracemalloc.start()
    start=time.monotonic()
    joined=backend.concatenate([minute.path]*(hours*60),tmp_path/f'{hours}-hours.flac')
    elapsed=time.monotonic()-start
    _,peak=tracemalloc.get_traced_memory(); tracemalloc.stop()
    assert joined.frames==8000*3600*hours
    assert joined.path.stat().st_size<hours*1_100_000
    assert peak<8_000_000
    print({'fixture':'repeated synthetic silence; no neural generation', 'hours':hours,
           'frames':joined.frames,'bytes':joined.path.stat().st_size,
           'python_peak_bytes':peak,'assembly_seconds':round(elapsed,3)})

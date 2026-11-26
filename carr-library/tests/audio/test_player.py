"""Lifecycle tests use a fake driver; actual Windows device test is separate."""
from array import array
import math
import ctypes
import os
import sys
import time
import wave

import pytest

from carr_library.audio.player import NativePlayer,PlayerError,PlayerUnavailable


def wav(path,seconds=1):
    data=array('h',(round(2000*math.sin(2*math.pi*440*i/24000)) for i in range(round(24000*seconds))))
    with wave.open(str(path),'wb') as out:
        out.setparams((1,2,24000,len(data),'NONE','not compressed'))
        out.writeframes(data.tobytes())
    return path


class FakeWaveDriver:
    def __init__(self):
        self.buffers=[];self.frames=0;self.opened=False;self.paused=False
        self.volumes=[];self.released=0;self.block=2
    def open(self,rate,channels,width):
        self.opened=True;self.block=channels*width;self.frames=0
        return 12345
    def queue(self,handle,pcm):
        assert handle==12345 and self.opened
        item={'pcm':pcm,'done':False};self.buffers.append(item);return item
    def done(self,buffer): return buffer['done']
    def release(self,handle,buffer):
        assert buffer['done'];self.buffers.remove(buffer);self.released+=1
    def reset(self,handle):
        self.frames=0
        for item in self.buffers:item['done']=True
    def close(self,handle):
        assert not self.buffers;self.opened=False
    def pause(self,handle): self.paused=True
    def resume(self,handle): self.paused=False
    def position(self,handle,rate,block_align): return self.frames
    def volume(self,handle,value):
        assert handle==12345;self.volumes.append(value)
    def finish(self):
        for item in self.buffers:
            if not item['done']:
                item['done']=True;self.frames+=len(item['pcm'])//self.block


def player():
    driver=FakeWaveDriver()
    return NativePlayer(_driver=driver,_start_thread=False),driver


def test_lifecycle_buffers_pause_seek_volume_stop_and_close(tmp_path):
    native,driver=player();source=wav(tmp_path/'відтворення.wav')
    original=source.read_bytes()
    assert native.open(source)==1000
    assert native.duration_ms()==1000
    native.set_volume(35);assert driver.volumes[-1]==35
    native.play();assert native.state=='playing'
    assert len(driver.buffers)<=3
    assert sum(len(item['pcm']) for item in driver.buffers)<=3*8192*4
    driver.finish();native._pump()
    assert native.position_ms()==375
    native.pause();assert native.state=='paused' and driver.paused
    native.seek(600);assert native.position_ms()==600 and native.state=='paused'
    native.resume();assert native.state=='playing' and not driver.paused
    while native.state=='playing':driver.finish();native._pump()
    assert native.position_ms()==1000
    native.play();assert native.position_ms()==0
    native.stop();assert native.position_ms()==0 and not driver.buffers
    native.close();native.close()
    assert native.state=='closed' and not driver.opened
    assert source.read_bytes()==original
    native.open(source);native.play();native.close()  # Reopen after close is supported.


def test_invalid_input_and_controls_never_reach_driver(tmp_path):
    native,driver=player()
    with pytest.raises(PlayerError):native.play()
    for volume in (-1,101,True,1.5,'5'):
        with pytest.raises(ValueError):native.set_volume(volume)
    rf64=tmp_path/'long.wav';rf64.write_bytes(b'RF64'+b'\0'*4+b'WAVE')
    with pytest.raises(PlayerError,match='RF64'):native.open(rf64)
    playlist=tmp_path/'fake.wav';playlist.write_text('https://invalid.example/voice.wav')
    with pytest.raises(PlayerError,match='RIFF'):native.open(playlist)
    assert not driver.opened
    source=wav(tmp_path/'valid.wav');native.open(source)
    for position in (-1,1001,True,1.5,'0'):
        with pytest.raises(ValueError):native.seek(position)
    native.close()


def test_driver_error_is_readable_and_pending_buffers_remain_owned(tmp_path):
    native,driver=player();native.open(wav(tmp_path/'source.wav'))
    def fail(handle,pcm):raise PlayerError('Synthetic disconnected output device')
    driver.queue=fail
    with pytest.raises(PlayerError,match='disconnected output'):
        native.play()
    assert native.state=='error'
    native.stop();assert native.state=='stopped'
    native.close()


def test_truncated_wav_is_not_reported_as_successful_playback(tmp_path):
    source=wav(tmp_path/'truncated.wav')
    source.write_bytes(source.read_bytes()[:100])
    native,driver=player();native.open(source)
    with pytest.raises(PlayerError,match='declared duration'):
        native.play()
    assert native.state=='error'
    native.close()


def test_volume_unavailable_does_not_hide_device_capability(tmp_path):
    native,driver=player()
    def unavailable(handle,value):raise PlayerError('volume unsupported')
    driver.volume=unavailable
    native.open(wav(tmp_path/'source.wav'))
    assert native.capabilities()['volume'] is False
    assert 'volume' in native.notice
    native.play()
    with pytest.raises(PlayerError,match='unsupported'):native.set_volume(20)
    native.close()


def test_non_windows_is_explicitly_unavailable():
    if sys.platform=='win32':
        pytest.skip('Non-Windows availability assertion is not applicable on Windows.')
    with pytest.raises(PlayerUnavailable,match='Windows'):NativePlayer()


def test_winmm_structs_and_calls_with_fake_system_dll(monkeypatch):
    """ABI layout and dispatch only, explicitly not a native output-device test."""
    from carr_library.audio import player as module
    calls=[]
    class Function:
        def __init__(self,name):self.name=name
        def __call__(self,*args):
            calls.append((self.name,args))
            if self.name=='waveOutOpen':args[0]._obj.value=12345
            if self.name=='waveOutPrepareHeader':args[1]._obj.flags=2
            if self.name=='waveOutWrite':args[1]._obj.flags=1
            if self.name=='waveOutGetPosition':
                args[1]._obj.kind=4;args[1]._obj.time.value=400  # Driver chooses bytes.
            return 0
    class DLL:
        def __getattr__(self,name):
            function=Function(name);setattr(self,name,function);return function
    library=DLL()
    def load(name,**kwargs):
        assert name=='winmm.dll' and kwargs['winmode']==0x800
        return library
    monkeypatch.setattr(module.sys,'platform','win32')
    monkeypatch.setattr(module.ctypes,'WinDLL',load,raising=False)
    driver=module._WinMM()
    assert ctypes.sizeof(module._WaveFormat)==18
    assert ctypes.sizeof(module._WaveHeader)==(48 if ctypes.sizeof(ctypes.c_void_p)==8 else 32)
    assert ctypes.sizeof(module._MMTime)==12
    handle=driver.open(24000,1,2)
    buffer=driver.queue(handle,b'\0'*200)
    assert driver.done(buffer)
    assert driver.position(handle,24000,2)==200
    driver.volume(handle,50)
    name,args=calls[-1]
    assert name=='waveOutSetVolume' and args[0] is handle
    assert args[1]==0x80008000
    driver.release(handle,buffer);driver.reset(handle);driver.close(handle)
    assert library.waveOutOpen.restype is ctypes.c_uint32


@pytest.mark.skipif(sys.platform!='win32' or os.environ.get('CARR_AUDIO_DEVICE_TESTS')!='1',
                   reason='Actual Windows output-device probe NOT_RUN without explicit local device setup.')
def test_actual_windows_device_stream_pause_resume_seek_and_close(tmp_path):
    """Real API/device movement, still not a human listening or NVDA pass."""
    source=wav(tmp_path/'native.wav',seconds=2)
    with NativePlayer() as native:
        native.open(source)
        native.set_volume(10)
        native.play()
        time.sleep(.35)
        assert native.position_ms()>0
        native.pause();position=native.position_ms()
        time.sleep(.1)
        assert abs(native.position_ms()-position)<=20
        native.seek(1000)
        assert abs(native.position_ms()-1000)<=20
        native.resume();time.sleep(.2)
        assert native.position_ms()>1000
        native.stop();assert native.position_ms()==0

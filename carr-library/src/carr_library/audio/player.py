"""Bounded local PCM WAV playback using the installed Windows WinMM API.

No command strings, shell, downloads or system-wide mixer changes are used.
Playback errors are surfaced to the native UI; lifecycle tests with a fake
driver are not evidence that a real audio device or NVDA has been exercised.

Primary API contracts: Microsoft Learn /windows/win32/api/mmeapi/
nf-mmeapi-waveoutopen, ns-mmeapi-wavehdr, nf-mmeapi-waveoutgetposition,
nf-mmeapi-waveoutreset and nf-mmeapi-waveoutsetvolume (checked 2026-11-26).
"""
from __future__ import annotations

import ctypes
from pathlib import Path
import sys
import threading
import wave

from .ffmpeg import _local


class PlayerError(RuntimeError):
    pass


class PlayerUnavailable(PlayerError):
    pass


class _WaveFormat(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("tag",ctypes.c_uint16),("channels",ctypes.c_uint16),
                ("rate",ctypes.c_uint32),("bytes_per_second",ctypes.c_uint32),
                ("block_align",ctypes.c_uint16),("bits",ctypes.c_uint16),
                ("extra",ctypes.c_uint16)]


class _WaveHeader(ctypes.Structure):
    _fields_ = [("data",ctypes.c_void_p),("length",ctypes.c_uint32),
                ("recorded",ctypes.c_uint32),("user",ctypes.c_size_t),
                ("flags",ctypes.c_uint32),("loops",ctypes.c_uint32),
                ("next",ctypes.c_void_p),("reserved",ctypes.c_size_t)]


class _TimeUnion(ctypes.Union):
    _fields_ = [("value",ctypes.c_uint32),("smpte",ctypes.c_ubyte*8)]


class _MMTime(ctypes.Structure):
    _fields_ = [("kind",ctypes.c_uint32),("time",_TimeUnion)]


class _WinMM:
    def __init__(self):
        if sys.platform != "win32":
            raise PlayerUnavailable("Native preview is available on Windows. Rendered audio can still be exported here.")
        try:
            # Search only the Windows system directory, not a book/project folder.
            self.api = ctypes.WinDLL("winmm.dll", winmode=0x00000800)
        except OSError as exc:
            raise PlayerUnavailable("Windows audio services are unavailable.") from exc
        self._retained=[]
        signatures = {
            "waveOutOpen": [ctypes.POINTER(ctypes.c_void_p),ctypes.c_uint32,
                            ctypes.POINTER(_WaveFormat),ctypes.c_size_t,ctypes.c_size_t,ctypes.c_uint32],
            "waveOutPrepareHeader": [ctypes.c_void_p,ctypes.POINTER(_WaveHeader),ctypes.c_uint32],
            "waveOutWrite": [ctypes.c_void_p,ctypes.POINTER(_WaveHeader),ctypes.c_uint32],
            "waveOutUnprepareHeader": [ctypes.c_void_p,ctypes.POINTER(_WaveHeader),ctypes.c_uint32],
            "waveOutPause": [ctypes.c_void_p], "waveOutRestart": [ctypes.c_void_p],
            "waveOutReset": [ctypes.c_void_p], "waveOutClose": [ctypes.c_void_p],
            "waveOutGetPosition": [ctypes.c_void_p,ctypes.POINTER(_MMTime),ctypes.c_uint32],
            "waveOutSetVolume": [ctypes.c_void_p,ctypes.c_uint32],
            "waveOutGetErrorTextW": [ctypes.c_uint32,ctypes.c_wchar_p,ctypes.c_uint32],
        }
        for name, args in signatures.items():
            function = getattr(self.api,name)
            function.argtypes, function.restype = args, ctypes.c_uint32

    def _check(self, code):
        if code:
            text = ctypes.create_unicode_buffer(512)
            self.api.waveOutGetErrorTextW(code,text,len(text))
            raise PlayerError(f"Windows audio device error {code}: {text.value or 'operation unavailable'}")

    def open(self,rate,channels,width):
        handle=ctypes.c_void_p()
        block=channels*width
        fmt=_WaveFormat(1,channels,rate,rate*block,block,width*8,0)
        self._check(self.api.waveOutOpen(ctypes.byref(handle),0xffffffff,ctypes.byref(fmt),0,0,0))
        if not handle.value:
            raise PlayerError("Windows returned no usable audio output handle.")
        return handle

    def queue(self,handle,pcm):
        data=ctypes.create_string_buffer(pcm)
        header=_WaveHeader(ctypes.cast(data,ctypes.c_void_p),len(pcm),0,0,0,0,None,0)
        self._check(self.api.waveOutPrepareHeader(handle,ctypes.byref(header),ctypes.sizeof(header)))
        try:
            self._check(self.api.waveOutWrite(handle,ctypes.byref(header),ctypes.sizeof(header)))
        except BaseException:
            released=self.api.waveOutUnprepareHeader(handle,ctypes.byref(header),ctypes.sizeof(header))
            if released:
                # A faulty/disconnected driver can retain a prepared buffer.
                # Keep its Python memory alive until reset permits release.
                self._retained.append((handle,data,header))
            raise
        return data,header

    def done(self,buffer):
        return bool(buffer[1].flags & 0x00000001)

    def release(self,handle,buffer):
        self._check(self.api.waveOutUnprepareHeader(handle,ctypes.byref(buffer[1]),ctypes.sizeof(buffer[1])))

    def reset(self,handle):
        self._check(self.api.waveOutReset(handle))
        for item in tuple(self._retained):
            if item[0] is handle:
                self._check(self.api.waveOutUnprepareHeader(handle,ctypes.byref(item[2]),ctypes.sizeof(item[2])))
                self._retained.remove(item)
    def close(self,handle): self._check(self.api.waveOutClose(handle))
    def pause(self,handle): self._check(self.api.waveOutPause(handle))
    def resume(self,handle): self._check(self.api.waveOutRestart(handle))

    def position(self,handle,rate,block_align):
        stamp=_MMTime();stamp.kind=2  # TIME_SAMPLES; driver may return an alternative.
        self._check(self.api.waveOutGetPosition(handle,ctypes.byref(stamp),ctypes.sizeof(stamp)))
        if stamp.kind==2:
            return int(stamp.time.value)
        if stamp.kind==4:  # TIME_BYTES
            return int(stamp.time.value)//block_align
        if stamp.kind==1:  # TIME_MS
            return int(stamp.time.value)*rate//1000
        raise PlayerError("The audio driver does not provide a usable playback position.")

    def volume(self,handle,percent):
        level=round(percent*65535/100)
        # Always the open HANDLE: passing a device ID would change all instances.
        self._check(self.api.waveOutSetVolume(handle,level | (level << 16)))


class NativePlayer:
    """Play/pause/seek one local ordinary RIFF PCM16 WAV without loading it all.

    `_driver` and `_start_thread` are private injection points for lifecycle
    testing. The normal constructor loads only the real Windows system API.
    """
    def __init__(self,*,_driver=None,_start_thread=True):
        self._driver=_driver if _driver is not None else _WinMM()
        self._thread_enabled=_start_thread
        self._lock=threading.RLock()
        self._wake=threading.Event()
        self._shutdown=threading.Event()
        self._thread=None
        self._handle=None
        self._wave=None
        self._buffers=[]
        self._state="closed"
        self._path=None
        self._base_frame=0
        self._total_frames=0
        self._rate=24000
        self._block_align=4
        self._finished=False
        self._error=None
        self._volume=70
        self.volume_available=None
        self.notice=""

    @property
    def state(self):
        with self._lock:
            return self._state

    def capabilities(self):
        return {"backend":"Windows WinMM waveOut", "formats":["RIFF PCM16 WAV"],
                "streaming":True,"pause":True,"seek":True,"volume":self.volume_available,
                "device_selection":False,"rf64":False,"compressed_playback":False,
                "notice":self.notice}

    def _require(self,*,errors=True):
        if self._handle is None or self._wave is None:
            raise PlayerError("Open a rendered WAV file before using playback controls.")
        if errors and self._error:
            raise PlayerError(self._error)

    def open(self,path):
        path=_local(path)
        if path.suffix.lower()!='.wav':
            raise PlayerError("Native preview accepts PCM WAV. Export a WAV preview or open a chapter master.")
        with path.open('rb') as stream:
            header=stream.read(12)
        if header[:4]==b'RF64':
            raise PlayerError("RF64 preview is unavailable in this player. Play individual chapter WAV masters.")
        if len(header)!=12 or header[:4]!=b'RIFF' or header[8:]!=b'WAVE':
            raise PlayerError("Select a valid local RIFF WAV file, not a playlist or URL.")
        try:
            reader=wave.open(str(path),'rb')
            if (reader.getcomptype()!='NONE' or reader.getsampwidth()!=2 or
                    reader.getnchannels() not in (1,2) or not 8000<=reader.getframerate()<=192000 or
                    reader.getnframes()<1):
                raise PlayerError("Preview requires uncompressed PCM16 mono or stereo WAV at a supported sample rate.")
        except (wave.Error,EOFError) as exc:
            raise PlayerError("The WAV header could not be read safely.") from exc
        except BaseException:
            if 'reader' in locals(): reader.close()
            raise
        try:
            with self._lock:
                self._close_locked()
                handle=self._driver.open(reader.getframerate(),reader.getnchannels(),reader.getsampwidth())
                self._handle,self._wave,self._path=handle,reader,path
                self._rate=reader.getframerate()
                self._block_align=reader.getnchannels()*reader.getsampwidth()
                self._total_frames=reader.getnframes()
                self._base_frame=0
                self._finished=False
                self._state="stopped"
                self._error=None
                self._shutdown.clear()
                self.volume_available=True
                self.notice=""
                try:
                    self._driver.volume(handle,self._volume)
                except PlayerError as exc:
                    self.volume_available=False
                    self.notice=f"This audio device does not provide per-player volume: {exc}"
                return self.duration_ms()
        except BaseException:
            if self._wave is not reader:
                reader.close()
            raise

    def _reset_locked(self):
        self._driver.reset(self._handle)
        while self._buffers:
            self._driver.release(self._handle,self._buffers[0])
            self._buffers.pop(0)

    def _close_locked(self):
        if self._handle is not None:
            self._reset_locked()
            self._driver.close(self._handle)
            self._handle=None
        if self._wave is not None:
            self._wave.close()
            self._wave=None
        self._state="closed"
        self._path=None
        self._error=None

    def _pump_locked(self):
        if self._state!='playing':
            return
        while self._buffers and self._driver.done(self._buffers[0]):
            self._driver.release(self._handle,self._buffers[0])
            self._buffers.pop(0)
        frames_per_buffer=min(8192,max(1,self._rate//8))
        while len(self._buffers)<3:
            pcm=self._wave.readframes(frames_per_buffer)
            if not pcm:
                if self._wave.tell()<self._total_frames:
                    raise PlayerError("The WAV ended before its declared duration; playback stopped.")
                if not self._buffers:
                    self._state="stopped"
                    self._finished=True
                break
            if len(pcm)%self._block_align:
                raise PlayerError("The WAV contains an incomplete PCM sample frame.")
            self._buffers.append(self._driver.queue(self._handle,pcm))

    def _pump(self):
        with self._lock:
            try:
                self._pump_locked()
            except (OSError,EOFError,PlayerError,wave.Error) as exc:
                self._error=f"Playback stopped: {exc}"
                self._state="error"
                try:
                    self._reset_locked()
                except PlayerError:
                    pass  # Retain native buffers until a successful reset/close.

    def _worker(self):
        while not self._shutdown.is_set():
            self._pump()
            self._wake.wait(.01)
            self._wake.clear()

    def _ensure_worker(self):
        if self._thread_enabled and (self._thread is None or not self._thread.is_alive()):
            self._thread=threading.Thread(target=self._worker,name="carr-audio-playback",daemon=True)
            self._thread.start()
        self._wake.set()

    def play(self):
        with self._lock:
            self._require()
            if self._state=='playing':
                return
            if self._finished:
                self._reset_locked()
                self._wave.rewind()
                self._base_frame=0
                self._finished=False
            self._driver.resume(self._handle)
            self._state='playing'
            self._pump()
            self._require()
            self._ensure_worker()

    def pause(self):
        with self._lock:
            self._require()
            if self._state=='playing':
                self._driver.pause(self._handle)
                self._state='paused'

    def resume(self):
        with self._lock:
            self._require()
            if self._state!='paused':
                raise PlayerError("Playback is not paused.")
            self.play()

    def stop(self):
        with self._lock:
            self._require(errors=False)
            self._reset_locked()
            self._wave.rewind()
            self._base_frame=0
            self._finished=False
            self._error=None
            self._state='stopped'

    def seek(self,milliseconds):
        if type(milliseconds) is not int or milliseconds<0:
            raise ValueError("Playback position must be nonnegative whole milliseconds.")
        with self._lock:
            self._require()
            duration=self.duration_ms()
            if milliseconds>duration:
                raise ValueError("Playback position is beyond the end of this audio file.")
            was=self._state
            self._reset_locked()
            target=self._total_frames if milliseconds==duration else milliseconds*self._rate//1000
            self._wave.setpos(target)
            self._base_frame=target
            self._finished=target==self._total_frames
            self._state='stopped' if self._finished else was
            if self._state=='playing':
                self._driver.resume(self._handle)
                self._pump()
                self._require()
                self._ensure_worker()

    def position_ms(self):
        with self._lock:
            self._require()
            frame=self._total_frames if self._finished else self._base_frame+self._driver.position(
                self._handle,self._rate,self._block_align)
            return max(0,min(self._total_frames,frame))*1000//self._rate

    def duration_ms(self):
        with self._lock:
            self._require()
            return self._total_frames*1000//self._rate

    def set_volume(self,value):
        if type(value) is not int or not 0<=value<=100:
            raise ValueError("Playback volume must be an integer from 0 to 100.")
        with self._lock:
            if self._handle is not None:
                self._driver.volume(self._handle,value)
                self.volume_available=True
            self._volume=value

    def close(self):
        with self._lock:
            self._close_locked()
            self._shutdown.set()
            self._wake.set()
        thread=self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1)
        self._thread=None

    def __enter__(self): return self
    def __exit__(self,*_): self.close()

# Native bounded PCM playback

`carr_library.audio.playback.PCMPlayer` plays the integrated studio's local
PCM16 mono/stereo WAV and RF64 masters through the installed Windows WinMM
`waveOut` API. It has no new package dependency, shell command, media association,
download, remote request or full-waveform memory allocation. It reuses the
production `wav_info` validator and opens the selected source read-only.

## Application interface

```python
from carr_library.audio.playback import PCMPlayer, playback_capabilities

capabilities = playback_capabilities()  # No audio starts during inspection.
player = PCMPlayer()
player.play(master_path, start_seconds=0, device_id=None)
player.pause()
player.seek(42.5)
player.resume()
current = player.status()
print(current.state, current.position_seconds, current.duration_seconds)
player.stop()
player.close()
```

`PlaybackController` is an alias of `PCMPlayer`. The optional `on_status`
callback receives immutable `PlaybackStatus` objects and may run on the worker.
A wx caller must use `wx.CallAfter` or poll `status()` from a GUI timer; never
write native GUI controls directly from the callback. The status also includes
frame counts, sample rate, channels, device identity/name, buffered bytes,
position accuracy and an error message. It is JSON serializable through
`to_dict()`; the original path is local state, not telemetry.

`play` validates the local file then starts one worker. Pause, resume and seek
are bounded queue commands and are reflected asynchronously in status. Seek
preserves the paused/playing choice, rounds to a sample frame and can address
RF64 offsets using Python's 64-bit file seek. Seeking to the end completes
playback. `stop(timeout=3)` requests cancellation, waits a bounded time for
cleanup and resets the visible position to zero. `close` stops the worker and
prevents restarting that controller. A driver that does not return control is
reported; a second worker is not started over it.

Default buffering is four 80 ms blocks. Supported configuration is two to eight
blocks of 10–250 ms each, independent of source duration. Native buffers stay
alive until the driver marks them complete and `waveOutUnprepareHeader`
succeeds. Seek/stop first reset the native queue, then unprepare the returned
headers. A driver cleanup failure retains the outstanding objects and disables
new native opens until application restart; driver-owned pointers are never
freed to hide an error. Normal completion releases every buffer and device.

Current time uses `waveOutGetPosition`, checking the returned time format and
unwrapping its 32-bit counter for long sessions. If a driver returns an
unsupported time format, status uses completed-buffer boundaries and labels that
approximation. Device-reported position is not a measured acoustic latency or
guarantee of sample-accurate output. Pause latency and available formats depend
on the driver. No hardware volume, pitch/time stretch, device hot-swap recovery,
cross-device synchronization or real-time DSP is claimed by this adapter.

## Windows/device availability

The Studio Production page exposes a named output-device chooser, Play,
Pause/resume, Stop, a position-in-seconds field, Go, Back/Forward ten seconds,
and a read-only position/duration/device status. Device changes take effect on
the next Play. Duplicate device names have numbered output labels. The GUI
polls immutable state every 500 milliseconds without moving focus; the explicit
Read playback status command moves focus to the readable status field.

Within Studio, Ctrl+Alt+P pauses/resumes, Ctrl+Alt+Left/Right seeks ten seconds,
and Ctrl+Alt+T reads status. Switching projects or importing another original
stops the old preview before replacing the current context. A failed driver
cleanup keeps the old project and prevents a new render from starting. A bad
FFmpeg installation remains readable in the dialog so Choose FFmpeg executable
can repair the configuration.

The Studio retains its previous optional local `ffplay` preview on non-Windows
hosts. Its status reports whether that process is running or has failed and
does not invent a playback position. Native pause, seek and output-device
controls are disabled there. This wrapper is separate from the WinMM adapter's
explicit Windows-only availability contract below.

`output_devices()` lists installed WinMM devices without playing audio.
`playback_capabilities()` reports available/unavailable with a reason; Linux has
an explicit unavailable result and never starts `ffplay` or another fallback.
Missing, removed, busy or incompatible output devices produce readable status.
Changing the source file while it plays stops playback without modifying it.

`native_playback_probe()` defaults to one second of synthetic silence and
actually exercises open/write, pause, seek, resume, stop and cleanup. Run the
installed-package probe on Windows:

```powershell
python -I tests/audio/native_playback_probe.py
```

It reports `PASS`, `UNAVAILABLE` (including CI machines without an audio output)
or `ERROR`. An unavailable device is not device acceptance. Supplying an optional
path explicitly authorizes playing a short portion of that selected file.
Physical output, listening, keyboard and NVDA acceptance are always reported
separately as `NOT_RUN` by this automated probe.

## Reviewed primary contracts

The implementation was written after reviewing these Microsoft references:

- [waveOutOpen](https://learn.microsoft.com/en-us/windows/win32/api/mmeapi/nf-mmeapi-waveoutopen): native signatures, mapper and callback options. The adapter uses no callback function and polls returned buffer flags.
- [WAVEHDR](https://learn.microsoft.com/en-us/windows/win32/api/mmeapi/ns-mmeapi-wavehdr) and [waveOutWrite](https://learn.microsoft.com/en-us/windows/win32/api/mmeapi/nf-mmeapi-waveoutwrite): preparation, queue ownership and completion.
- [waveOutUnprepareHeader](https://learn.microsoft.com/en-us/windows/win32/api/mmeapi/nf-mmeapi-waveoutunprepareheader) and [waveOutReset](https://learn.microsoft.com/en-us/windows/win32/api/mmeapi/nf-mmeapi-waveoutreset): release ordering and returned buffers.
- [Pause/restart behavior](https://learn.microsoft.com/en-us/windows/win32/multimedia/stopping-pausing-and-restarting-playback): pausing before initial submission, driver latency and reset semantics.
- [waveOutGetPosition](https://learn.microsoft.com/en-us/windows/win32/api/mmeapi/nf-mmeapi-waveoutgetposition), [MMTIME](https://learn.microsoft.com/en-us/previous-versions/dd757347(v=vs.85)), [WAVEFORMATEX](https://learn.microsoft.com/en-us/windows/win32/api/mmeapi/ns-mmeapi-waveformatex) and [WAVEOUTCAPSW](https://learn.microsoft.com/en-us/windows/win32/api/mmeapi/ns-mmeapi-waveoutcapsw): fixed-width ABI, format alignment and device metadata.

Microsoft labels Waveform Audio a legacy API and recommends WASAPI/Audio Graphs
for new Windows 10/11 work when possible. This optional compatibility adapter
uses the small existing operating-system API; it is not a WASAPI implementation
or a claim of modern device-feature parity.

## Evidence

Linux tests use an explicitly synthetic native boundary for buffer/state/seek,
cleanup, source modification, device failure and DWORD wrap behavior. They also
check the Windows ABI layouts and true RIFF/RF64 files. Those tests establish
controller behavior and bounded reads, not Windows playback. Actual native
device, audible quality and NVDA checks remain separate gates.

Initial Linux run on Python 3.12.14: 17 tests passed and one actual Windows test
was skipped. The direct probe returned `UNAVAILABLE` with the Windows platform
requirement. Additional synthetic ABI tests exercise the real ctypes header
objects, verifying reset/unprepare/close ordering, preserving an allocated
header after queue failure and retaining memory when a driver refuses cleanup.
These results do not claim execution of a Windows device API.

# Microphone pause, resume and explicit monitoring

The existing local recording dialog now offers **Pause** and **Resume**. Start
still needs a freshly enumerated, explicitly selected DirectShow input. Opening
the dialog, refreshing microphones or listing outputs never starts recording.
No input name fallback, network service or alternate recording engine is used.

## What Pause means

Pause asks the existing admitted FFmpeg capture to finish its current WAV
segment. The dialog enables Resume only after the capture process tree has
confirmed exit, the segment's actual PCM has been verified, and any monitoring
buffers and output handle have been released. While paused there is no capture
process and no new input frames. The same recording operation and its quiet
holds remain owned. Progress reports Paused without inventing completion.

Resume rechecks the configured FFmpeg identity and opens the same exact
DirectShow alternative identifier for the next segment. A missing input fails;
it cannot select a similarly named microphone. Stop during a pause saves the
already retained segments without opening another input. Cancel discards the
unfinished take while preserving earlier completed recordings. An uncertain
capture or monitor close retains private files and quiet ownership and requires
application restart before another take; it is never reported as successful
cancellation.

The final WAV is assembled by streaming the unchanged 16-bit PCM sample frames.
There is no resampling, crossfade, inserted silence or wall-clock timestamp
substitution during this assembly. Its duration is the exact retained frame
count divided by the selected sample rate. Paused wall time is omitted. Review
the joins before attaching the take to Studio: restarting an input can make a
join audible, and this feature does not claim gapless microphone continuity.

The selected audio-duration and byte allowances still apply. A take permits at
most 128 admitted segments and one hour of total paused time; those bounds do
not increase the recorded-duration allowance. Assembly reserves space for both
segments and final PCM and streams bounded blocks. Cancellation remains checked
while waiting, verifying and joining.

## Monitoring is a separate explicit choice

Monitoring is off by default. Choose **Refresh monitoring outputs**, enable
**Enable dry input monitoring**, select a specific output, and then Start. Use
headphones to avoid feeding speaker sound back into the microphone. Selection is
frozen for the take and revalidated on each output open. An unavailable or
changed route refuses the operation; it never falls back to the default device.

The monitor reads only the growing private PCM WAV written by the existing
capture process. It does not open a second microphone or decode another source.
FFmpeg packet flushing makes appended PCM available while capture continues.
The worker submits at most four 50 ms buffers to the selected WinMM output.
Old monitoring backlog and a full output queue drop monitoring frames instead
of delaying capture, allocating an unbounded queue, or changing the saved PCM.
Driver and FFmpeg buffering can add latency. This is dry input monitoring, not
a low-latency effects or acoustic-quality guarantee.

All native buffer memory remains retained until Windows acknowledges header
release; the output handle remains owned until Close succeeds. Pause, Stop and
Cancel all close monitoring through that same ownership path. Native WinMM
driver calls are synchronous and cannot be forcibly preempted by this Python
component. No test below claims otherwise.

## Receipts and backup

Existing `carr-recording/1` receipts remain readable unchanged. Controlled takes
use `carr-recording/2`, with contiguous retained-frame ranges, each range's PCM
SHA-256, its segment-ending reason, and an explicit notice that paused time is
omitted. The ordinary whole-WAV digest is still required. Monitoring records
only a digest of the chosen output identity and submitted/dropped frame counts;
submission does not prove audible playback.

Opening a retained take and the existing typed Studio backup validator verify
the whole WAV and every v2 frame range. Backup and restore never open an engine,
microphone or output device, restore no current microphone selection, and do
not turn a receipt into execution authority. Unknown receipt schemas and
unrelated fields still fail validation. Publication keeps the existing boundary:
once the verified original and its receipt have committed, a late cancellation
cannot erase them.

## Evidence and qualification

`tests/audio/test_recording_controls.py` uses actual admitted FFmpeg children
and a synthetic tone. It checks that Pause has no active capture owner or file
growth, Resume retains exact segment bytes, Stop/Cancel have distinct durable
outcomes, altered segments are refused, and backup can restore a v2 take after
the original library has been removed. It does not use a physical microphone.

`tests/audio/test_recording_monitor.py` checks fixed Windows ABI structures,
bounded real PCM reads and native-call ownership contracts using a controlled
WinMM double. It is not native-device qualification. The dialog tests execute
the production handlers and canonical progress transitions.

The existing `scripts/windows_recording_smoke.py` retains its actual native
keyboard, source-origin, retained-publication, Cancel, Escape and owner-close
checks and adds Pause/Resume with real FFmpeg. Its default run opens no monitor
output and reports that separate qualification as `NOT_RUN_OFF_BY_DEFAULT`.
On a Windows host with a chosen usable output, `--require-monitor-output` adds
actual waveOut buffers and requires confirmed native cleanup; lack of an
identifiable output fails that requested gate. Physical microphone quality,
audible routing, latency and screen-reader acceptance require their own actual
hardware/manual evidence and are not implied by synthetic media results.

## Reused contracts

The capture stays on canonical `audio/process.py` admission, whole-tree cleanup
and publication. The bounded waveOut structures and release ordering were
adapted from prepared component `f510053bacfcde3d48b9b8d6436a36ec2b0aa97d`,
`audio/playback.py` and its dry-monitor helper; the alternate waveIn capture
engine and player were not adopted. Output identities reuse the existing
reader `speech/playback_devices.py` inventory and revalidation.

Primary platform contracts:

- [FFmpeg formats: packet flushing](https://ffmpeg.org/ffmpeg-formats.html)
- [FFmpeg DirectShow input](https://ffmpeg.org/ffmpeg-devices.html#dshow)
- [Windows waveOutReset](https://learn.microsoft.com/en-us/windows/win32/api/mmeapi/nf-mmeapi-waveoutreset)
- [Windows waveOutUnprepareHeader](https://learn.microsoft.com/en-us/windows/win32/api/mmeapi/nf-mmeapi-waveoutunprepareheader)
- [Windows waveOutClose](https://learn.microsoft.com/en-us/windows/win32/api/mmeapi/nf-mmeapi-waveoutclose)

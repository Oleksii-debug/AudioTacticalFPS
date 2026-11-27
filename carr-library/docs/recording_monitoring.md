# Dry monitoring while recording

Dry monitoring is an optional live output of the PCM already being captured.
It is off when the recording dialog opens. It uses the existing selected input,
private WAV, recording worker and measured progress. It does not open a second
input or change the recorded samples.

## Native controls

Open the existing **Local microphone recording** window. Refresh and explicitly
select a microphone as before. To hear its captured PCM while recording:

1. Choose **Refresh monitoring outputs**.
2. Select **Enable dry monitoring** and choose a specific output.
3. Choose **Start recording**.

An enabled monitor without a selected output keeps Start disabled. The output
is checked again by its native interface identity on the recording worker. A
removed, replaced or ambiguous device fails before capture starts; the program
does not select another output automatically. Numeric device order is not a
persistent identity. Refreshing an inventory does not open either an input or
an output.

Use headphones when monitoring a microphone to avoid acoustic feedback. The
selected output belongs to this recording session. This does not change the
Windows default, NVDA routing, or another reader's output selection.

**Stop and save recording** finalizes and verifies the same retained WAV.
**Cancel unfinished recording** discards unfinished capture after owned cleanup.
Progress query and Close remain available during capture. The shared recording
quiet hold suppresses progress speech and unrelated playback until capture and
monitoring both confirm cleanup.

## Bounds and cleanup

The adapted PR152 WinMM implementation queues at most four 50 ms PCM buffers.
This bounds queued application PCM to 200 ms; it is not a promise about a
driver's or device's total audible latency. If output falls behind, the monitor
drops old monitoring frames instead of blocking capture or accumulating an
unbounded queue. Those drops do not remove samples from the saved recording.

The tap reads only the growing private capture WAV. It checks file identity and
PCM format, and it reads bounded complete-frame blocks. Its output is dry:
project effects and processing are not applied to this live feed.

Native buffers remain strongly owned until Reset, UnprepareHeader and Close
succeed. A pending capture process and a pending monitoring driver share the
existing CleanupOwner completion barrier. Private staging, the recording lock
and the quiet hold remain retained until both are quiescent, including when a
status-write exception masks a pending process cleanup error. A failed close
cannot authorize another recording or announce confirmed silence.

## Receipts and compatibility

Ordinary unmonitored originals and retakes retain the existing
carr-recording/1 and carr-recording/2 envelopes. Explicitly monitored takes use
carr-recording/3, with the same source, runtime, PCM, limits and completion
fields, plus:

- monitoring.enabled: true;
- monitoring.output_identity: the selected native interface identity's SHA-256;
- monitoring.submitted_frames and monitoring.dropped_frames: exact nonnegative
  integer counts whose sum cannot exceed the verified capture frame count.

A monitored retake can also contain the existing retake relation with its
original identity, previous identity and previous PCM digest. It does not
reinterpret donor segmented-recording schemas. Retake traversal and complete
backup admission must validate that relation whenever the strictly admitted
receipt contains it.

Submitted frames describe native queue submission, not confirmation that a
person heard them. Counts do not establish microphone or listening quality.
The original's bytes remain immutable; monitor cleanup precedes publication.

## Qualification

This source reuses the bounded buffer and growing-file implementation from
PR152, head 55a0436aba29b5ac4750110f8c8fdc566922d520, adapted to this application's
existing output identity and cleanup contracts.

Prepared qualification at this checkpoint is **NOT_RUN**:

- tests/audio/test_recording_monitor.py: modeled WinMM buffer lifetime,
  format and identity boundaries, real growing-file PCM, and combined cleanup;
- tests/audio/test_recording_monitor_capture.py: real FFmpeg synthetic capture
  with a modeled output sink, unchanged PCM, mixed-version retakes, receipt
  validation, failure propagation and retained recording locks;
- tests/ui/test_recording_dialog.py: existing worker/quiet/cancel/scope cases,
  plus explicit opt-in and output selection;
- scripts/windows_recording_smoke.py: actual native controls and synthetic
  FFmpeg capture, with monitoring left off after exercising its controls;
- scripts/windows_recording_monitor_probe.py: actual selected WinMM output of
  a growing generated 48 kHz PCM fixture, real buffer completion and cleanup.

The native monitor probe requires --device-index and
--expected-device-identity from the reviewed current inventory. Installed
execution additionally uses --expect-installed and -I -B. It fails before
playback if the index now identifies another device. Its output explicitly
keeps actual input capture, physical microphone, human listening and NVDA
qualification separate. The current DirectShow endpoint diagnostic must be
resolved independently before claiming actual combined device capture.

Microsoft's native ownership contracts:
[waveOutOpen](https://learn.microsoft.com/en-us/windows/win32/api/mmeapi/nf-mmeapi-waveoutopen),
[waveOutReset](https://learn.microsoft.com/en-us/windows/win32/api/mmeapi/nf-mmeapi-waveoutreset),
[waveOutUnprepareHeader](https://learn.microsoft.com/en-us/windows/win32/api/mmeapi/nf-mmeapi-waveoutunprepareheader),
[waveOutClose](https://learn.microsoft.com/en-us/windows/win32/api/mmeapi/nf-mmeapi-waveoutclose).

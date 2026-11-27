# Explicit local recording

This recording workflow reuses the public capture implementation reviewed in
PR123, head `28a4e5428a035af8d2f9edd89ebd4ae3802d1f5b`, adapted to the
current audio runtime, process runner, project assets and native worker.
It does not add another operation framework, project store or audio player.

## Use in Resonance

Open **Audio Studio → Record microphone and manage takes…**. Opening the
window loads saved recording receipts only. **Refresh microphones** lists
DirectShow audio inputs without recording; even a single device remains
unselected. Select an input explicitly, give the recording a title and choose
the PCM sample rate, mono/stereo and duration limit. Then choose **Start recording**.

**Stop and save recording** asks the owned FFmpeg process to finish its WAV,
waits for its process family to stop, verifies actual PCM and writes its receipt.
**Cancel unfinished recording** discards that unfinished capture. A cancellation
arriving after the atomic publication cannot erase an already committed original.
**Close/Escape** cancels unfinished work and waits for confirmed cleanup; an
uncertain cleanup retains its device lock, staging and audio quiet hold.

The duration limit is an explicit resource allowance, not an estimate of how
long the speaker will talk. Progress reports measured captured seconds and does
not invent a total or ETA. **Current operation progress** remains enabled while
busy; it also uses the configured existing progress shortcut (Ctrl+Alt+G by
default), and places the result in the modal status control. Buttons have native
labels, tab order and mnemonic access. No new global shortcut is reserved.

Select a saved recording to **Listen to saved recording** using the existing
WavePlayer. Its bytes are verified on the worker before playback. **Record retake
of selected recording** creates a new original with an exact link to the
previous take; it does not replace the previous WAV. **Add to saved Studio
project** asks for an existing project and chapter, then appends an ordinary
content-addressed asset cue. The original script, source identifiers and
quotations remain unchanged. Reopen an already open Studio editor to see the
new saved revision.

This workflow selects a recording **input**. Reader output-device selection,
live input monitoring and a professional waveform/timeline editor are separate
features and are not implemented by this change.

## Runtime and limits

Capture requires an explicitly configured local Windows FFmpeg build with
DirectShow. The application's existing **Local audio runtime setup** flow
selects/verifies that runtime. A transferable application archive does not gain
an audio-runtime payload or any redistribution clearance through recording.

The service accepts PCM16 at 16, 24, 44.1 or 48 kHz and one or two channels.
A selected take is bounded to 1–21,600 seconds and at most 2 GiB, with stricter
caller audio budgets retained. The chosen duration must fit the PCM byte
allowance and actual free disk reserve before the input opens. FFmpeg diagnostic
pipes and staging inventory are bounded. Windows capture starts suspended,
enters its own non-breakaway Job Object, then resumes; termination and cleanup
keep exact handles instead of rediscovering a process by PID.

Progress speech/beeps are held quiet for the entire capture, even if the ordinary
mute preference is off. Only confirmed capture cleanup releases that hold.
No model or cloud service is invoked. Device disappearance, changed library or
runtime identity, output conflicts and invalid PCM fail explicitly.

## Retained originals and backup

Saved originals and typed receipts live under
`state/studio/recordings/<recording-id>.wav` and the matching JSON file.
The receipt binds source device identity, FFmpeg SHA-256, actual PCM format/frame
count, whole-file SHA-256, selected allowances and completion reason.
Retakes additionally bind their original and immediately previous take.

Project attachment uses the existing
`studio/projects/<project-id>/assets/<sha256>.wav` namespace and an asset cue
whose ID retains the recording ID. It does not rewrite source quotations or
replace a narration role automatically. Restored recordings do not authorize a
device or reactivate a runtime.

The complete backup integration validates copied recording receipts, actual PCM
and retake ancestry, coordinates the same `.recording.lock`, and preserves
unreceipted final originals as inert recovery data. Unfinished
`.pending-recording-*` staging is excluded. A receipt-write failure preserves the
committed original and reports the missing receipt; the window lists its
existence without decoding, selecting or playing it. Interrupted physical capture
is not resumable: choose a new take after reviewing retained originals.

## Acceptance boundaries

Portable and installed gates use **actual FFmpeg** with a test-only lavfi source
adapter. Production exposes only enumerated DirectShow inputs, never arbitrary
FFmpeg filters or capture argv. The portable gates cover Stop/Cancel, exact PCM,
retakes, no-clobber publication, late cancellation, changed roots/runtime,
resource limits, retained cleanup locks, UI generation/cancel races and observer
failure. They do not qualify a microphone.

Native probes are separate and fail explicitly if their actual requirements
are unavailable:

| Probe | What it verifies | What it does not claim |
| --- | --- | --- |
| `scripts/windows_recording_smoke.py` | Real wx controls and keyboard dispatch; actual FFmpeg fixture capture, Stop, retake, Cancel, Escape and modal progress | Physical microphone, listening or NVDA |
| `tests/audio/native_recording_process_probe.py` | Actual Windows owned child/descendant exit, unassigned-child cleanup and concurrent unrelated-process isolation | Device capture |
| `scripts/windows_recording_device_probe.py` | With an explicitly selected virtual input: real DirectShow capture plus current MCI output, independently measured 997 Hz loopback PCM | Physical microphone, live monitoring, listening or an output-device matrix |

The device probe supports `--inventory-only` with no capture. Capture requires
an exact `--device-id` or unique exact `--device-name` supplied by the caller.
A CI invocation must select only the reviewed virtual endpoint installed for
that evaluation. Silent audio and a different generated frequency fail the
independent measurement controls. No source test alone establishes native
device or human-listening acceptance.

Primary contracts:
[FFmpeg DirectShow input](https://ffmpeg.org/ffmpeg-devices.html#dshow),
[TerminateJobObject](https://learn.microsoft.com/en-us/windows/win32/api/jobapi2/nf-jobapi2-terminatejobobject),
[Windows job accounting](https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_basic_accounting_information).

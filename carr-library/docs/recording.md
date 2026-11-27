# Explicit local recording

This recording workflow reuses the public capture implementation reviewed in
PR123, head `28a4e5428a035af8d2f9edd89ebd4ae3802d1f5b`, adapted to the
current audio runtime, process runner, project assets and native worker.
It does not add another operation framework, project store or audio player.
The finite Pause/Resume segment loop reuses PR152 recording blob
`6ad1b2dda3d51bf2163763ee233d9ed874526c4e`, adapted to the current owned
processes, shared resource broker, monitoring and retained-cleanup boundary.

## Use in Resonance

Open **Audio Studio → Record microphone and manage takes…**. Opening the
window loads saved recording receipts only. **Refresh microphones** lists
DirectShow audio inputs without recording; even a single device remains
unselected. Select an input explicitly, give the recording a title and choose
the PCM sample rate, mono/stereo and duration limit. Then choose **Start recording**.

**Pause recording** asks the current capture segment to finish and waits for
its exact process family and any monitoring buffers to close. Only the confirmed
**Paused** state enables **Resume recording**. Resume rechecks the same selected
input and local runtime, then opens the next segment of the same take. The
recording task, resource reservation and quiet hold remain active while paused.
**Stop and save recording** also works while paused and saves retained frames
without opening another input. **Cancel unfinished recording** while paused
removes only that unfinished take.

Paused wall time is omitted from the saved audio. Segment frames are joined
without inserted silence, resampling or fades; review the joins before use.
Pause and Resume have native mnemonic access (Alt+H and Alt+M in this dialog),
remain reachable by Tab, and do not introduce global shortcuts.

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

This workflow selects a recording **input**. Optional dry monitoring uses the
existing explicit output selector; it closes during Pause and reopens only with
Resume. It is off by default. See [Dry recording monitoring](recording_monitoring.md).
Reader output selection and the Studio clip editor retain their own controls;
this capture workflow does not claim a full professional timeline editor.

## Runtime and limits

Capture requires an explicitly configured local Windows FFmpeg build with
DirectShow. The application's existing **Local audio runtime setup** flow
selects/verifies that runtime. A transferable application archive does not gain
an audio-runtime payload or any redistribution clearance through recording.

The service accepts PCM16 at 16, 24, 44.1 or 48 kHz and one or two channels.
A selected take is bounded to 1–21,600 seconds and at most 2 GiB, with stricter
caller audio budgets retained. The chosen duration must fit the PCM byte
allowance and actual free disk reserve before the input opens. FFmpeg diagnostic
pipes and staging inventory are bounded. A pauseable take permits at most 128
capture attempts and one hour of total paused wall time, further restricted by
the caller's wall-time, staging-file and byte budgets. Admission includes the
peak storage for the separate segments and joined WAV. The existing shared
broker reserves declared CPU/memory, disk allowance and the exact selected
input identity before a process starts; those declarations are cooperative
limits, not measured physical memory or an operating-system sandbox.
Windows capture starts suspended,
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
Controlled captures write `carr-recording/4`: exact contiguous frame ranges,
per-segment PCM hashes and the pause-omission notice accompany the original
whole-WAV hash. Optional retake and monitoring declarations retain their existing
meaning. Historical v1, v2 and v3 receipts keep their original strict shapes;
no existing receipt is rewritten to imply that it was segmented.

Project attachment uses the existing
`studio/projects/<project-id>/assets/<sha256>.wav` namespace and an asset cue
whose ID retains the recording ID. It does not rewrite source quotations or
replace a narration role automatically. Restored recordings do not authorize a
device or reactivate a runtime.

The complete backup integration validates copied recording receipts, actual PCM
and retake ancestry, verifies every v4 segment against its actual retained PCM,
coordinates the same `.recording.lock`, and preserves
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
failure. The added `tests/audio/test_recording_controls.py` uses actual FFmpeg
segments to check Pause/Resume, paused Stop/Cancel, exact joined PCM, bounded
monitoring, changed inputs/runtime, broker admission and direct or masked pending
cleanup. The existing recording UI and backup suites also exercise paused
lifetime and restored frame/retake receipts. These new cases are prepared and
**NOT_RUN** until executed against the composed source and installed wheel.
They do not qualify a microphone.

Native probes are separate and fail explicitly if their actual requirements
are unavailable:

| Probe | What it verifies | What it does not claim |
| --- | --- | --- |
| `scripts/windows_recording_smoke.py` | Real wx controls and keyboard dispatch; actual FFmpeg fixture Pause/Resume with exact segment/frame proof, Stop, retake, Cancel, Escape and modal progress | Physical microphone, listening or NVDA |
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

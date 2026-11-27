# Local microphone recording

The Audio Studio menu includes **Record microphone and manage saved inputs**.
Opening this dialog reads saved recording receipts. **Refresh microphones**
enumerates the configured FFmpeg DirectShow devices on Windows. Discovery does
not start recording, and even a single device remains unselected until the user
chooses it. No driver, engine, device, or network fallback is selected.

Choose the microphone, title, duration limit, sample rate and mono/stereo, then
choose **Start recording**. Recording waits for existing progress speech and
playback to confirm silence. Both the recording dialog and the main window hold
progress audio muted for the capture. Named native controls support keyboard
navigation. **Stop and save recording** sends FFmpeg its fixed quit command and
waits for the WAV header to be finalized. **Cancel unfinished recording**
terminates capture and removes incomplete output. Escape and Close wait for
cancellation cleanup before leaving an active recording.

Before opening the device, capture also waits for the library's shared resource
policy in `state/operations/resources.json`. It reserves one declared CPU slot,
a 512 MiB memory estimate, estimated staging/publication space and the exact
selected microphone identity. These are cooperative reservations, not measured
memory usage or a memory sandbox. The default resource wait is 60 seconds.
Stop or Cancel while waiting aborts without opening a microphone; after capture
starts, Stop finalizes PCM and Cancel discards incomplete output. The reservation
remains held through native child cleanup, verification and publication. A
device inventory changed during the wait requires fresh explicit selection.

The output is 16-bit PCM WAV at 16, 24, 44.1 or 48 kHz. The dialog permits up to
180 minutes; the service also enforces a finite duration, a 2 GiB upper byte
allowance, one active capture, bounded diagnostics and a free disk reserve.
Only the explicitly selected, enumerated DirectShow identity is passed to the
worker. Capture uses the existing cancellable audio process runner and copied
native environment, including removal of inherited `FFREPORT`. On Windows the
capture is attached to an owned Job Object with `KILL_ON_JOB_CLOSE`; if Windows
refuses lifetime protection, capture is stopped. This also terminates an
attached capture when its owner exits abruptly. Stop has a five-second
finalization allowance before incomplete output is discarded.

Completed WAV bytes are validated, hashed and published without overwriting
anything under `state/studio/recordings/<id>.wav`. A separate relative receipt
records the source device identity, configured FFmpeg hash, actual PCM format,
frame count, byte count, SHA-256 and completion reason. Saved input selection
and playback reverify the actual bytes. A late cancellation or progress
observer failure does not remove a committed recording. A receipt-write failure
reports the retained WAV path for recovery and does not claim successful capture.

Existing private-state backup includes these retained originals. Restore derives
paths from the new state root and does not approve a microphone. Callers attach
recordings to Studio through `RecordingService.get(id)` and the existing managed
input retention API, binding the returned `audio.sha256`; they do not grant
authority to an arbitrary saved path. Recordings are inputs, so generated-audio
exclusion does not discard them. This adds no second backup format or DSP engine.

## Verification boundaries

`tests/audio/test_recording.py` runs actual FFmpeg with a fixed synthetic source
adapter defined only in tests. It verifies PCM, graceful Stop, Cancel, collision
preservation, receipt identity and backup → removal of the original state →
restore at a different path → exact retained-byte verification. The production
service has no synthetic-source or arbitrary-input setting.

`tests/audio/test_recording_resources.py` uses the same actual FFmpeg test-only
source adapter to verify shared contention, pre-admission Stop/Cancel, timeout,
exact library/device binding and reservation lifetime through cleanup and
publication. It adds no physical microphone or Windows driver qualification.

`tests/ui/test_recording_dialog.py` executes the production callbacks for explicit
selection, deferred quiet admission, Stop versus Cancel, cleanup and progress
observer failures. These headless callback tests do not claim native keyboard,
physical microphone, Windows driver, acoustic quality or NVDA acceptance.
`test_windows_capture_is_killed_after_abrupt_owner_exit` uses actual Windows
processes and a Job Object with no microphone; it is skipped outside Windows.
Physical-device recording and human assistive-technology checks remain NOT_RUN
until performed on a selected Windows device.

`scripts/windows_recording_smoke.py` must run with the installed interpreter,
`-I -B`, real wx MainLoop and a staged FFmpeg build. It dispatches native keys
for explicit source selection, Start, Stop, Cancel and Escape, checks actual
retained PCM, and confirms cleanup. The probe's fixed synthetic source never
discovers or opens physical microphone hardware. Its existence is not a native
PASS claim; the platform job must execute it.

Device interface: <https://ffmpeg.org/ffmpeg-devices.html#dshow>.
Windows lifetime contract:
<https://learn.microsoft.com/windows/win32/procthread/job-objects>.

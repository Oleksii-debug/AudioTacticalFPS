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
cancellation cleanup before leaving an active recording. If native cleanup cannot
confirm silence, the dialog reports that state and retains its quiet reservation.
Stop can request another bounded cleanup attempt. Closing that pending dialog
keeps the native owner, staged bytes and capture admission reserved in the
background until silence is confirmed; worker completion alone does not release
them. No executor worker waits indefinitely for that confirmation.

The output is 16-bit PCM WAV at 16, 24, 44.1 or 48 kHz. The dialog permits up to
180 minutes; the service also enforces a finite duration, a 2 GiB upper byte
allowance, one active capture, bounded diagnostics and a free disk reserve.
The shared resource broker reserves one CPU slot, a declared memory and disk
allowance, and the exact microphone identity before capture. Stop while waiting
cancels admission; after capture begins it retains the graceful Stop-and-save
behavior. Reservations and staged bytes remain owned through uncertain native
cleanup. Receipt and orphan inventories stop at their item budget and support
cancellation while scanning.

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
Opening the dialog also reports canonical retained WAVs without receipts. They
remain unselected, unplayed and unverified; listing them performs no audio,
device or runtime probe.

Existing private-state backup includes these retained originals. Restore derives
paths from the new state root and does not approve a microphone. Callers attach
recordings to Studio through `RecordingService.get(id)` and the existing managed
input retention API, binding the returned `audio.sha256`; they do not grant
authority to an arbitrary saved path. Recordings are inputs, so generated-audio
exclusion does not discard them. The canonical complete-backup archive uses the
explicit [version 6 media envelope](BACKUP_FORMAT_V6.md) when typed media is present.

## Verification boundaries

`tests/audio/test_recording.py` runs actual FFmpeg with a fixed synthetic source
adapter defined only in tests. It verifies PCM, graceful Stop, Cancel, collision
preservation, receipt identity and backup → removal of the original state →
restore at a different path → exact retained-byte verification. The production
service has no synthetic-source or arbitrary-input setting.

`tests/ui/test_recording_dialog.py` executes the production callbacks for explicit
selection, deferred quiet admission, Stop versus Cancel, cleanup and progress
observer failures. These headless callback tests do not claim native keyboard,
physical microphone, Windows driver, acoustic quality or NVDA acceptance.
The shared owned-process acceptance covers process-family lifetime separately
from physical microphone capture.
Physical-device recording and human assistive-technology checks remain NOT_RUN
until performed on a selected Windows device.

The composed source and installed Windows evidence must be recorded for the
exact integration commit. Earlier donor results do not qualify this composition.
The resource regressions also exercise an actual competing process, CPU/RAM/disk
and selected microphone admission, Stop while waiting, and a live child whose
cleanup has not yet been confirmed.

`scripts/windows_recording_smoke.py` must run with the installed interpreter,
`-I -B`, real wx MainLoop and a staged FFmpeg build. It dispatches native keys
for explicit source selection, Start, Stop, Cancel and Escape, checks actual
retained PCM, and confirms cleanup. The probe's fixed synthetic source never
discovers or opens physical microphone hardware. Its existence is not a native
PASS claim; the platform job must execute it.

Device interface: <https://ffmpeg.org/ffmpeg-devices.html#dshow>.
Windows lifetime contract:
<https://learn.microsoft.com/windows/win32/procthread/job-objects>.

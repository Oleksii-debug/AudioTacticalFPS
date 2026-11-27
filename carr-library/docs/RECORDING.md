# Local microphone recording

The current desktop reuses the PR149 recorder with the existing local FFmpeg
resolver, WavePlayer, progress presenter, managed Studio documents and complete
backup. It does not introduce another speech service or player.

## User flow

Open **Audio → Record microphone and manage saved inputs**. An installed local
FFmpeg executable may be chosen explicitly. Refreshing devices only enumerates
DirectShow microphones. Even a single discovered device remains unselected.
Choose a device, title, maximum duration, PCM16 sample rate and channel count,
then choose **Start recording**.

Start first waits for the shared notification output to stop and for registered
content players to report a stopped state. It then admits the exact DirectShow
device through the existing resource broker. No capture occurs on dialog open,
on discovery, on restore, or while waiting for admission. An outstanding
recording quiet hold prevents a new content-playback handoff.

**Stop and save** sends only FFmpeg's fixed q command, waits for WAV finalization,
confirms that the complete owned process family has stopped, and verifies the
retained PCM. **Cancel** stops the owned family and discards unfinished staging.
A cancellation arriving after the original has been published cannot erase that
original. If receipt publication fails, its bytes remain an inert recovery input.

Saved recordings are listed from bounded typed receipts. **Listen** and **Use**
verify the selected PCM bytes. Use creates a separate ordinary managed Studio
project, copies an independent WAV into its audio/assets folder, retains the
small immutable recording receipt in the source reference, and opens the existing
Studio. The generated script is one explicit sound directive; no inference,
automatic transcription or shell interpretation occurs. Later derivative edits
retain that original asset even when they remove the directive.

## Retained state and recovery

Completed originals use:

- state/studio/recordings/<32-hex-id>.wav
- state/studio/recordings/<32-hex-id>.json, exact carr-recording/1 receipt

The receipt binds the selected device hash/name, the local FFmpeg hash, exact
PCM hash/size/rate/channels/frames, UTC creation time, explicit limits and
completion cause. It contains no executable path or device-open permission.

The typed complete-backup producer also retains final WAVs without receipts and
exact .pending-recording-<8-character>/capture.wav crash stages as opaque
originals. They may lack a complete WAV header. They are never automatically
played, rendered, interpreted as successful captures, or used to authorize a
microphone. A final receipt requires its actual matching PCM. Invalid receipts,
unregistered files, external aliases and links cause explicit review errors.

The producer is bounded to 10,000 recording/recovery identities, 30,000 retained
files and 32 GiB total; each PCM original is at most 2 GiB and each receipt at
most 64 KiB. It counts all directory entries before filtering exact transient
receipt stages. It acquires the recorder's actual .recording.lock underneath
the existing outer library writer, and complete backup rechecks its inventory
before and after copying. Active recording or uncertain native cleanup blocks
a snapshot. Restore retains bytes without selecting an executable or microphone.

## Native ownership and resource admission

The shared audio.windows_job.JobProcess attaches a private Windows Job Object
at process creation using JOB_LIST and an explicit standard-handle list.
Descendants therefore belong to the job before any child instruction runs.
There is no PID search, unowned launch window, machine-wide kill, shell or
fallback to a less protected Windows process.

close() is serialized and bounded. It confirms ActiveProcesses == 0 before
dropping the final job handle. A failed native stop, wait or handle-close retains
that exact owner for retry. The recorder transfers staging, its cross-process
capture guard, the existing ResourceLease and media quiet hold to that owner.
A returned worker future or closed modal does not by itself release the device.

Capture reservations use operation_id recording:<id> and exclusive device
dshow:<hash-of-DirectShow-alternative-identity>. One CPU slot, the configured
positive shared RAM pool and the explicit bounded output/staging allowance are
declared. These are cooperative allowances, not measured CPU/RAM/VRAM usage or
a memory sandbox. A bounded additional verification allowance follows the chosen
maximum duration. The broker's normal same-directory/process scope is preserved;
this is not a machine-wide claim about unrelated applications.

The native helper also supports the existing installer through explicitly
duplicated regular binary streams, retaining caller ownership. Capture itself
uses capped diagnostic pipes. FFREPORT and SSLKEYLOGFILE are stripped; raw
device diagnostics do not enter normal UI errors.

## Verification and integration

The source gates are test_capture_ownership, test_recording,
test_recording_backup, test_recording_dialog, the existing progress presenter
suite with recording-hold cases, and the current operations-facade suite.
The recorder tests use real FFmpeg with an explicitly injected synthetic lavfi
source, not a physical microphone.

scripts/windows_recording_smoke.py uses the installed package, real wx controls
and keyboard events, real FFmpeg synthetic PCM, Stop/Cancel/Escape, receipt
reopening and actual resource release. An optional independent executable hash
can be supplied with --expected-ffmpeg-sha256. Physical microphone capture, NVDA
speech and listening quality are separate NOT_RUN evidence until actually
performed. New port tests were NOT_RUN when the source checkpoint was prepared
during the executor outage.

The desktop integration hook is install_recording_actions(frame), after the
existing Audio menu. It registers the idempotent semantic command audio.recording
with no default shortcut. The recording facade presents receipt metadata only;
it does not label an unverified dashboard row as an output qualification.

No runtime is downloaded and no payload redistribution gate is changed. An
optional user-selected executable is not a redistribution authorization for a
sealed audio package.

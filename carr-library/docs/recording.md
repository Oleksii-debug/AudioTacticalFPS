# Local microphone recording and verified WAV export

The Audio Studio menu contains **Record microphone and manage saved inputs...**.
Opening this window only loads saved receipt metadata. It does not discover
devices, select a microphone, start playback, or begin a capture.

## Start, pause, resume, stop and cancel

1. Configure the existing local FFmpeg/FFprobe runtime in engine settings.
   Windows capture requires that selected FFmpeg build to support DirectShow.
2. Choose **Refresh microphones** and explicitly select the desired device.
   Identically named devices retain distinct DirectShow alternative identities.
   Even a single discovered device is never selected automatically.
3. Choose a title, duration allowance, PCM sample rate and Mono or Stereo.
   The recorder writes 16-bit PCM WAV. Available rates are 16000, 24000, 44100
   and 48000 Hz. The GUI permits 1–180 minutes; the selected duration and format
   must also fit the explicit 2 GiB byte allowance and current free disk space.
4. Choose **Start recording**. Playback and progress notices must first confirm
   silence. Capture stays blocked if an existing audio owner cannot do so.
5. **Pause microphone recording** requests a graceful segment close. The window
   reports **Paused** and enables **Resume microphone recording** only after the
   actual capture process and any monitoring buffers/handle have stopped. Resume
   reopens the same explicitly selected microphone as another segment. Paused
   wall time is omitted, with no inserted silence or unrequested effects. Review
   joins before using the take. There are at most 128 segments and one hour of
   cumulative paused time; the duration allowance counts retained frames.
6. **Stop and save recording** asks FFmpeg to finalize the current WAV. Its
   header, complete PCM frame count, limits and SHA-256 must verify before it
   becomes a saved original.
7. **Cancel unfinished recording** discards an unfinished take after its owned
   processes have stopped. Previously saved originals remain intact. Closing
   the dialog also cancels an active unfinished capture.

The recorder uses the desktop's existing worker and shared resource broker.
Its declared CPU, memory, selected input/output and peak disk allowances stay
reserved during Pause and until all actual native owners are quiescent. These
are cooperative limits, not measured device performance. A lower explicit
operator policy may refuse admission before any input opens. Stop while waiting
for admission opens no input.

If native process or output cleanup cannot yet confirm silence, the operation
retains the capture lock, staging, resource lease and media quiet holds. A UI
flag cannot release them. The existing cleanup owner retries the exact owned
resources. Closing cancels the operation while its worker and owner remain
responsible for cleanup; it does not announce that a still-owned microphone
has been released. After a successful publication, a late cancellation does
not erase the committed recording.

Windows discovery and capture opt into a private Win32 Job attached at process
creation. Normal render/conversion callers and the existing POSIX process
termination path remain unchanged. The recorder does not expose arbitrary
FFmpeg input arguments, a shell, an automatic network source or a fallback
device. Its selected executable must retain the same hash across discovery and
capture. Ambient FFREPORT is removed from these two recording operations.

## Explicit dry monitoring

Monitoring is off by default. Choose **Refresh monitoring outputs**, enable
**Enable dry input monitoring**, and explicitly select a currently identifiable
output in **Monitoring output (explicit selection required)**. Enumeration does
not open that output and never selects a default or fallback. Start remains
disabled until the selected monitoring output is present. Use headphones to
avoid microphone-to-speaker feedback.

Monitoring reads bounded complete PCM frames from the same private capture
WAV; it does not open a second microphone or alter the saved samples. It uses
the existing reviewed WinMM waveOut ABI, with at most four 50 ms buffers.
Slow output drops monitoring samples instead of delaying capture or growing
memory. Receipts distinguish submitted and dropped frames. Submission does
not prove audible delivery, latency, absence of driver loss or listening quality.
Pause closes this monitor handle too; Resume reopens the exact selected output.
Device displacement is an error, never permission to use another device.

## Saved originals and export

Saved recordings live under the active library's private
**state/studio/recordings** folder. A receipt records the relative WAV filename,
selected device identity, runtime hash, format, exact frame count and audio
hash. The optional controlled recording format adds contiguous retained-frame
ranges and their PCM SHA-256 values, pause/stop boundaries and monitoring
counts. The original v1 API/receipts remain readable, and capture without the
new controls still writes v1. Listing receipts does not read hours of PCM; preview, use and export
verify the selected original again.

**Listen to saved recording** starts only after current progress audio is
quiet. Pending capture, playback and export are bound to the exact LibraryApp
object, root path and actual directory identity from which this window opened.
Replacing that object, replacing a directory at the same path, changing its root
or closing the frame cancels queued work and prevents late results or
preview playback from being applied to another library.

Select a saved original and choose **Export verified recording to new WAV**.
Choose an unused .wav filename outside the private library. Export streams
bounded chunks into staging, rechecks the original receipt, file identity and
content, and uses the existing atomic no-clobber audio publication boundary.
An existing destination, a changed source, cancellation or changed destination
scope leaves that destination untouched. No microphone, speech model or
FFmpeg process is required to export a completed recording. Export does not
modify the original or load the entire WAV into memory.

The optional **Use selected recording** action appears only when an existing
caller provides a selection callback. The standalone menu does not imply that
a take has been inserted into a timeline or project.

## Backup and recovery

The existing complete library backup includes both committed WAVs and their
receipts even when generated audio caches are excluded. They are ordinary
private state, not rebuildable cache. No new backup schema or typed producer
framework is introduced. Lock files and pending capture directories are not
published as completed recordings. Both full-format4 and standalone state
recovery validate actual copied WAV geometry, contiguous segment coverage and
each segment hash. Missing/overlapping/gapped or forged ranges are rejected even
when an outer inventory is rewritten; old external paths are never followed.

After restoring to another library root, saved originals can be verified and
exported without the original device or runtime. Restore does not authorize a
microphone. WAVs retained after a receipt-write failure remain visible as
unverified originals; the program does not silently play or approve them.

## Verification boundaries

The source regression suites use actual FFmpeg with a test-only synthetic
sine input, real process pipes, and real backup/delete/restore/export. They
never request access to a physical microphone. Fault-injected cleanup tests
verify retained admission decisions but do not themselves establish native
Win32 behavior.

On Windows, require **tests/audio/test_windows_audio_ownership.py** in addition
to the ordinary recording, cleanup, export, UI and audio process suites. It
launches a real interpreter and descendant inside the owned job and checks
cancel, timeout, diagnostic overflow and an exited parent with a live child.
The installed **scripts/windows_recording_smoke.py** checks actual wx controls,
keyboard focus, synthetic FFmpeg capture, Pause/Resume, Stop/Cancel/Close and
exact WAV export. It also requires a real explicitly selected Windows waveOut
endpoint for the dry-monitoring sequence, checks its actual native handles and
buffer release, and binds the resulting retained PCM ranges. No endpoint is
mocked or skipped. An unavailable/incompatible output leaves that native gate
failed and unqualified.
Its file destination is a controlled fixture, not a claim about keyboard
operation of the OS file picker.

The Pause/Resume/monitoring successor is source-reviewed work until its new
portable, installed and actual native gates execute on the same revision.
Physical microphone capture, driver permissions, acoustic listening, monitoring
latency/quality and human NVDA operation require separate device evidence.
Synthetic FFmpeg and selected-output protocol evidence does not qualify those
properties. This workflow reuses the existing Studio and speech/audio services.

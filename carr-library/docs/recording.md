# Local microphone recording and verified WAV export

The Audio Studio menu contains **Record microphone and manage saved inputs...**.
Opening this window only loads saved receipt metadata. It does not discover
devices, select a microphone, start playback, or begin a capture.

## Start, stop and cancel

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
5. **Stop and save recording** asks FFmpeg to finalize the current WAV. Its
   header, complete PCM frame count, limits and SHA-256 must verify before it
   becomes a saved original.
6. **Cancel unfinished recording** discards an unfinished take after its owned
   processes have stopped. Previously saved originals remain intact. Closing
   the dialog also cancels an active unfinished capture.

If native process cleanup cannot yet confirm silence, the operation reports
that condition and retains the capture lock, staging and media quiet holds.
**Stop** requests another cleanup attempt. Closing the window retains that
owner in the background; it does not announce that a still-owned microphone
has been released. After a successful publication, a late cancellation does
not erase the committed recording.

Windows discovery and capture opt into a private Win32 Job attached at process
creation. Normal render/conversion callers and the existing POSIX process
termination path remain unchanged. The recorder does not expose arbitrary
FFmpeg input arguments, a shell, an automatic network source or a fallback
device. Its selected executable must retain the same hash across discovery and
capture. Ambient FFREPORT is removed from these two recording operations.

## Saved originals and export

Saved recordings live under the active library's private
**state/studio/recordings** folder. A receipt records the relative WAV filename,
selected device identity, runtime hash, format, exact frame count and audio
hash. Listing receipts does not read hours of PCM; preview, use and export
verify the selected original again.

**Listen to saved recording** starts only after current progress audio is
quiet. Pending capture, playback and export are bound to the exact LibraryApp
object and root from which this window opened. Replacing that object, changing
its root or closing the frame cancels queued work and prevents late results or
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
published as completed recordings.

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
keyboard focus, synthetic FFmpeg capture, Stop/Cancel/Close and exact WAV export.
Its file destination is a controlled fixture, not a claim about keyboard
operation of the OS file picker.

Physical microphone quality, driver/device permissions, live monitoring,
pause/resume capture, acoustic listening and human NVDA operation require
separate device evidence. This basic recording slice does not implement
multitrack clip editing, timeline insertion or a second transcription service.

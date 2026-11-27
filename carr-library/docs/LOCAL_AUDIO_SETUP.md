# Explicit local audio preparation

The installed application can keep its transferable payload free of FFmpeg while
preparing a separate private runtime from an archive the user has already chosen.
The setup wrapper reuses `packaging/prepare_ffmpeg_runtime.py` and the installed
`carr_library.audio.runtime` verifier. It does not download, select an engine,
change library preferences, or alter the installed application.

## Plan and prepare

Run `SetupLocalAudio.cmd` to print the current pinned URL, size, SHA-256, runtime
inventory, expected disk space, and proposed external destination. The default
performs no writes and does not run executable files. Obtain that exact archive
separately.

For a chosen local archive, review and confirm the plan:

```text
SetupLocalAudio.cmd --archive C:\Downloads\ffmpeg-pinned.zip --destination C:\LocalRuntimes\audio-new --library-root C:\Research\Library --interactive
```

`--prepare` is the explicit non-interactive equivalent. Preparation requires
Windows x64 and a new work folder outside the application and the supplied
library root. The default destination is a new folder under
`LOCALAPPDATA\Resonance\runtimes`. The archive remains in its original location.

The unchanged preparer verifies the exact archive and member bytes, retains the
approved notices, executes its fixed version/filter/license checks, and verifies
the completed runtime again. Space must cover both staging and the completed
runtime plus headroom. If preparation fails, keep any newly created external work
folder for inspection; existing directories and library files are never adopted
as a new destination or removed.

After success, use the current library's engine settings to select the printed
FFmpeg and FFprobe paths. Setup does not activate them automatically. This is a
private local preparation path, not permission to redistribute FFmpeg or a
replacement for the existing transfer-inventory guard.

## Integration and evidence boundary

The bundle builder must ship the fixed source tools
`tools/audio-setup/setup_local_audio.py` and
`tools/audio-setup/prepare_ffmpeg_runtime.py`, with `SetupLocalAudio.cmd` beside
the payload's `.venv`. Both helpers belong to the sealed inventory. A standard
generation contains that same payload-relative layout; no legacy installation
layout or alternate audio runtime family is introduced.

Portable tests use a tiny synthetic pinned archive to verify consent, no-write
planning, path/identity/disk refusals, exact delegation and final readback. They
do not claim real FFmpeg Windows qualification. Actual native preparation is
reported only when the existing preparer's fixed Windows probes succeed.

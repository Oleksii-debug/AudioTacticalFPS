# Local FFmpeg FULL setup

The source-only setup flow reuses this application's existing pinned FULL
runtime and archive preparer. It does not replace the audio engine, its identity
checks, the 17-package core lock, or the 53-package semantic lock.

After installing the core application, run **SetupLocalAudio.cmd**. Review the
archive URL, exact SHA-256, download size and new external output directory.
Only the exact confirmation **YES** starts preparation. Closing the prompt,
entering any other answer, or running the Python helper without an action flag
leaves the runtime unprepared.

The fixed runtime is FFmpeg 9.1 FULL. The archive is 264,268,459 bytes, with
SHA-256
f9a83be8f97b8989a223a15e4af234accd0842bacf7c126af5a3aae759785e48.
The helper checks the shipped source lock against the existing
carr_library.audio.runtime constants and retains its complete original
license/documentation inventory through prepare_ffmpeg_runtime.py.

## Plan, explicit download and an existing archive

From an installed application directory:

    .venv\Scripts\python.exe -I -B -X utf8 tools\audio-setup\setup_local_audio.py --output C:\LocalRuntimes\FFmpeg-Reviewed

This prints a plan. Add --interactive to require YES or --download to explicitly
download and prepare without a prompt. SetupLocalAudio.cmd uses the interactive
form and accepts --output and --archive arguments.

To reuse the exact already-downloaded archive without any network access:

    SetupLocalAudio.cmd --archive C:\RetainedInputs\ffmpeg-9.1-full_build.zip --output C:\LocalRuntimes\FFmpeg-Reviewed

The output directory must be new and outside the application installation.
Without --output, the helper proposes a new timestamped directory under
LOCALAPPDATA\Resonance\runtimes. It checks the space needed for the archive,
independent preparation, final retained files and a free-space reserve.

Setup creates no model or voice registrations and never changes a library.
When byte verification succeeds, open **Audio Studio → Production → Choose
FFmpeg executable** and choose runtime\ffmpeg.exe in the reported directory.
FFprobe remains beside it; retain the whole directory, including licenses and
documentation. Existing application runtime checks still apply when audio is
actually used.

## Boundaries and failure recovery

Only the fixed publisher archive is requested, with a size/hash bound and
reviewed HTTPS redirect hosts. Ambient proxy credentials and SSL key-log
settings are not used. The desktop does not invoke this downloader. No user
document, recording, credential or API profile is sent.

Setup does not execute FFmpeg or claim native audio qualification. It does not
download speech/music weights. A failed or interrupted operation does not
change engine settings or replace an existing destination. Retain any created
directory for review; an intact downloaded archive can be reused with --archive
and a new output directory.

A managed installation holds its existing application lease while this
entrypoint runs, so an update cannot replace its executing setup helper.

The core bundle includes only these source helpers and their lock, never the
FFmpeg archive or runtime. This local acquisition is separate from permission
to redistribute FFmpeg and linked components. The existing delivery and
managed-update target producers continue to refuse both sealed audio layouts
until package-specific redistribution evidence is admitted.

## Provenance and acceptance

The user flow is adapted from canonical commit
966e5ccf892243ead6942c635af5fbab47e72551, while retaining the current FULL
runtime architecture. The canonical LGPL-shared runtime, its alternate module
and downloader are not installed in parallel.

The source checkpoint was executed on Linux/Python 3.12.14: all 28 focused
setup cases and 20 bundle cases passed through the strict no-skip runner. They
use synthetic archives and the real current preparer/bundle verifier. A separate
plan-only CLI run read the actual FULL source lock, reported its exact pins and
left the proposed output directory absent. No external download or native
FFmpeg execution was claimed by these checks.

The Windows source and managed-update lanes require the focused setup suite.
The full-bundle lane also executes the exact sealed helper with the installed
Python in isolated mode while its outbound firewall rule is active, and requires
the plan's external destination to remain absent. That installed Windows gate
has not yet executed on this candidate. Real interactive acquisition and native
runtime execution remain separate evidence.

The download loop revisits its 600-second budget after each single buffered
read. An in-flight socket read retains its configured 15-second timeout; this
is a bounded deadline with that possible read overrun, not an exact
600.000-second interruption guarantee.

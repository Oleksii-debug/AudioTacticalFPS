# Private Windows audio runtime

The offline bundle can include the reviewed FFmpeg Windows executable,
its original GPLv3 license, build notes and a versioned provenance manifest.
No download occurs when the application opens. The optional build argument is
`--audio-dir`; omit it for a library-only package.

This integration reuses the existing runtime preparer and pinned identities
from commit `d14bc613ff0bff47d9df4b2fd552ec11c7c4655f`. It does not introduce a
second renderer. The integrated Audio Studio, voice adapters and CLI use the
same discovery and validation function.

## Pinned payload

The selected archive is
[Gyan FFmpeg 9.0.2 essentials](https://github.com/GyanD/codexffmpeg/releases/download/9.0.2/ffmpeg-9.0.2-essentials_build.zip),
114768076 bytes, SHA256
`60f467265b1e312373dbcd92200c2618a74850f98d3d078e94296bb3fa2047ba`.
The exact executable is 105423872 bytes, SHA256
`3256173f3f8bffd7df12227c68adf68025edb1832273a9530688a7bb1ed8edec`.
The license and build notes have independent pinned hashes in
`carr_library.audio.runtime.ARCHIVE_MEMBERS`. Source revision:
[`946fcce07b6dcd0331c8cc609192aeff5e1924f8`](https://github.com/FFmpeg/FFmpeg/tree/946fcce07b6dcd0331c8cc609192aeff5e1924f8).

`packaging/prepare_ffmpeg_runtime.py` reads an already acquired exact archive,
extracts only the three admitted members, verifies their size and hash, and
publishes a new directory. `--verify-native` additionally executes Windows
version, required-filter and license checks. A Linux preparation records
Windows execution as NOT_RUN. Binary verification is separate from actual
speech, mixing, playback, accessibility and listening acceptance.

## Integrity and discovery

The builder validates the runtime before and after copying. The enclosing
bundle manifest records every file and the `bundled_audio_runtime` flag. The
installer and application reject missing, modified, extra, unannounced or
redirected payloads. A bad bundled runtime does not fall back silently.

Explicit user configuration, or the installation-controlled `CARR_FFMPEG`
setting, takes precedence. Otherwise the application discovers the sealed
runtime relative to its Python environment, then an existing executable in
absolute local PATH directories. It never searches the working directory or
relative PATH entries. Executable paths must be ordinary local files without
symlinks, reparse points, remote shares or alternate data streams. Script and
project files cannot supply executable paths.

No binaries, voice recordings, credentials or model weights are stored in the
public source repository. The runtime is for the owner's private package;
the upstream license and notices remain included. This component does not
provide a code-signing certificate, signed update service or license server.

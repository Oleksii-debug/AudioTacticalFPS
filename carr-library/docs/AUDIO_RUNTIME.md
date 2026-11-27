# Private offline FFmpeg runtime

This optional supplement supplies the exact local FFmpeg, ffprobe and ffplay
executables used by the audio adapters. It adds no Python dependency. It is
separate from the application's immutable original documents and generated
audio. It does not implement another renderer or provider.

## Reviewed input

- Official FFmpeg download page: <https://ffmpeg.org/download.html> links to the
  FFmpeg-Builds/BtbN provider. FFmpeg itself publishes source, not these binaries.
- Provider release: `autobuild-2026-11-26-12-56`, asset `795900712`.
- Archive: `ffmpeg-n9.0.2-982-g836df4e858-win64-lgpl-shared-9.0.zip`.
- Archive bytes: `77787475`; SHA-256:
  `f7ba27e7986a2d0835881e742a682dec17896f6dc2cdcf9e596f043d2763d894`.
- Runtime version string: `n9.0.2-982-g836df4e858-20261126`. This describes the
  exact provider build, not a claim that it is an unmodified FFmpeg 9.0.2 release.
- FFmpeg source: `836df4e85894b757debe268f84ffc08c57623a73`.
- Build recipes: `d05871a37b5fc80e00714c708edec35c7b9ebfb0`.
- LGPL shared variant; embedded configuration enables version 3, disables
  GPL-only x264/x265/rubberband and nonfree fdk-aac. Codecs and filters must still
  be checked by executing this exact Windows build.

`packaging/audio-runtime/source-lock.json` contains all original file sizes and
hashes, the publisher checksum identity, exact source archive URLs/hashes and
supplemental notices. All original upstream archive files are kept. Additional
FFmpeg license texts, its exact source archive and the exact build recipes are
included in the private runtime. The build-recipe dependency index records
upstream source declarations; it does **not** claim a complete independently
reproduced source bundle for every linked third-party library. Public binary
redistribution clearance is not claimed. Preserve the private deployment
boundary and the original licenses.

The source lock's two false review flags are an actual outstanding rights gate,
not incorrect values to flip after a green execution test. The verifier,
preparation/attachment reports and native smoke receipt now retain those
booleans independently of execution. The pinned owner-local profile rejects
missing, non-boolean or promoted clearance fields even if a source-lock hash
is re-pinned. A later public profile requires a reviewed source/notice bundle
and an explicit policy change; no existing installed local runtime is blocked
by network or entitlement checks.

The pinned dependency-source-index.json enumerates 127 build recipes, not a
resolved list of shipped components. Reconcile the exact enabled configuration,
actual imported DLLs and statically linked libraries (the build uses
`--pkg-config-flags=--static`), including transitive dependencies and their
notices/corresponding source/build instructions. Disabled recipes must not be
counted as shipped. Verify user replacement of shared LGPL libraries, applicable
reverse-engineering terms and distribution notices before approving a public
package. The exact FFmpeg and build-recipe source archives alone do not close
this gate. Neither a private ZIP nor a successful Windows smoke closes it.

## Explicit acquisition and preparation

The application never calls either setup helper and never silently downloads a
runtime, voice or model. Run on a packaging machine with an existing absolute
parent directory:

```text
python packaging/acquire_audio_runtime.py --output ABS_INPUT_DIRECTORY
python packaging/acquire_audio_runtime.py --output ABS_INPUT_DIRECTORY --download
python packaging/prepare_audio_runtime.py --archive ABS_ARCHIVE_PATH --sources-dir ABS_INPUT_DIRECTORY --output ABS_NEW_RUNTIME_DIRECTORY
```

The first command prints a plan without accessing the network. The second
downloads exactly three public hash-locked inputs. Existing correct downloads
are reused, and existing wrong files are preserved with an error. A truncated,
oversized, changed or HTTP-redirected download cannot become a completed input.
Preparation is fully offline and executes no downloaded code. It requires a
fresh output directory, rejects unsafe/linked/duplicate archive members, copies
the exact reviewed payload, verifies every output byte and checks executable
and DLL PE machine headers. Its report says `native_execution: NOT_RUN`.

## Integrated bundle layout and startup

Place the prepared directory at `BUNDLE/audio-runtime`. Include every file,
including `audio-manifest.json`, in the existing outer `bundle-manifest.json`
SHA-256 inventory and set `bundled_audio_runtime` to the JSON boolean `true`.
Never modify a previously delivered archive or an installed `.venv`. The final
assembler must use the same source lock committed in its source tree.

The current Audio Studio reaches this exact verifier through
`carr_library.audio.runtime.verify_bundle_audio` and `find_ffmpeg`. The adapter
returns the verified absolute `bin/ffmpeg.exe` path, so the renderer does not
need a PATH change. Explicitly selected absolute executables remain a separate
local configuration. A changed, missing, mixed or unannounced bundled runtime
fails closed.

`packaging/build_windows_bundle.py --audio-dir ABS_RUNTIME` copies the complete
verified nested inventory and rechecks it before publication. The bootstrap
installer checks the nested inventory; the installed verifier binds its source
lock, all native files, notices and the original archive provenance. The older
flat experiment layout retains its existing verification for compatibility;
new default CI preparation uses the reviewed shared build above.

The generic `bundled_audio_tools(bundle_root=None)` helper remains available
for explicit ffprobe/ffplay adapters. Integrity inspection never establishes
native execution or rights clearance.

## Required native Windows acceptance

Add the supplied FFmpeg, ffprobe and ffplay executable paths to the existing
outbound-block firewall stage **before** running the installed application and
its audio workflows. Keep the Python/OCR blocks and all existing tests.

```text
INSTALLED_PYTHON -I -B scripts/windows_audio_runtime_smoke.py --bundle ABS_BUNDLE
INSTALLED_PYTHON -I -B scripts/windows_audio_smoke.py
```

The smoke checks the installed library's runtime verification, actual binary
version, eleven required filters and PCM/FLAC/MP3 encoders. It then processes a
real synthetic stereo WAV in a Unicode/spaced path, checks exact frame count
and measured filter response, actually encodes FLAC and MP3 and probes their
duration/channels, and verifies unchanged original bytes. It emits nine passed
cases only after execution. On a non-Windows host it returns `NOT_RUN` and exit
77. Do not treat that result as a green native gate.

This runtime smoke does not establish speaker playback, recording, speech
quality, neural inference, MusicGen, long-duration scene continuity, human
listening assessment or NVDA acceptance. Those belong to the integrated audio
workflows and their separate evidence.

The second command exercises this application's actual Windows SAPI,
FFmpeg rendering, literal narration, durable resume, native controls and the
Windows credential vault. Run it using the installed environment after clearing
any explicit FFmpeg environment override. The integrated gate blocks outbound
access for the three FFmpeg tools and the SAPI PowerShell helpers. Actual device
capture/playback, neural inference, human listening and manual NVDA remain
separate acceptance work.

## Transfer and verification boundary

Current source and build-recipe archives do not complete the transitive source,
notice and replacement review. The delivery archive helper rejects either audio
runtime layout before staging; changing an outer clearance flag cannot bypass
that gate. Local preparation, installation and native checks remain available.
A distributable profile needs an explicit reviewed package-specific decision.

Focused source regressions:

```text
python scripts/ci_check.py tests/packaging/test_audio_runtime.py tests/packaging/test_audio_acquisition.py tests/audio/test_audio_runtime.py tests/packaging/test_windows_bundle.py tests/packaging/test_delivery_archive.py
```

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

Use the existing Windows bundle builder and native-runtime supplement first,
then attach the prepared audio payload to a fresh output:

```text
python packaging/add_audio_runtime.py --bundle ABS_NATIVE_SUPPLEMENTED_BUNDLE --audio-runtime ABS_PREPARED_AUDIO --output ABS_NEW_AUDIO_BUNDLE
```

The adapter copies only sealed installation inputs, preserves the exact source
commit and tree, verifies the application wheel contains this runtime verifier,
and adds every audio file plus its inner manifest to the existing outer seal.
It publishes the acceptance manifest last and checks that it still owns the
output directory around every publication move. Failure cleanup preserves any
replacement directory and unrelated files. Previously delivered bundles,
installed environments, caches and private library data are never inputs to
this copy. The existing source-wheel verifier records the exact commit's tree;
the installer recognizes the optional audio flag and checks its complete layout.
The installed native dependency check verifies the pinned inner identity before
loading extension modules, while accurately reporting audio execution NOT_RUN.

`audio.ffmpeg.select_audio_tool` is the common studio/CLI/voice-registry selector.
An explicit absolute local executable wins. Otherwise it verifies the bundled
runtime and returns its exact path on Windows x64. The verifier discovers only
interpreter-prefix bundle roots and binds the fixed source lock, every file and
the inner manifest to the outer inventory. It does not change PATH or execute
a tool during discovery. The existing installed-tool fallback is used only when
no bundle runtime exists; it ignores relative PATH entries and the current
directory. Invalid explicit selections and corrupt declared bundles fail with
an error and cannot silently switch to a different executable. Automatic paths
are verified again when the studio enumerates voices or starts a render.

The CARR11 `activate_bundled_audio_runtime` compatibility API remains available,
but the studio and CLI use explicit verified paths and do not call it. No DLL
is loaded into Python, and no network or credential setting is changed.

`bundled_audio_tools(bundle_root=None)` returns the exact verified absolute
paths for adapters that require explicit executables. `verify_audio_runtime`
supports cross-platform inventory inspection and never proves native execution.
A missing, changed, unannounced or ambiguously located runtime fails closed.
An ordinary source installation without this optional supplement is unchanged.

## Required native Windows acceptance

Add the supplied FFmpeg, ffprobe and ffplay executable paths to the existing
outbound-block firewall stage **before** running the installed application and
its audio workflows. Keep the Python/OCR blocks and all existing tests.

```text
INSTALLED_PYTHON -I -B scripts/windows_audio_runtime_smoke.py --bundle ABS_BUNDLE
INSTALLED_PYTHON -I -B scripts/windows_installed_audio_smoke.py --bundle ABS_BUNDLE
```

The smoke checks installed-package identity, automatic studio/CLI selection,
the installed library's runtime verification, actual binary
version, eleven required filters and PCM/FLAC/MP3 encoders. It then processes a
real synthetic stereo WAV in a Unicode/spaced path, checks exact frame count
and measured filter response, actually encodes FLAC and MP3 and probes their
duration/channels, and verifies unchanged original bytes. It emits eleven passed
cases only after execution. On a non-Windows host it returns `NOT_RUN` and exit
77. Do not treat that result as a green native gate.

The separate installed studio smoke verifies the same sealed bundle and uses
the common direct tool selector without changing PATH. Its fifteen named cases
cover installed source identity, actual SAPI synthesis, stereo production,
measured duration/clipping, chapters/subtitles/stems, immutable source, complete
output reuse, literal narration, Windows Credential Manager isolation, native
Studio controls/focus/voice list/preview and saved project readback. The existing
source-only `windows_audio_smoke.py` entry point remains separate and does not
claim to execute the sealed delivery runtime. Preserve the existing native DLL
activation wrapper and block both the selected media tools and SAPI PowerShell
outbound before the installed probes. Neither probe establishes manual NVDA
acceptance or human listening quality.

This runtime smoke does not establish speaker playback, recording, speech
quality, neural inference, MusicGen, long-duration scene continuity, human
listening assessment or NVDA acceptance. Those belong to the integrated audio
workflows and their separate evidence.

Focused source regressions:

```text
python scripts/ci_check.py tests/packaging/test_audio_runtime.py tests/packaging/test_audio_acquisition.py
python scripts/ci_check.py tests/packaging/test_audio_bundle.py tests/audio/test_tool_selection.py
python scripts/ci_check.py tests/audio/test_narration_plan.py tests/ui/test_audio_studio.py tests/audio/test_project_backup.py
```

## Literal source narration

New Studio projects and imported book text use literal narration by default.
Headings, speaker-like labels and bracketed phrases remain spoken text. Form
feeds identify source segments, and every spoken chunk is bound to exact
code-point offsets in the retained input. Select Structured production script
explicitly to interpret role, pause, music or output directives. Changing the
interpretation invalidates previous rendered results.

Version 2 Studio projects save this choice. Existing version 1 independent
scripts keep script interpretation; version 1 source-linked projects reopen as
literal narration. Project backup and restore accept both versions without
rewriting the original source or selecting executable paths from project data.

## Reused source identity

The acquisition, preparation, lock, notices and native probe originate from
CARR11 commit `d2d5dc045787e0dde803fd0ef434e7885da2284b`. The supplemental
adapter and publication regressions originate from
`6198258a31b600cac95f99f5387d59a1362b56a4`. The bounded interpreter-prefix
discovery correction and safe PATH search reuse the relevant functions from
`1386bf9c9abe36544f2641a15a70434ff0ad1e9d`. Integration adapts only the tool
selector, existing bundle metadata, installer gates and their acceptance probes;
it does not introduce a second production renderer or acquisition pipeline.
Literal narration and the separate installed studio probe also reuse that
existing CARR11 implementation through reviewed commit
`10d31b848bc116f043c12c3d747168b195eb0e13`; its output-directory ownership
correction is retained alongside the newer sealed tool selector.

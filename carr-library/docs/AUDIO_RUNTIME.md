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

Build the base bundle with `--source-commit` and the final dependency SBOM,
then add the existing native DLL supplement. The source verifier derives both
the exact commit and its tree from the checked application wheel and local Git
objects. Attach audio only into a new output directory:

```text
python packaging/add_audio_runtime.py --bundle ABS_NATIVE_BUNDLE --audio-runtime ABS_PREPARED_AUDIO_RUNTIME --output ABS_NEW_COMPLETE_BUNDLE
```

The helper verifies the original sealed bundle, current installer, current
runtime verifier in the application wheel and every audio input byte. It copies
only sealed files, preserves source identity, verifies the result and publishes
the acceptance manifest last. It never collects an installed `.venv`, cache or
private database. Preserve previously delivered archives and installations.

The shared speech and studio factories in `runtime_settings.py` use
`bundled_audio_tools()` for their default FFmpeg and ffprobe paths. The verifier
discovers only interpreter-prefix bundle roots; it ignores the current
directory, arbitrary environment paths and PATH for discovery. It binds the
exact source lock, every file and the inner manifest to the outer installation
inventory. Explicit local executable selections still take precedence.
These factories do not change PATH or save the discovered paths in user engine
settings. An invalid announced bundle fails before ordinary local discovery.

`bundled_audio_tools(bundle_root=None)` returns the exact verified absolute
paths for adapters that require explicit executables. `verify_audio_runtime`
supports cross-platform inventory inspection and never proves native execution.
`activate_bundled_audio_runtime()` remains available for callers that explicitly
need the verified bin directory prepended to their Windows process PATH.
A missing, changed, unannounced or ambiguously located runtime fails closed.
An ordinary source installation without this optional supplement is unchanged.

## Required native Windows acceptance

Add the supplied FFmpeg, ffprobe and ffplay executable paths to the existing
outbound-block firewall stage **before** running the installed application and
its audio workflows. Keep the Python/OCR blocks and all existing tests.

```text
INSTALLED_PYTHON -I -B scripts/windows_audio_runtime_smoke.py --bundle ABS_BUNDLE
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

Focused source regressions:

```text
python scripts/ci_check.py tests/packaging/test_audio_runtime.py tests/packaging/test_audio_acquisition.py tests/packaging/test_audio_bundle.py tests/audio/test_bundled_runtime_settings.py
```

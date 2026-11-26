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

Place the prepared directory at `BUNDLE/audio-runtime`. Include every file,
including `audio-manifest.json`, in the existing outer `bundle-manifest.json`
SHA-256 inventory and set `bundled_audio_runtime` to the JSON boolean `true`.
Never modify a previously delivered archive or an installed `.venv`. The final
assembler must use the same source lock committed in its source tree.

Use the reviewed attachment helper after the exact-source core bundle and its
native supplement have been verified. It copies only sealed files to a fresh
output and writes the enclosing acceptance manifest last:

```text
python packaging/add_audio_runtime.py --bundle ABS_CORE_NATIVE_BUNDLE --audio-runtime ABS_VERIFIED_RUNTIME --output ABS_NEW_BUNDLE
```

```python
from carr_library.audio_runtime import activate_bundled_audio_runtime
activate_bundled_audio_runtime()
```

Call this once during the common desktop/CLI startup before constructing an
audio adapter. The verifier discovers only interpreter-prefix bundle roots;
it ignores the current directory, arbitrary environment paths and PATH for
discovery. It binds the exact source lock, every file and the inner manifest
to the outer installation inventory. Only then, on native Windows x64, it
prepends the verified `audio-runtime/bin` directory to this process's PATH.
Explicitly selected absolute executable paths still take precedence in the
existing adapters. Existing PATH entries are preserved. No DLL is loaded into
Python, no network setting is changed and no API credential setting is touched.

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

The second command tests the installed studio, actual Windows SAPI synthesis,
FFmpeg rendering, literal narration, durable resume, native controls and the
Windows credential vault. It requires the bundle's `.venv` interpreter and its
installed package, identifies `source_commit` and `source_tree` from the sealed
bundle, and checks that default tool discovery selects the supplied FFmpeg.
Its JSON has `windows_execution: PASS` only after all fifteen named cases pass.
Block outbound access for the actual SAPI PowerShell helpers as well as the
three FFmpeg executables before this command. Neural inference, audio devices,
native-speaker listening and manual NVDA remain separate acceptance work.

## Selected studio integration

The runtime reuses the existing PR99 studio in the PR97 integration. Imported
books and ordinary text use literal narration with exact source spans; structured
production directives require an explicit mode selection. Saved projects retain
their interpretation, and legacy projects are migrated according to their
recorded source relationship. Existing original files stay unchanged.

The source-install studio smoke remains `scripts/windows_audio_smoke.py`.
The separate installed-wheel smoke above requires this sealed audio bundle and
cannot silently substitute a source checkout or another FFmpeg installation.
Voices, model weights and private external helpers are separate reviewed assets;
the runtime supplement does not establish their installation or quality.

Default audio-tool lookup inspects absolute PATH directories itself, excludes
the current directory and relative entries, and selects native executable
names. This avoids Python 3.11's Windows `shutil.which` current-directory
precedence. An explicit CLI FFmpeg selection is also passed to local voice
discovery, so Flite and the renderer use the same selected installation.

Focused source regressions:

```text
python scripts/ci_check.py tests/packaging/test_audio_runtime.py tests/packaging/test_audio_acquisition.py tests/packaging/test_audio_bundle.py tests/audio/test_runtime_integration.py tests/audio/test_narration_plan.py tests/ui/test_audio_studio.py
```

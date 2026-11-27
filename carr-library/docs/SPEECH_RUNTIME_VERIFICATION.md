# Required speech runtime coverage

Updated 2026-11-27. This is the test-platform mapping for the speech cases
originally combined in `tests/speech/test_runtime.py` at source
`00176ea1cb56ae3ce6287b93cd1d4f7f2571c9fc`. Moving a case does not
qualify a release: acceptance requires successful runs on the final integrated
commit, including every job in the relevant workflows.

## Finite, required case mapping

| Required platform and test module | Cases | Actual boundary covered |
| --- | ---: | --- |
| Native Windows: [test_runtime.py](../tests/speech/test_runtime.py) | 13 | Twelve shared validation, process, source-preservation and publication cases, plus one actual installed SAPI voice and PCM synthesis case. |
| Ubuntu 24.04: [test_flite_runtime.py](../tests/speech/test_flite_runtime.py) | 6 | Actual Flite PCM, distinct voices, volume, source invariance, no overwrite, cancellation, chunking/budget and rejection of unsupported controls. |

The six required Flite cases are:

1. `test_actual_local_pcm_speech_and_three_distinct_voices`
2. `test_actual_volume_reduction_and_source_invariance`
3. `test_no_overwrite_existing_audio`
4. `test_cancel_synthesis_leaves_no_partial_audio`
5. `test_actual_chunked_synthesis_and_budget`
6. `test_unsupported_native_controls_do_not_silently_apply`

Their measured PCM, volume, source, no-clobber, cancellation and budget
assertions are preserved. Missing FFmpeg, its Flite filter or the required
`awb`, `rms`, `slt` and `kal16` voices fails the dedicated module.

Both [main Windows](../../.github/workflows/carr-windows.yml) and
[V3](../../.github/workflows/carr-v3.yml) call the same mandatory
[Linux Flite workflow](../../.github/workflows/carr-flite.yml). Neither caller
can succeed if that job fails. The Windows source and installed gates retain
the common/SAPI module, require exactly thirteen successful cases, and require
the actual SAPI case by name in installed acceptance. V3's broad Windows
discovery excludes only the dedicated Flite module, whose six cases remain
required by the Linux job in that same workflow.

The Linux job selects only the dedicated module. It does not collect or waive
the native Windows SAPI case. There is no blanket skip, expected-failure
allowance, changed product capability or relaxed source/installed gate.

## Why the selected Windows build does not supply Flite

The reviewed Windows runtime is the exact LGPL shared build declared by
[audio-runtime/source-lock.json](../packaging/audio-runtime/source-lock.json):
`n9.0.2-982-g836df4e858-20261126`, archive SHA256
`f7ba27e7986a2d0835881e742a682dec17896f6dc2cdcf9e596f043d2763d894`.
Its retained [build configuration](../packaging/audio-runtime/provenance/build-configuration.txt)
does not enable `libflite`. The pinned upstream
[FFmpeg configure source](https://github.com/FFmpeg/FFmpeg/blob/836df4e85894b757debe268f84ffc08c57623a73/configure)
disables external libraries unless selected and makes the Flite filter depend
on `libflite` and threads. The corresponding
[build recipe](https://github.com/FFmpeg-Builds/FFmpeg-Builds/tree/d05871a37b5fc80e00714c708edec35c7b9ebfb0)
does not supply that missing enablement.

This is a capability of the selected build, not a claim that Windows or an
LGPL-labelled build can never support Flite. FliteProvider's discovery is
retained unchanged. Its handling of the five upstream voice names and the
intentional nonzero voice-list exit matches the pinned upstream source.

The official [FFmpeg Flite filter documentation](https://ffmpeg.org/ffmpeg-filters.html#flite)
also describes the required build option. Ubuntu's official
[FFmpeg package](https://packages.ubuntu.com/noble/ffmpeg) uses
[libavfilter9](https://packages.ubuntu.com/noble/libavfilter9), whose declared
dependencies include libflite1. These primary sources were checked on
2026-11-27. Package declarations are not substituted for runtime verification.

## Linux execution and retained evidence

The required job installs Ubuntu 24.04's `ffmpeg` and `libflite1` packages
explicitly. Before tests it verifies the selected executable, build option,
actual filter and all four tested voices through the production provider.
It records the event source commit/tree, relevant source hashes, CPython
version, package versions, FFmpeg version/configuration and hashes of the
executable, libavfilter and Flite libraries actually selected from those
packages. The job fails if a different executable shadows the selected
Ubuntu binary.

It runs:

```sh
python -B scripts/ci_check.py tests/speech/test_flite_runtime.py
```

The existing runner's full offline socket policy, deadline, process-tree
cleanup, bytecode policy, JUnit failure/skip rejection and XFAIL/XPASS rejection
are unchanged. The job additionally requires the exact six named passed cases
and the runner's six-pass, zero-failure, zero-skip verdict.

The `carr-flite-<run-id>-<attempt>` artifact contains only text and JSON:
source identity, package/runtime identity, capability evidence, the complete
strict-suite log and its exact-case summary. It contains no executable,
shared library, model, voice asset or synthesized audio. The CI runtime is
not added to the Windows delivery or treated as distribution clearance.

## Qualification boundary

A passing Linux Flite run qualifies these six real-process regression
scenarios for its recorded Linux runtime. It does not establish Windows
Flite availability, human listening quality, pronunciation quality for the
135-language catalogue, private-engine/model inference, device playback or
manual NVDA accessibility. Those remain separate evidence requirements.
The Windows SAPI case likewise demonstrates its measured PCM contract, not
a completed listening or screen-reader audit.

# Audio implementation evidence — 2026-11-26

Environment: Linux x86_64, Python 3.12.14, FFmpeg/ffprobe
7.1.5-0+deb13u1, installed GPL-enabled Debian build. No runtime binary is included
in this change. FluidSynth 2.4.4 was discovered as an installed library.

## Required source tests

Run from `carr-library`:

```sh
PYTHONPATH=src python -m pytest -q tests/audio
```

Final local audio suite: **53 passed in 7.04 seconds**, also verified with plain
`python -m pytest` and no inherited PYTHONPATH. Combined speech/audio after the
shared local-path correction: **96 passed, 41 subtests passed in 7.75 seconds**.
These overlapping runs are not added together.

Actual FFmpeg tests cover measured channel placement (left/center/right), precise
cue onset and silence, speech/music sidechain attenuation, peak limiting, EQ
response, gate suppression, echo tail, loudness normalization and measured
LUFS/true peak, silent true peak, fades/crossfade channel isolation, conversion,
decoded Opus sample-rate/frame correctness, input preservation, no overwrite,
scope/symlink/protocol/playlist rejection, cancellation, explicit budgets,
capability inspection, and RIFF/RF64 validation. A one-hour delayed-cue test
isolates the child process and checks the regression in peak memory.

Windows UNC shares, device paths, mapped network drives and reparse-point
ancestors are rejected before file processing; raw share/URL cases are tested
before any filesystem lookup. Implicit current-directory executables are not
runtime discovery candidates. The optional music worker launches by trusted
absolute path under Python isolated mode, independent of CWD or PYTHONPATH.

Music tests distinguish actual FluidSynth synthesis from optional-model
contract tests. They exercise manifest pins, separate licenses, traversal,
pickle rejection, changed asset bytes, offline unavailability, cancellation,
runtime/asset cache identity and actual PCM generation with the selected
installed SoundFont. On systems without that optional runtime/font, its test
asserts the unavailable state; that does not establish actual synthesis there.

Neural MusicGen weights and runtime were **not installed**, downloaded or run.
The neural adapter's unavailable/validation boundaries are tested. Actual neural
generation, its musical continuity, benchmark and listening acceptance remain
**NOT_RUN**. Native Windows/NVDA, output devices and listening quality were not
tested by these Linux fixtures.

## Actual long-duration processing

All below outputs were actually rendered, fully sample-count/peak/SHA256 checked,
and then removed from temporary storage. They are processing fixtures, not
claims of hours of original neural music or speech.

| Fixture | Actual duration and format | Bytes | Wall time | Parent / child peak RSS |
| --- | --- | ---: | ---: | ---: |
| 600 explicit repetitions of a 60-second synthetic silent segment | 10 hours, 8 kHz mono PCM16; 288,000,000 frames | 576,000,114 | 15.140 s | 18,240 / 34,272 KiB |
| 600 explicit repetitions of a 60-second synthetic silent segment | 10 hours, 32 kHz stereo **RF64**; 1,152,000,000 frames | 4,608,000,114 | 74.404 s | 18,524 / 36,992 KiB |
| Prepared synthetic cue at 3599.5 seconds, otherwise silence | 1 hour, 8 kHz mono PCM16; 28,800,000 frames | 57,600,114 | 1.387 s | 19,416 / 33,972 KiB |
| 180 explicit repetitions of one actually synthesized 60-second FluidSynth score | 3 hours, 24 kHz stereo PCM16; 259,200,000 frames | 1,036,800,114 | 22.915 s | 23,944 / 38,656 KiB |

The delayed-cue fixture first exposed a genuine memory problem in
`aresample(first_pts=0)`: child peak RSS 229,548 KiB. Replacing that prefix with
pull-driven silence plus concat reduced measured child RSS to 33,972 KiB while
preserving the exact cue onset and duration. No memory figure represents an
entire desktop application or neural model.

RF64 output was checked from its actual `RF64` container header and `ds64`
64-bit data size, not simulated only by a tiny header fixture. Its SHA256 was
`2b4131fe71d570a906646111b0fd14c225ebfff80ee0e1849310baaef58bdc7d`.
The 8 kHz ten-hour output SHA256 was
`b27c69a72d454e874cf3cd6c112b51f904f383eeae45c3983dd95b02ca2e297d`.

The actual MIDI test used the already installed `TimGM6mb.sf2`, documented by
Debian's `timgm6mb-soundfont` copyright file as GPL-2, with SHA256
`c5378b62028c920cb11e4803327983fee2f2cdff5dc89c708e39da417e51c854`.
Its 60-second score used the explicit minor-piano pattern, tempo 90, seed 19.
The three-hour repeated output peak was 0.0220947265625 and SHA256
`afa14c518657fe63a577abd441f0d4fda8fc0a1454e878fd49c564bdc979f9b2`.
The SoundFont was not copied into the repository, package or public artifact.

An actual loudness test caught another defect: FFmpeg `loudnorm` may raise its
internal rate to 192 kHz, causing a later sample-count trim to shorten the
timeline. The adapter now restores the selected sample clock before trimming;
the regression checks both exact duration and measured output LUFS.

Independent review also found master echo tails and tempo changes were being
silently cropped/padded to the pre-effect inferred duration. The renderer now
keeps separate pre-master and final sample counts. Actual echo-tail and tempo
regressions verify inferred duration and intentionally fixed output duration.
Crossfade routing is checked sample-for-sample against isolated rendered channel
controls; a Fourier measurement inside the fading window was rejected because
envelope spectral leakage is not evidence of channel leakage.

These results verify the local batch renderer and selected synthetic music
fixtures. They do not close the full V3, Windows, model, human listening or NVDA
acceptance gates.

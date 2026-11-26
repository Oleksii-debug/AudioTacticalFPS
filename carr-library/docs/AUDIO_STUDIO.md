# Local prepared-track Audio Studio

The studio turns a typed scene plan and prepared local WAV files into real audio:
one stereo master, a mix for each scene, and separate voice, music and effect
stems for the groups present in that scene. Narration engines supply their WAVs
through the separate voice adapter. The studio itself does not synthesize speech
or generate neural music, and it never changes a retained book or quotation.

## Rendering contract

`carr_library.audio.studio.render_project` accepts an approved input directory,
a sequence of `Scene` objects and a separate output directory. A trusted local
FFmpeg executable must be supplied explicitly. There is no automatic download,
cloud fallback, paid API, shell invocation or user-supplied filtergraph.

A `Track` supplies a relative WAV path, its start in milliseconds, kind
(`voice`, `music` or `effect`), gain from 0 to 1, pan from -1 to 1, and optional
fade-in and fade-out durations. Mono pan uses equal-power left/center/right
placement. Stereo tracks retain their two channels and use balance; this is not
a claim of arbitrary multichannel routing or binaural spatial audio.

A `Scene` supplies a stable ASCII ID, its tracks and its total duration in
milliseconds. Music can be ducked under the actual voice signal. A scene with
no tracks is an exact pause. Inputs that extend beyond a scene or have an
oversized fade are rejected rather than silently clipped. Inputs are not
looped automatically. Millisecond boundaries round up to the next output sample.

Prepared inputs support uncompressed PCM16 WAV, mono or stereo, at 8–192 kHz.
Tracks are resampled to the chosen output rate. The initial output format is
stereo PCM16 WAV, with RF64 selected by FFmpeg when RIFF size limits require it.
Compressed export, live playback, recording, EQ/reverb editors, neural music,
LUFS measurement and perceptual quality acceptance are separate work packages.

The fixed FFmpeg graph reuses `aresample`, `volume`, `pan`, `afade`, `adelay`,
`apad`, `atrim`, `amix`, `sidechaincompress` and `alimiter`. Limiting disables
automatic makeup gain and uses 0.95 full-scale headroom. A limiter is not a
certification of listening quality or loudness compliance. Native parameters
were checked against the installed FFmpeg filter and WAV-muxer help.

## Durable projects and cancellation

The output folder contains `studio-project.json`, a `scenes` directory and
content-keyed master WAVs. Its checkpoint binds the typed settings, relative
input paths and hashes, FFmpeg executable hash, output format, frame counts,
scene stems and output checksums. The active plan records chapter boundaries
in exact frames and becomes complete only after its continuous master is
verified and the checkpoint is flushed.

Every completed scene is independently checkpointed. Restarting the same plan
checks those files before reusing them. Changing one input or scene setting
invalidates its dependent scene; other completed scenes remain reusable. A
cancelled job stops its actual FFmpeg process and retains already accepted
scenes. A corrupt cache is regenerated into new paths; the damaged file and old
masters remain available for inspection. No existing audio is overwritten.
Malformed checkpoints are reported and preserved, not silently reset.

`RenderLimits` sets the sample rate, cumulative input and output byte budgets,
wall-clock time and scene/track budgets. Defaults allow up to 64 GiB of input,
64 GiB of retained project outputs, 24 hours of execution, 32 tracks per scene
and 10,000 scenes. These are configurable resource budgets, not a ten-minute
audio-duration ceiling. The renderer checks estimated space before starting a
scene or master and watches actual output growth during native processing.
Choose a new output folder or deliberately manage old project versions when
retained renders consume the budget; successful outputs are never deleted
automatically to make space.

Final assembly streams the scene WAVs through FFmpeg's concat demuxer. Python
validates bounded headers and hashes files in 1 MiB blocks; it does not load a
multi-hour waveform into RAM. The RF64 reader checks exact 64-bit sizes and
sample count. A temporary hard-link playlist uses generated local filenames,
with the concat demuxer in safe mode and input protocols restricted to `file`.

## Local file boundary

Inputs stay inside the selected directory and must be ordinary unlinked WAV
files. Traversal, URLs, Windows device names, reparse points, symlinks, hardlinks,
truncated WAV data and invalid numeric settings are rejected. Source hashes are
checked again after native rendering. Output is staged, verified and published
without replacement. A cooperative writer lock prevents two application jobs
from updating the same project at once.

The executable and local parent directories must be trusted and stable. The
protocol allowlist and typed argv prevent this API from requesting network
media or arbitrary commands; they are not an operating-system sandbox for a
malicious FFmpeg executable or a hostile filesystem administrator. No binaries,
voice samples, model weights or user recordings are redistributed by this module.

## Executed acceptance

Run the actual tests from the `carr-library` source directory with a trusted
FFmpeg on PATH, or its absolute path in `CARR_FFMPEG`:

```console
python -m pytest -q -o xfail_strict=true tests/audio/test_studio.py
```

The development environment uses Linux, CPython 3.12.14 and FFmpeg
7.1.5-0+deb13u1, a Debian GPL-enabled build. The tests use generated PCM tones
and silence, not real speech or neural music. They measure three distinct
left/center/right roles, channel isolation, voice timing, music ducking, effect
placement, limiter headroom, an exact silent pause and unchanged input hashes.
They also execute real-process cancellation, checksum recovery, selective scene
regeneration, no-clobber publication and an actual one-hour stereo 8 kHz silence
render with restart reuse. This is a long streaming fixture, not one hour of
original music generation.

A separate real FFmpeg fixture forces RF64 on a short output and validates its
64-bit size table. A greater-than-4-GiB render, full-duration neural generation,
Windows execution, audio-device playback, NVDA and human listening acceptance
have not been established by these tests. They remain explicit acceptance work.

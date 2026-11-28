# Optional local instrumental engines

The existing Audio Studio renderer accepts four explicit music engines:
procedural, MusicGen, FluidSynth and ACE-Step. Every installed engine uses the
same scene cue, chunk checkpoint, cancellation, MIDI/provenance publication and
project cache contracts. A missing installation raises a clear error. There is
no model, SoundFont or runtime download and no provider fallback.

## Current acceptance boundary

The FluidSynth shared-library backend actually rendered a 1.25-second mono
PCM16 recording at 8 kHz, exactly 10,000 frames, on Linux with installed
FluidSynth 2.4.4 and TimGM6mb. FFmpeg performed conversion and the existing
renderer published the MIDI and provenance companions. Recorded WAV SHA256:
616fe147c997d3684810dad8c26099032763472f27c7afeca5f70eebc2137adf.
This was a short functional probe, not a listening or native Windows test.

After recovering the execution environment, the focused adapter, original music,
manifest and production suites passed: **66 tests and 7 subtests**, Python 3.12,
21.47 seconds. Actual FluidSynth coverage rendered a 16.125-second score in
three chunks, preserved its first accepted chunk across cancellation/resume,
measured nonzero unclipped PCM, matched the procedural renderer's MIDI, and
rendered a production scene. Mutating the SoundFont invalidated a completed
project before cached output reuse. These tests used the installed native
library and sound bank, with no download or installation.

FluidSynth CLI/Windows behavior, human listening, instrument release continuity
and every ACE-Step model inference remain **NOT_RUN**. Existing successful
FFmpeg regression evidence belongs to the preceding separate fixes.

No ACE-Step checkpoints or compatible Torch installation were available.
The ACE-Step worker tests use clearly named fake modules to exercise the fixed
protocol, offline checks and returned-file validation. Passing these tests
cannot establish model inference or music quality. Native dependencies are
trusted installation components; the Python audit hook is defense in depth,
not an operating-system sandbox for hostile native code.

## Explicit scene selection

Use structured script mode to interpret music directives:

    [music engine=fluidsynth model_manifest=fluidsynth/manifest.json mood=calm tempo=90 seed=42]
    Narrator: Read these words.

    [music engine=ace-step model_manifest=ace-step/manifest.json mood=suspense tempo=90 seed=42]
    Narrator: The next scene begins.

Manifest paths are relative to the selected production project's models
directory. They never refer to URLs. Literal narration continues to speak
bracketed text as text.

The model manifest remains schema_version 1. Its files object maps every
installed regular file to its actual SHA256, and revision identifies an exact
asset revision or SHA256. license_accepted records the user's explicit review.
Inventories, manifest hashes, paths, licenses and byte budgets are rechecked
before rendering or reusing a completed project. Templates below describe
settings only; they are not valid installed manifests and contain no approved
asset hashes.

## FluidSynth SoundFont profile

The profile engine is fluidsynth and its license describes the selected
SoundFont. settings contains:

| Setting | Meaning |
| --- | --- |
| api_source_revision | 5500b60788287ceb7297f628f75c488d2720f61a |
| backend | cli or library, explicitly selected |
| executable | Manifest-listed FluidSynth CLI path, for cli |
| library | Manifest-listed FluidSynth shared library or DLL path, for library |
| runtime_version | Exact installed 2.x version, checked by the worker |
| runtime_license | Reviewed runtime license and attribution |
| soundfont | Manifest-listed .sf2 or .sf3 asset |
| output_rights_notice | Reviewed SoundFont output conditions and attribution |
| timeout_seconds | Explicit per-chunk worker limit, default 3600 |

The adapter renders the existing deterministic original score. It uses the
same final MIDI as the procedural FFmpeg synthesizer, with four-bar windows
and one preceding bar of instrument release context. Reverb and chorus are
disabled, and FFmpeg trims each result to the planned sample count. The
shared-library worker uses the public sample-clock MIDI/file renderer API;
it never opens an audio or MIDI device. Its interpreter is the running
application's trusted Python with only standard-library imports.

The CLI form is adapted from the existing public PR103 implementation.
Both backends isolate each render in the existing cancellable subprocess
runner and keep accepted chunks for resume. SoundFonts have independent rights:
FluidSynth's LGPL license does not establish the SoundFont's license or output
rights. The local test asset was TimGM6mb, with the installed Debian notice
identifying GPL-2.0-only; that asset is not included in the repository.

Reviewed public API source:
https://github.com/FluidSynth/fluidsynth/tree/5500b60788287ceb7297f628f75c488d2720f61a

## ACE-Step 1.5 profile

The profile engine is ace-step. Set trusted_weights only after reviewing
the exact checkpoints, included Python model code and installed dependencies.
The complete installation must be materialized without links under the
manifest root. settings contains:

| Setting | Meaning |
| --- | --- |
| source_revision | ca1e85fe9430179831e6bc6be790c332190a3866 |
| python | Manifest-listed interpreter executable |
| package_dir | Completely inventoried Python package directory |
| model_name | acestep-v15-turbo, acestep-v15-base or acestep-v15-sft |
| device | cpu or cuda, explicitly selected; no speed promise |
| threads | Integer 1–64, default 2 |
| inference_steps | Integer 1–100; default 8 for turbo or 32 otherwise |
| chunk_seconds | Per-inference duration 10–120 seconds, default 30 |
| timeout_seconds | Per-chunk lifetime 1–86400 seconds, default 3600 |
| output_rights_notice | Reviewed model output rights and attribution |

The installation includes checkpoints/vae, checkpoints/Qwen3-Embedding-0.6B,
and the selected DiT checkpoint. Three critical upstream entrypoints are
checked against fixed reviewed SHA256 values, in addition to the whole profile
inventory. The worker runs Python with -I -S -B, disables executable startup
site hooks, and adds only the declared package directory.

The adapter calls AceStepHandler.initialize_service and
GenerationParams/GenerationConfig/generate_music from the pinned upstream.
Instrumental mode, [Instrumental] lyrics, BPM, key, 4/4 meter, deterministic
seed, disabled LM reasoning and PCM16 WAV output are explicit. The initializer's
two exact reviewed hooks are restricted to checking local files and rejecting
model-code mismatches. This prevents its normal automatic download/code-sync
paths and does not install an unused language model.

Each result must be one successful local PCM16 WAV inside staging. A receipt
is checked against the actual sample count/rate/channels before FFmpeg
conversion. Generated files, file count, diagnostics and process lifetime are
bounded. The native runtime and dependencies still require independent review.

The scene duration has no ten-minute ceiling. ACE-Step operates in bounded
independent chunks with one prompt and successive seeds; seamless neural
continuation, transition quality and native stems are **not accepted** by this
adapter. A final shorter chunk generates the upstream minimum ten seconds and
is trimmed to the exact requested sample count. The provenance describes this
assembly and keeps listening_acceptance as NOT_RUN.

Reviewed primary source, including the public request/result dataclasses and
initializer download behavior:
https://github.com/ace-step/ACE-Step-1.5/tree/ca1e85fe9430179831e6bc6be790c332190a3866

Public implementation reused for interface integration:
https://github.com/Oleksii-debug/AudioTacticalFPS/commit/9efc5a7f439f63fa2c822771d081eaba0221ccdb

## Reproduce the focused tests

    python -m pytest -q tests/audio/test_music_adapters.py tests/audio/test_music.py tests/audio/test_model_manifest.py tests/audio/test_production.py --import-mode=importlib

The two actual FluidSynth tests require the installed Linux fixture and skip
explicitly when it is absent. The remaining worker tests exercise synthetic
contract doubles without weights. Native Windows runtime and real ACE-Step
acceptance remain separate gates.

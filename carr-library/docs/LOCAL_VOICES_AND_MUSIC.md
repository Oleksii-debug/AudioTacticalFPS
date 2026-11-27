# Local voices and music

This implementation adds working local adapters and a resumable local score
generator to the same application. It does not download models, install voices,
send source text to a service, or silently select another engine.

## Installed voices

`audio.engines.default_registry()` discovers Windows SAPI 5 voices through
System.Speech, the separate 32-bit SAPI 5 view when available, installed eSpeak
NG, and the Flite voices included in a selected FFmpeg build. `voices()` returns
usable profile records; `capabilities()` also explains missing engines.

The shared synthesis boundary is:

```python
registry.synthesize(
    text, destination_wav, voice="sapi5:default", language="en",
    rate=0, pitch=0, volume=1, cancelled=is_cancelled,
)
```

Text is data. SAPI receives a fixed encoded PowerShell program and JSON on stdin;
it calls `Speak(string)`, not `SpeakSsml`. Flite reads a fixed local text filename;
the script never becomes a filtergraph or a shell argument. eSpeak and Piper
receive text on stdin. All outputs are verified PCM16 files, staged beside the
destination and published without replacing an existing file. Cancellation
terminates the owned worker process tree. Individual speech requests have
bounded text, PCM, diagnostics and time budgets; the production scheduler splits
long scripts into requests instead of limiting an entire book.

SAPI exposes integer rate and volume. eSpeak exposes rate, pitch and volume.
Piper exposes length scale and volume. Basic Flite voices expose volume and
English speech; they explicitly reject another language or unsupported native
prosody. The studio's separate audio effects can change rendered audio, which
does not constitute native pronunciation or expressive speech synthesis.

Legacy SAPI 4 is explicitly unavailable pending a tested compatible helper.
Native SAPI 5 has a Windows-only real-speech regression. Linux tests of its fixed
command/data boundary are not Windows speech or NVDA acceptance.

## dordiai-voices

The adapter is named **dordiai-voices** and uses an independently written client
for its observed JSON-lines worker interface. No source, private voice catalogue,
reference recording, model or add-on binary is copied into this repository.

The separately installed runtime supplies `nvda_worker.py`, its embedded Python,
models and `models/voices/catalog.json`. A versioned local installation manifest
must declare `protocol: nvda-worker-jsonl-pcm24k-v1`. Voice names and languages are
read from the installed JSON catalogue; there is no fixed voice count.

One reference request identifies the chosen profile, language, original text and
numeric controls. The worker returns bounded JSON lines containing base64
`chunk` or `pcm` data, at 24 kHz mono PCM16. The client requests `next` between
chunks, handles loading status, checks alignment and total bytes, rejects mixed
completion/data messages, and streams PCM straight to disk. A bounded writer
thread keeps cancellation responsive even when a model worker stops reading a
large Unicode request. Cancel/drop requests are followed by terminating the
owned process when necessary; partial audio is not published.

The wrapper denies Python network and child-process audit events and removes
host credential variables from the worker environment. A backend requiring its
own subprocess currently fails explicitly. This is a trusted local-runtime
integration, not a sandbox for malicious Python, native libraries or pickle
files. Installed interpreter/system dependencies outside the manifest directory
remain trusted software. Actual packaged dordiai-voices inference and native
language/listening acceptance have not been executed by these protocol tests.

## Model installation manifests

`LocalModelManifest` reads only an explicitly selected local JSON file. Schema 1
records the engine, model identity, exact 40/64-character revision, license,
acceptance flags, relative asset paths with SHA256 values, and engine settings.
Remote URLs, traversal, alternate streams, symlinks/reparse points, duplicate JSON
fields, nonfinite values, moving revisions and changed assets fail closed.
Verification streams file bytes and supports cancellation and a configurable
asset-byte budget.

Piper requires manifest-listed ONNX/configuration files. dordiai-voices and
MusicGen also require a complete inventory of regular files under the local
installation directory; unlisted files or linked subdirectories are rejected.
Voice cache identities reverify pinned assets, so modification or revoked
manifest acceptance cannot be hidden by an already rendered audio cache.

Acceptance flags record an explicit installation decision. A hash establishes
identity; it is not a security review, a license grant or a voice-consent grant.
Nothing in this module downloads or prepares an unreviewed model automatically.

## Working procedural music

`render_music(...)` returns the generated WAV, MIDI, source manifest and exact
frame count. `generate_music(...)` returns the WAV path for production jobs.
Parameters include duration, mood, tempo, optional key, seed, source gain,
sample rate, cancellation, progress and an explicit disk budget. Supported moods
are calm, suspense, bright, hopeful, sad, neutral, energetic and dark. Sample rates are 8000, 16000,
22050, 24000, 44100, 48000 and 96000 Hz.

The procedural composer produces original note events with repeatable seeded
decisions, chord progressions and melody variations. FFmpeg supplies sine
synthesis, envelopes, mixing and PCM encoding; this is a basic synthesized
instrumental score, not neural music, a recording, sampled orchestral instruments
or a claim of professional composition quality. Motifs and chord patterns recur;
recorded audio is not looped. A standard MIDI file preserves the note events for
later use in a compatible tool. An optional verified FluidSynth/SoundFont renderer now reuses that exact MIDI.
See [local music adapters](LOCAL_MUSIC_ADAPTERS.md) for its installation contract
and actual short native-library evidence.

Four-bar chunks are independently rendered and hash-checked. A stable JSON
journal is saved after every accepted chunk. Cancellation preserves completed
chunks; restart verifies and reuses them, while a corrupted chunk is regenerated.
A file lock protects one output from concurrent writers. Changed score settings
require a new destination. The final concat uses internal numeric filenames and
file-only protocols, streams from disk, and enables RF64 for WAV data beyond the
ordinary RIFF size limit. Matching companion MIDI/provenance files from an
interrupted publication can be recovered without overwriting different files.
There is no ten-minute ceiling: disk budgets and engine chunk sizes are separate
from total project duration.

## Optional MusicGen

MusicGen is a first-class selectable optional local engine for the personal
noncommercial profile. Its adapter follows AudioCraft's local-directory
`MusicGen.get_pretrained`, `generate`, and `generate_continuation` interfaces at
source revision `896ec7c47f5e5d1e5aa1e4b260c4405328bf009d`.

The installation must contain separately reviewed, hash-pinned
`state_dict.bin`, `compression_state_dict.bin`, a compatible Python/runtime and
complete offline text-encoder cache. The manifest must declare the exact
reviewed source revision, selected device and executable, CC-BY-NC-4.0 weight
terms, personal noncommercial use and explicit trusted-checkpoint acceptance.
The fixed worker enables offline framework modes, strips host credentials and
denies Python socket/network and subprocess audit events before loading the
model. Missing dependencies fail; there is no remote download fallback.

Generation uses bounded 25-second chunks with up to five seconds of preceding
audio as continuation context. Each cached chunk records its parent checksum, so
a changed preceding chunk invalidates dependent continuations. PCM normalization
and resampling are delegated to FFmpeg. A full multi-hour waveform is never
passed to MusicGen or loaded in RAM. Model loading occurs per uncached chunk in
this adapter; throughput, GPU compatibility and continuity require measurement
on the chosen installation. Five seconds of continuation does not guarantee
musical coherence over hours.

No AudioCraft weights or runtime are present in the validation environment, and
no real MusicGen inference has been run. The adapter's existence and fail-closed
manifest tests do not establish successful neural generation. ACE-Step has an optional adapter for the pinned 1.5 interface, with reviewed
local manifests and auto-download/code-sync disabled. Its synthetic worker
contract tests do not establish model inference; see
[local music adapters](LOCAL_MUSIC_ADAPTERS.md).

## Reproducible evidence

The focused tests run with:

```text
python -m pytest -q tests/audio/test_model_manifest.py tests/audio/test_engines.py tests/audio/test_music.py
```

The actual Linux validation used FFmpeg 7.1.5, which enumerated local Flite awb,
kal, kal16, rms and slt voices. A real English speech sentence produced nonzero
24 kHz mono PCM16. Actual procedural checks cover deterministic waveform/MIDI,
seed changes, exact sample counts, source gain/headroom, a 601.125-second score,
cancel/restart, checksum corruption repair, no overwrite and recovery of matching
publication companions. A sparse RF64 fixture tests large-file metadata only;
it is explicitly not many-hour audio generation or neural evidence. SAPI actual
speech runs only on Windows. Listening quality, physical audio devices,
Ukrainian/Slovak neural pronunciation and NVDA acceptance remain separate gates.

## Primary interface references

- [Microsoft SpeechSynthesizer](https://learn.microsoft.com/en-us/dotnet/api/system.speech.synthesis.speechsynthesizer?view=netframework-4.8.1)
- [Microsoft speech rate](https://learn.microsoft.com/en-us/dotnet/api/system.speech.synthesis.speechsynthesizer.rate?view=netframework-4.8.1)
- [FFmpeg audio filter documentation](https://ffmpeg.org/ffmpeg-filters.html)
- [eSpeak command-line interface](https://espeak.sourceforge.net/commands.html)
- [Piper official CLI source](https://github.com/OHF-Voice/piper1-gpl/blob/main/src/piper/__main__.py)
- [Pinned AudioCraft MusicGen interface](https://github.com/facebookresearch/audiocraft/blob/896ec7c47f5e5d1e5aa1e4b260c4405328bf009d/audiocraft/models/musicgen.py)
- [Pinned AudioCraft local model loading](https://github.com/facebookresearch/audiocraft/blob/896ec7c47f5e5d1e5aa1e4b260c4405328bf009d/audiocraft/models/loaders.py)
- [Pinned AudioCraft weight license](https://github.com/facebookresearch/audiocraft/blob/896ec7c47f5e5d1e5aa1e4b260c4405328bf009d/LICENSE_weights)

## Explicit score key and canonical mood preservation

The current typed script accepts, for example:

```text
[music mood=hopeful key=F# tempo=120 seed=7]
Narrator: Exact source text remains unchanged.
```

The finite tonic set is C, C#, D, D#, E, F, F#, G, G#, A, A# and B.
Minor modes are selected by suspense, sad and dark; other moods select major.
The same tonic reaches the procedural notes, exported MIDI, optional
FluidSynth score, ACE-Step keyscale and MusicGen prompt. An unsupported value
is rejected before native engine selection. This is a validated control, not
a guarantee that an unqualified neural model will follow the requested key.

Omitting key preserves the prior C-major/A-minor behavior, serialized cue fields
and direct music-work settings. Existing production receipts still bind the
exact composer source hash: a source update is not permission to accept a
receipt for a different renderer. Explicit keys enter the saved script, typed
plan, cue cache identity and generated provenance. A completed render replays
only under its current exact identities; a changed key regenerates its music.
Managed project and typed complete-backup checks retain the exact source and
selected key.

The canonical hopeful mood now passes through the existing canonical-project
review into this score model. The old negative fixture that used hopeful as an
unsupported current mood instead supplies an actually unknown mood; a separate
positive fixture protects the newly represented control.

This change adds 23 current-family cases plus one canonical-import case.
They include actual FFmpeg PCM/MIDI pitch and frame checks, all eight moods,
exact replay, strict refusal and typed backup. ACE-Step protocol fixtures run
an explicitly synthetic local child; the MusicGen request fixture substitutes
only model generation before actual FFmpeg conversion. No model weights,
neural inference quality, hardware speed or listening acceptance is claimed.
Their execution remains pending at authoring.

The current Sound and Clip controls use explicitly retained local PCM assets
on the SFX, ambience and music tracks. The historical door/click/chime/noise
generator API is not represented as equivalent to those user assets. V3-12
marks a local sound-effect generator optional; this correction adds no second
generator or process family.

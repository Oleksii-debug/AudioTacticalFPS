# Local audio runtime and verified studio controls

This module is the audio backend of the existing Universal Research Library. It
does not introduce a second application. The persistent production director owns
scene jobs, role mapping, retries, cache identity, subtitles and project state;
the audio runtime executes typed, local operations and returns measured artifacts.

## Working operations

`carr_library.audio.AudioRuntime` provides:

- `inspect(path)`: actual FFprobe format, duration, rate, channels and exact
  frame count where the container supplies it.
- `mix(tracks, output, duration=..., sample_rate=24000, channels=2)`: real
  FFmpeg mixing, sample-aligned start times, trims, gain, equal-power pan or
  explicit left/right routing, preserved stereo routing, fades, speech-driven
  background ducking, per-track and master effects, and a non-boosting limiter.
  An empty track list plus a positive duration produces real digital silence.
- `convert(source, output, ...)`: range extraction, rate/channel conversion,
  typed effect chains and the locally available WAV, FLAC, MP3, Ogg Vorbis,
  Opus, AAC/M4A/M4B, AIFF or WMA encoder. Codec availability is discovered from
  the installed build. Opus requires an explicit 48000 Hz output selection.
- `concatenate(scenes, output, ...)`: streaming join of matching PCM WAV scenes
  through an internally generated, path-validated concat manifest. Thousands of
  scenes do not become thousands of simultaneously open decoder/filter inputs.
  Heterogeneous scenes must first be converted to matching PCM.
- `measure()` / `measure_loudness()`: measured integrated LUFS, true peak,
  loudness range and threshold. Silence has no finite integrated loudness; it
  is reported as silence with null values, not a fabricated measurement.
- `render_music(MusicSpec(...), output)`, `render_sound(name, output)` and
  `export_midi(spec, output)`: actual local generation described below.

Every completed audio operation returns `AudioInfo` including its actual path,
codec, duration, channels, rate, byte size and SHA256. Inputs remain unchanged.
No output overwrites an existing file. Staging is private and on the same
filesystem as the destination; a completed, validated file is linked into place
without replacement. Cancellation and failure discard incomplete staging files.

```python
from carr_library.audio import AudioRuntime, Effect, MusicSpec, Track

runtime = AudioRuntime(project_directory, input_roots=(selected_source_directory,))
bed = runtime.render_music(
    MusicSpec(duration=20, mood="suspense", tempo=90, seed=237), "bed.wav")
result = runtime.mix(
    [Track(left_voice, pan=-1),
     Track(right_voice, start=4, routing="right"),
     Track(bed.path, role="music", gain_db=-12, routing="stereo")],
    "scene.wav", duration=20,
    effects=(Effect("loudness", target_lufs=-18, peak_db=-1),))
measurement = runtime.measure(result.path)
```

## Effects and precise limitations

`effect_controls()` returns parameter defaults, numeric ranges and units for
native accessible controls. Reorder or bypass typed `Effect` values; unsupported
effect names or controls are rejected, including controls on bypassed effects.
User text is never accepted as an FFmpeg expression.

Available adapter controls include parametric EQ, low/high shelves, high/low
pass, notch, gate, compressor, limiter, FFT denoise, de-click, de-clip, de-esser,
single echo, four-reflection room ambience, chorus, tempo, pitch, stereo width,
gain and dynamic loudness normalization. A chain is enabled only when the local
FFmpeg build supplies its required filters. A graphic EQ can use multiple typed
parametric bands.

The room control is a four-reflection echo model using FFmpeg `aecho`; it does
not claim calibrated reverberation time, an acoustic room simulator or
convolution IR support. Delay provides one echo, not unconstrained recursive
feedback. Gate exposes the actual attack/release/reduction controls; an
unsupported hold control is rejected. This backend renders offline previews;
it does not claim real-time DSP, plugin hosting, recording-device acceptance or
human listening quality. Delay/room tails are retained only inside the selected
timeline duration. Tempo changes use conversion before assembling a master
timeline. Pitch and tempo are separate controls.

Dynamic loudness normalization is followed by actual output measurement.
Requested targets and measured values are distinct. Very short, silent or
strongly constrained signals need not reach a selected integrated target.

## Ordered effects and saved presets in the desktop

Open **Audio Studio → Convert audio and apply effects**, select a local source,
then set its range and output sample rate, channel count, PCM bit depth and
encoding bitrate. Opus requires an explicit 48000 Hz selection. Lossy bitrate
applies to MP3, AAC, Opus and WMA; Ogg Vorbis uses the adapter's existing quality
setting. A codec can reject an unsupported rate or depth before publication.

**Edit ordered effect chain and presets** checks the installed FFmpeg filters
in the existing background worker, then opens native keyboard-accessible
controls. Effects run from top to bottom. Select a row to edit its numeric
parameters and units, enable or bypass it, remove it, or move it one position.
Undo/redo retains the last 32 editing changes in this dialog. Reset restores
the selected effect's default numeric controls at the chosen sample rate.
The chain has a 32-effect allowance. Unsupported enabled effects and invalid
numeric values are refused; bypass never hides invalid values.

Applying an ordered chain makes the basic gain/speed/cleanup controls inactive.
**Use basic effect controls** explicitly returns to those controls. The
selected source, output settings and immutable chain are captured before the
conversion is queued. A library-folder change requires reopening the dialog.
Audio is rendered only after selecting a new output filename; neither opening
the chain editor nor loading a preset starts playback or changes source audio.

The built-in presets are Dry audio, Narration cleanup and Podcast loudness.
**Save chain as a new preset** writes a separately named preset; **Load effect
preset** replaces only the editable chain and can be undone. Saving is an
explicit persistent action: cancelling the surrounding editor does not delete
a preset that was already saved. Presets retain ordered controls, bypass
states, sample rate, identity and revision. Loading at another sample rate
revalidates every frequency against that rate.

User presets are bounded JSON settings under
`state/studio/effect-presets/<identity>.json`, with at most 256 presets and
64 KiB per record. They contain no commands, filter expressions, file inputs,
plugins or executable locations. A stale reviewed preset cannot overwrite a
newer version through the store API. Complete library backups include these
settings even when generated audio is excluded; restored presets are
revalidated before use.

`tests/audio/test_effect_presets.py` covers actual signal order and bypass
with FFmpeg, non-overwrite review, invalid saved values and a full library
backup/restore. `scripts/windows_effect_chain_smoke.py` exercises native
keyboard actions, numeric/preset dialogs and the existing asynchronous audio
conversion. These new gates need evidence from the exact candidate revision;
their presence alone is not a completed Windows/NVDA or listening assessment.

## Saved track, scene and master chains

Open **Audio Studio → Edit scene audio clips**, then choose a saved Studio
project. **Clip effects**, **Speaking role effects**, **Scene effects** and
**Master effects** use the same ordered chain editor. Speaking-role effects
process synthesized PCM before its placement; the accepted raw voice chunk
stays reusable. Clip effects process the retained selection. Scene effects
process that scene's mix; master effects process the assembled final mix.

These saved chains use project schema 3. Schemas 1 and 2 keep their exact old
serialized fields and fingerprints. Editing timing, moving/copying a clip,
changing a script without removing its attached processing, undo/redo and full
library backup retain schema-3 settings. Every edit uses the existing project
revision and fingerprint check. A role or scene carrying effects cannot silently
disappear when its script structure is changed.

Track stems include their own processing, while scene and master chains apply
only to their respective mixes. Delivered chapter WAVs are exact selections
from the processed master when a master chain is active. Text, measured line
boundaries, subtitles and source references stay unchanged. Timeline chains
preserve duration: perform a tempo change in **Convert audio and apply effects**
before adding that audio to a scene.

**Preview saved clip** includes the clip chain. **Preview without clip effects**
bypasses only that chain, keeping the same trim, gain, position and fades. Both
are explicit playback actions and use the existing player/quiet-mode owner.
Stop confirms device closure before an editing operation or preview cleanup.
Split and join require bypassed clip processing so a stateful filter is not
silently restarted or merged at the cut; join also requires matching chains.

The same dialog exposes **Master sample rate**. Choose one of the supported
project rates before rendering; existing filter frequencies are revalidated.
Opus export requires choosing 48000 Hz explicitly. Original retained audio
continues to use its own measured source rate.

`tests/studio/test_project_effects.py` checks exact legacy identities, typed
round trips, stale edits, actual FFmpeg clip/scene/master levels, dry comparison,
chapter consistency, actual SAPI/Flite synthesis reuse and full backup/restore
after deleting the original input. The native clip gate exercises the new
effect controls through real keyboard events. These are candidate checks until
a named exact-commit execution result records their outcome.

## Local music and sound effects

The default `procedural` engine produces real PCM using a deterministic note
composer and FFmpeg sources. Mood, tempo, key, seed and intensity select a simple
pitched instrumental motif with a bass part and bounded fades. It does not use
stock recordings, downloaded weights or a paid API. Its repeating motif and
simple synthesized instruments are explicitly distinct from neural music and
general natural-language music understanding. The `prompt` field is meaningful
for neural engines; procedural generation uses the explicit musical controls.

MIDI export streams standard MIDI note, instrument, tempo and end-of-track events
without holding a many-hour event list in memory. Optional `midi` rendering uses
an explicitly installed FluidSynth executable and a separately reviewed
SoundFont. `configure_fluidsynth()` requires the expected SoundFont SHA256 and
license/attribution notice. No SoundFont is included or downloaded. FluidSynth
was not installed in this Linux validation environment, so its genuine native
rendering remains an installation-specific acceptance step.

`door`, `click`, `chime` and `noise` are procedural cues, not field recordings.
Unknown cue names fail. A door cue is a shaped low-frequency noise impact; it
does not claim realistic foley. Source assets supplied by the user can be used
as ordinary tracks instead.

### MusicGen and ACE-Step

Both are first-class optional local engines. They are not silently substituted
for each other, procedural music or remote providers. Configure them through
`LocalModelConfig(engine, python, directory, manifest, device=...)` and
`configure_music_model()`.

A model manifest has schema `1`, the engine name, exact reviewed `code_revision`,
`weights_license`, `output_rights_notice` and a complete `files` mapping from
relative model/cache filenames to SHA256. The model directory must be fully
materialized: no symlinks, junctions, special files, missing files or extra
unreviewed assets. MusicGen additionally requires `noncommercial_only: true`,
`weights_license: "CC-BY-NC-4.0"` and the private noncommercial profile. Its
`state_dict.bin`, `compression_state_dict.bin` and all needed offline conditioning
model/cache files must already exist. ACE-Step requires a simple `model_name`
and its complete local `checkpoints/<model_name>/...` inventory.

The same reviewed manifest must include `runtime_code_files`, the complete
relative `.py` inventory of the installed `audiocraft` or `acestep` package.
`probe_music_model()` locates that package using the selected Python and verifies
actual installed file bytes. Hard-pinned adapter entrypoint hashes prevent a
declared revision string alone from enabling arbitrary changed interfaces.
The probe does not import the engine or execute a model. Third-party native
dependencies and their licenses remain a separate runtime-installation review.

Registry states distinguish `configured`, `assets_verified`, `runtime_probed`,
`runtime_code_verified` and `inference_tested`. `available` stays false until the
installed code is verified **and** an actual requested generation returns valid
non-silent PCM. Merely adding a profile or passing a manifest test is not an
inference-success claim.

The trusted worker calls the reviewed upstream Python APIs in a selected local
environment. It retains the selected virtual-environment executable path and
uses `-I -S -B` with explicit site-package paths; executable `.pth` and
`sitecustomize` startup hooks are not loaded. API keys, proxies and arbitrary
Python paths are not inherited. Offline cache flags and Python audit denial
block network connections and child commands; writes are restricted to the
worker's staging directory. These controls are not an OS sandbox for arbitrary
native model libraries. Independent OS egress-denial tests remain required for
a model/runtime combination before release acceptance.

The worker validates PCM dimensions, finite samples, duration and non-silence,
then normalizes its sample peak if necessary and uses the same local conversion
pipeline. There are no automatic model downloads or hidden remote calls.
MusicGen/ACE-Step weights and inference were **not run** in this environment.
No voice/model weights or generated private recordings are in the source repo.

Reviewed upstream interface identities:

- AudioCraft `896ec7c47f5e5d1e5aa1e4b260c4405328bf009d`:
  [source](https://github.com/facebookresearch/audiocraft/tree/896ec7c47f5e5d1e5aa1e4b260c4405328bf009d).
  MIT code and separately reviewed CC-BY-NC-4.0 weights; local directory loading
  and `MusicGen.get_pretrained`, `set_generation_params`, `generate` inspected.
- ACE-Step 1.5 `ca1e85fe9430179831e6bc6be790c332190a3866`:
  [source](https://github.com/ace-step/ACE-Step-1.5/tree/ca1e85fe9430179831e6bc6be790c332190a3866).
  `AceStepHandler.initialize_service`, `GenerationParams`, `GenerationConfig`
  and `generate_music` inspected. Weight rights require a separate reviewed
  manifest. Automatic missing-model acquisition and code replacement are
  rejected by installation prechecks and the restricted worker.
- FFmpeg [filters](https://ffmpeg.org/ffmpeg-filters.html),
  [protocol controls](https://ffmpeg.org/ffmpeg-protocols.html) and
  [licensing](https://ffmpeg.org/legal.html). No custom audio-processing engine
  or untrusted plugin executor was added.

## Budgets, local boundaries and long jobs

`AudioBudget` controls elapsed worker time, output bytes, free-disk reserve,
diagnostic bytes, simultaneous decoder inputs, threads, individual FFmpeg
allocation and staging file count. These are explicit resource allowances, not
fixed project-duration ceilings. Large projects use durable scene/chunk jobs
and streaming concatenation. The output disk estimate is checked before work;
actual staging bytes are checked while running and after child exit.

Audio input/output roots reject UNC addresses, protocols, symbolic links,
junctions and ancestor links. Original source paths must be under explicitly
selected local roots. FFmpeg receives argument vectors, a file-only protocol
allowlist and a restricted media-demuxer list. External MOV data references stay
disabled. User playlists and filter strings are never executed. Concat
manifests are generated internally from inspected PCM files with correct path
escaping. Text/model instructions cannot select arbitrary files or network
protocols through this API.

Native audio launches receive a copied host environment with `FFREPORT`
removed, including differently cased keys. This prevents an inherited FFmpeg
diagnostic-file setting from writing to an unrelated path during inspection,
rendering or POSIX playback. Explicit application diagnostics remain bounded
by the process runner; locale, executable paths and audio-device settings are
preserved. The caller's environment is never changed.

WAV uses `-rf64 auto` to handle the RIFF size boundary when required. FLAC and
separate PCM scenes are alternatives. A per-call input bound is handled by
rendering scenes/stems and joining them, not by loading every track for a whole
book at once. Whole multi-hour waveforms are not accumulated in Python memory.
Neural models retain real per-chunk generation limits; scheduling multiple
chunks does not establish musical coherence or full-length original generation.

## Portable Studio inputs in complete library backups

The existing version-4/5 library backup includes every approved local `Cue.path` referenced
by typed Studio projects, saved revisions, production jobs and completed job
project snapshots. Required audio is included even when optional generated
cache is excluded. Inputs are copied as ordinary private files under
`state/studio/inputs/<SHA256>.audio`; identical bytes are stored once. Their
size and hash are part of the existing state manifest. A typed
`studio/backup-inputs.json` receipt binds each original input reference to its
retained bytes and each project snapshot to its source hash and fingerprint.
Missing, linked, empty, changing or oversized required inputs fail the backup
before its success manifest is published. The producer never edits the user's
original audio or saved project bytes.

Opening a project or submitting a saved cue does not approve its paths. New
external references enter the existing local input-review list and cannot be
read by render or backup until reviewed. A caller's explicitly selected input
directories can admit a new cue before rendering; the local review record binds
that approval to its current size and SHA-256. Reopening or resubmitting does
not silently clear a pending review or refresh an old approval. Managed restored
inputs are accepted only through their already verified state-manifest identity.

Restore validates the complete state and receipt before publishing a fresh
library destination. It then rebinds only typed cue paths to verified retained
files inside that destination. Saved revisions keep their revision numbers and
receive filenames matching the rebound project fingerprints. The original
backup remains unchanged. The editable Studio draft is copied exactly: its
current schema stores text, title, selected voice IDs and project ID, with no
external audio-path fields. Unrelated opaque application state is not rewritten.

External production output folders are explicitly excluded. Restored jobs
retain their prior status, destination and artifact identities as recovery
history, clear active output references and wait for a new output folder.
The Studio's Render / resume action asks for that folder, and the service
creates a fresh uniquely named output directory. Calling the renderer before
this selection fails without touching the old output location. Required input
hashes are checked again when they are used. Installed speech engines, models
and licensed runtime assets still need their normal local configuration;
retaining cue bytes does not imply a complete machine migration.

Historical version-4 backups without the input receipt remain readable. Their
project paths stay unchanged, because those backups cannot supply missing
external bytes. Typed jobs still require a new output folder, and any original
inputs must remain available and be explicitly reviewed again. Until that
review, neither rendering nor a subsequent backup may consume the inherited
paths. The native Studio dialog shows the actual files before allowing access;
verification then runs in its cancellable background worker. Approval is bound
to the current file hashes, and changed files require renewed review. A restore
never inherits approvals from the source machine. Restored
libraries can be backed up and restored again; receipts are regenerated for
the new paths, while retained audio is deduplicated.

`tests/storage/test_studio_input_backup.py` includes an actual FFmpeg lifecycle:
render, back up, delete every original input and the original library/output
trees, restore to a Unicode path, select a new output folder and render again.
The regenerated WAV is byte-identical to the original, with 30,000 frames at
24,000 Hz, stereo, and the selected cue confined to the right channel. The suite
also exercises retained-file and reference tampering, cancellation, unavailable
inputs, state budgets, historical backups and a second backup/restore. These
Linux process tests do not substitute for Windows device or NVDA acceptance.

## Measured evidence from this implementation

Linux CPython 3.12.14, Debian FFmpeg `7.1.5-0+deb13u1`, installed GPL-enabled build.
The focused audio/process/music/packaging suites passed 88 tests with no skips
at the implementation checkpoint. The suite uses actual FFmpeg processes for
rendering and numerical checks; optional-model fixtures verify installation
gates only and are clearly synthetic.

Measured behaviors include left/right placement, exact timing and samples,
speech-driven music ducking, +6 dB frequency-selective EQ, gate attenuation,
compressor dynamic-range reduction, limiter peak, impulse echo/reflection
timing, separate tempo/pitch controls, actual loudness/true-peak measurement,
codec conversion, no source modification, no overwrite, cancellation cleanup,
untrusted-playlist denial, Unicode/apostrophe filenames, and typed-control
rejection. Process tests cover fast-exit stdout/stderr overflow, timeout,
recursive staging budgets and progress. Packaging tests cover tampered bytes,
paths, architecture, archive links, duplicate names and offline-default behavior.

An actual long-duration streaming run produced these FLAC files at 8000 Hz mono:

| Duration | Exact frames | Output bytes | Measured render time |
| --- | ---: | ---: | ---: |
| 1 hour | 28,800,000 | 679,348 | 1.738 s |
| 3 hours | 86,400,000 | 2,110,288 | 5.830 s |
| 10 hours | 288,000,000 | 7,172,954 | 16.427 s |

The fixture is a genuine six-second procedural motif followed by silence in a
one-hour PCM scene, then explicitly repeated scene assembly. It tests long
streaming/time alignment, **not** full-length neural generation, music quality
or nonrepetition. Sampled peak Python RSS was 19,361,792 bytes; sampled combined
child RSS was 43,204,608 bytes. Sampling is an observed peak, not a guaranteed
OS-enforced memory maximum. Director restart/cache behavior has its own tests.
Human listening, actual local neural inference, FluidSynth output and Windows
audio devices/NVDA require their own genuine acceptance evidence.

## Pinned Windows runtime preparation

`packaging/windows-audio.lock.json` pins the publisher's Gyan FFmpeg 9.1
essentials ZIP, 119,467,219 bytes, SHA256
`b9bbbb3acece000cbeec994f856649354e52c773807841e78908374b24d658fd`.
The primary release asset metadata and independently downloaded archive matched.
The archive's separate FFmpeg, FFprobe, GPL license and vendor README bytes were
also independently hashed and the executables' AMD64 PE headers checked.
This is byte/provenance validation, not Windows executable acceptance.

From `carr-library`:

```text
python packaging/prepare_windows_audio.py --cache AUDIO_CACHE --output NEW_AUDIO_DIRECTORY
```

The command is offline by default. Add `--download` only for an explicit setup or
build acquisition from the pinned publisher release. Redirects must remain on
the approved HTTPS publisher/release-asset hosts. The helper never executes an
installer or binary. It extracts selected regular entries with bounded streaming
and per-file hashes, retains notices, and publishes a new directory without
replacement. Add `NEW_AUDIO_DIRECTORY/bin` to the selected app runtime's PATH.

The immutable primary release is
[Gyan FFmpeg 9.1](https://github.com/GyanD/codexffmpeg/releases/tag/9.1), with FFmpeg
source revision
[`74e4393595cf2b5857f0da925c4e0dd74ea306e5`](https://github.com/FFmpeg/FFmpeg/tree/74e4393595cf2b5857f0da925c4e0dd74ea306e5).
The vendor identifies its static build as GPL v3. This preparation is for the
authorized private personal deployment and local validation. A complete
corresponding-source bundle for all linked third-party components has not been
assembled here; the lock does not authorize public binary distribution.
The existing frozen release is not replaced by this optional runtime.

# Local Audio Studio: executable V3 workflow

This is an integrated extension of the existing Universal Research Library.
The library, its original files, authenticated quotations and existing research
workflows keep their established data model. Audio projects use separate,
explicitly selected local directories and a versioned JSON plan. This document
describes implemented behavior; the full V3 scope is still the acceptance target
in `UNIVERSAL_SCOPE_V3_AUDIO_AGENT.md`.

## Desktop workflow

Open **Audio Studio → Open studio**, or press **Ctrl+Alt+A**. The menu mnemonic
is **Alt+U**. The editor, speaker choices, numeric controls, progress, outputs and
errors are native text controls. No waveform, mouse or drag operation is needed.

1. Select a trusted, installed FFmpeg and matching ffprobe if they are not on the
   local PATH. Choose **Scan installed voices and audio tools**. This scans
   existing software; it does not download an engine, model or voice.
2. Enter an independent author script. With explicit speaker syntax enabled,
   `# Chapter title` starts a chapter, `Speaker: words` assigns a speaker, and
   `[pause 2]` sets the preceding line's following pause to two seconds. Text
   without a speaker prefix is narration. With syntax disabled, the complete
   input is one faithful narration text, including any apparent commands.
3. Choose **Prepare speakers** to review assignments. Select a speaker and an
   actual installed voice; set pan from -1 (left) through 0 (center) to 1 (right),
   speech-rate multiplier and voice volume, then **Apply voice selection**.
   Unsupported engine capabilities are not invented.
4. Music is optional and off by default. The currently executable generator
   offers calm, tense or bright **procedural** music. Its simple recurring motifs
   are explicitly distinguished from neural music generation.
5. **Ctrl+S** saves the project. On the first save, choose a parent directory;
   the application creates a new uniquely named project folder. **Ctrl+O** opens
   an existing project folder. **Ctrl+Shift+S** saves as a new project folder.
   A concurrent external edit causes a save conflict and retains the editor
   text. Saving an unchanged imported plan preserves its per-scene settings.
   Once a project folder is chosen, each open editor keeps its own recoverable
   draft. Invalid or incomplete text is retained even when project validation
   prevents Save. Recovery is an explicit choice and never starts a voice engine
   or restores executable paths from the draft.
6. Choose **Render or resume**. The application performs speech synthesis,
   exact-frame placement, pauses, optional local score, sidechain ducking,
   per-scene assembly, continuous output, isolated stems, measured loudness and
   output verification. It reports completion only after successful publication.
7. **Interrupt rendering** stops unfinished work and retains verified completed
   checkpoints. Render the same project after interruption or restart to resume.
   A changed line invalidates that line and downstream mixes while retaining
   unaffected speech and scenes. Existing completed output is verified before
   reuse. Altered exported files are preserved and a recovered output receives
   a separate destination.
8. **Play master**, Pause, Resume, Stop and a textual seek position provide
   local Windows playback. The current native player supports ordinary PCM16
   RIFF WAV. RF64 and compressed preview require a future verified player;
   those limitations do not limit the rendering/export backend.

The author-script editor is independent of source quotations. It never edits a
retained book, marks OCR reviewed, changes a printed-page label or inserts new
quotation evidence.

## Produced files

Successful renders are published under `exports/<project-sha256>/` in the chosen
project. This location contains:

- `master.wav`, stereo PCM16, with RF64 support for large rendered masters.
- The additionally selected compressed/lossless master, when its encoder exists
  in the selected local FFmpeg build.
- One WAV per chapter, in source order.
- Optional full-length per-speaker WAV stems and a separate music stem.
- `script.txt` and the exact versioned `project.json` used for this render.
- `chapters.json`, `timing.json`, SRT and WebVTT subtitles.
- `production.json`, with exact frame counts, checksums, engine identities,
  measured integrated LUFS/true peak, source hash and explicit acceptance limits.

Timings describe measured synthesis-segment boundaries. They are not claimed to
be phoneme or word alignment. Speaker stems contain positioned isolated speech;
the music stem is the unducked source bed. Master ducking and final limiting are
separate. LUFS measurement is an observed result, not an assertion that a target
was met; use the explicit loudness effect for a selected target.

Publication never silently replaces an existing output. Production verification
checks the actual audio streams, required artifacts, source/script identity,
chapter continuity, line/source mappings and all recorded checksums. A JSON file
saying “complete” is not sufficient evidence.

## Sound processing and conversion

Open **Audio Studio → Audio effects and conversion**, or use the corresponding
studio button. Select a local audio file and a new output path. The dialog
provides a native ordered effect list, explicit Move up/down, enable/bypass,
remove, numeric parameter editors with units/ranges, and strict preset import
and export. Conversion runs outside the UI thread and can be interrupted.

Implemented numeric effects use FFmpeg's existing filters:

| Control | Actual operation |
|---|---|
| EQ | Parametric frequency, gain and Q |
| High/low pass | Configurable cutoff |
| Gate | Threshold, ratio, attack and release |
| Compressor | Threshold, ratio, attack, release and makeup gain |
| Limiter | Bounded peak amplitude |
| Denoise | Spectral noise reduction and floor |
| Echo | Delay time and decay |
| Fades | Fade-in and fade-out duration |
| Gain | Decibel adjustment |
| Loudness | Selected integrated LUFS, true-peak and loudness-range targets |

The included Narration, Noise reduction, Echo and Loudness presets are editable.
Arbitrary filter graphs, shell commands and plug-ins are not accepted. This
implementation does not claim a room-reverb algorithm, formant processing,
automatic vocal separation or realtime plug-in hosting.

The backend checks the selected runtime's actual encoder/filter inventory.
Available exports include WAV, FLAC, MP3, Vorbis/Opus, AAC/M4A/M4B, AIFF and WMA
where the required local encoder exists. Opus uses its actual 48 kHz decoded
clock. Lossy formats do not acquire lossless or sample-exact claims.

## Installed voices and optional music

Windows voice discovery uses a fixed packaged System.Speech worker. Native and
SysWOW64 voices have distinct provider IDs, `sapi5` and `sapi5-x86`. User text is
passed as JSON data, never as PowerShell or SSML. Installed voices are not
redistributed. Actual compatibility is established by synthesis to a checked
PCM file; it is not inferred from a marketing language count.

An installed FFmpeg build with libflite can provide local English compatibility
voices (`awb`, `kal`, `kal16`, `rms`, `slt` when present). These are useful real
offline voices and test engines; no Ukrainian/Slovak native-quality claim is
made for them. Pronunciation quality requires the selected voice's actual
language support and listening assessment.

SAPI4 and **dordiai-voices** expose explicit unavailable status in this version.
SAPI4 needs a verified compatible helper. No actual dordiai-voices contract was
found in the inspected connected repositories, so a private integration was not
invented or copied.

MusicGen remains an intended first-class optional local engine under the
owner's noncommercial personal-use profile. ACE-Step is also planned. Neither
engine's weights were downloaded and neither was run in this change. The
current runtime accurately reports the missing verified engine/model setup.
Actual procedural PCM generation and Standard MIDI composition are implemented.
Optional FluidSynth rendering requires an explicitly selected local executable
and a SoundFont whose rights the operator confirms.

## Headless use

The existing installed application command gains an `audio` subcommand:

```text
research-library audio voices
research-library audio capabilities
research-library audio create --script scene.txt --directory NewProject --title "Two scenes" --music tense
research-library audio render --project NewProject --progress
research-library audio show --project NewProject
research-library audio status --project NewProject
research-library audio verify --production NewProject/exports/PROJECT_SHA
research-library audio backup --project NewProject --output NewBackup
research-library audio restore --backup NewBackup --directory RestoredProject --manifest-sha256 RECORDED_SHA
research-library audio convert --input input.wav --output output.flac --format flac
```

`python -m carr_library audio …` invokes the same code. Local executable paths
can be supplied with `--ffmpeg` and `--ffprobe`. `create --voice provider:id`
can be repeated to assign successive speakers. `--pan Speaker=-1` sets an
explicit placement. No API credentials, remote services or model downloads are
required or triggered by these commands.

## Persistence and boundaries

Plans are validated data. Unknown instruction/endpoint/output fields, invalid
source offsets, non-finite numbers and unsupported schema versions are refused.
The audio renderer accepts only implemented local providers and owns its
allowlisted operations. Source documents cannot select an executable, external
path, network endpoint or filter expression through their text.

Checkpoints are keyed by actual text/settings/voice/runtime identity and verified
from their bytes before reuse. A process lock prevents concurrent renders into
the same project. The journal is atomically replaced after completed steps;
failed or corrupted cache objects are not trusted as successful output.

Portable project backups use SHA256 manifests. Only validated committed state
and completed immutable checkpoints are copied, together with validated editor
draft snapshots; in-flight render staging files are excluded. Drafts are included
even when generated audio is omitted. The snapshot can be made during rendering.
Restore verifies before
publishing into a separate new directory, retains stable project identities,
and permits resuming from verified completed work. Exports can be recreated
from the saved project/checkpoints; this project backup is distinct from the
existing complete research-library backup command.

All final audio is streamed through files. The production has no arbitrary
10-minute duration ceiling. Large scenes concatenate bounded line/chunk jobs
instead of putting every line into one unbounded FFmpeg filter graph. Disk
requirements are checked using measured frame durations. The 1/3/10-hour
regressions are actual full-duration **repeated synthetic silence** fixtures;
they prove frame accuracy and streaming behavior, not neural generation,
many-hour musical coherence, or total native-process memory limits.

## Verification and remaining V3 acceptance

Source tests exercise actual FFmpeg effects, real local Flite speech, a
three-role/two-scene render, isolated stems, changed-line cache invalidation,
interruption/backup/restore, no-clobber publication, corrupted artifacts,
path/link attacks, source invariance, subtitle/source mapping and long streaming.
Separate native Windows scripts exercise installed SAPI and the real wx
controls. A Linux mock of a WinMM device is labelled a lifecycle test only.

Full V3 release acceptance remains open. In particular, real Windows 11/NVDA
and device/listening acceptance, SAPI4, dordiai-voices, actual MusicGen/ACE-Step,
full Balabolka feature parity, optional API vault/provider budgets, local LLM
planning, complete format/language expansion, CSL/Word/sync and the specified
large-corpus scale measurements must not be inferred from this working local
audio slice. No generated audio, model weights or voice binaries belong in the
public repository.

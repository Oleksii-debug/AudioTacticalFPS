# Local voices, Audio Studio and approved local tools

The desktop now uses the same local rendering services as the command line. It
creates audio files, saves projects and resumes verified work. The complete V3
requirements include additional features; this guide describes this implementation.

## Keyboard workflow

| Action | Menu or shortcut |
|---|---|
| Inspect actually installed voices | Voice → Installed local voices |
| Narrate focused text or its selection | Voice → Narrate selected text; Ctrl+Alt+V |
| Create narration or dialogue | Audio Studio → New narration or dialogue; Ctrl+Alt+U |
| Open a saved project | Audio Studio → Open saved audio project |
| Save the current studio form | Ctrl+S inside the studio |
| Save and export project JSON | Ctrl+Shift+S inside the studio |
| Render/resume, pause or cancel | Named native studio buttons |
| Play, pause or stop completed PCM output | Named native studio buttons |
| Back up or restore a project | Audio Studio menu |
| Convert a selected local audio file | Audio Studio → Convert local audio |
| Review a create-and-import assistant plan | Tools → Local assistant; Ctrl+Alt+J |
| Configure optional API profiles | Tools → Optional providers and API keys |
| Build a Boolean/NEAR search | Search → Build advanced query; Ctrl+Alt+Q |

The editor, selectors and status are native labelled controls. Status receives
focus after save/error. Invalid drafts remain in the editor. Closing a changed
form prompts to save; a failed save does not discard it. Editing is frozen while
a render uses its saved revision; Pause waits for a recoverable checkpoint.
Automated native-control tests do not establish actual NVDA spoken behavior.

### Narration and dialogue

Narration uses the first selected voice and preserves the supplied words. Dialogue
uses the role names in the form. For example:

```text
## Opening
Narrator: The room is quiet.
Speaker 1: Are you ready?
Speaker 2: Yes. Let us begin.
```

A continuation line belongs to the preceding role. Unknown role labels are errors;
the parser does not invent speakers. `##` introduces a chapter. The authored
script is retained separately from derived spoken lines. This deterministic format
is not a general natural-language or creative-rewriting model.

JSON scenario mode supports more roles, exact pauses, separate scenes, prepared
music/effects and local music cues. `studio plan` prints the saved plan. The
dataclasses in `studio/models.py` define its versioned contract. Unknown fields,
duplicate JSON keys, non-finite numbers and unsupported controls are rejected.

## Local engines

SAPI5 enumeration inspects actually installed 32-bit and 64-bit Windows COM voices.
eSpeak NG/eSpeak is supported when installed locally. The application does not
download voice DLLs or model weights on startup. Unsupported SAPI4 and dordiai-voices
integrations report unavailable capabilities. `dordiai-voices` needs its actual
versioned engine interface before an adapter can be accepted.

The optional private Windows audio supplement contains pinned FFmpeg 9.1
executables and original publisher notices. `packaging/windows-audio.lock.json`
records the archive URL, size, SHA256, selected-file hashes and upstream source.
`prepare_windows_audio.py` verifies a stable archive snapshot before extraction;
`add_audio_runtime.py` adds it to a new sealed installation. Installed discovery
follows interpreter prefixes, never document folders. A corrupt discovered
supplement is an error. An explicitly selected installed runtime remains available
for development. No preparation step publishes an installer or removes upstream
licence obligations.

```console
python -m carr_library studio voices
python -m carr_library studio engines
python -m carr_library studio new --text-file narration.txt --voice espeak:en-us --project-dir my-audio-project
python -m carr_library studio render --project-dir my-audio-project
python -m carr_library studio status --project-dir my-audio-project
python -m carr_library studio plan --project-dir my-audio-project
```

Replace the example voice with an ID returned by `studio voices`. Missing voices,
models and codecs produce visible errors, with no automatic remote fallback.

## Source fidelity, rendering and recovery

The renderer saves the plan revision, discovers voice identities, synthesizes
bounded chunks, measures PCM duration, places events on the sample grid, mixes
scenes and concatenates the master. Library originals and saved quotations are
never rewritten. Pronunciation derivatives retain original offsets and explicit
edits; their service is documented in `speech/README.md`.

Accepted chunks have checksums and verified audio headers. Cache identity includes
text/settings/engine identity, selected take and pipeline version. A changed line
invalidates its speech and dependent mixes; unchanged lines/scenes can be reused.
Corrupt cache files remain available for diagnosis and are regenerated under new
names. A changed existing master is an error, not permission to overwrite it.

SAPI identity currently covers voice-token attributes and the helper executable,
not every proprietary voice's private model/DLL dependency. After updating such a
voice, select a new take/revision and review the output. Complete vendor-model
fingerprinting is not claimed.

Projects contain SQLite state, immutable plan revisions, accepted chunks, durable
jobs and output manifests. Pause/cancel/Ctrl+C retains completed work. Reopen and
render again to resume. A previous running job can be recovered as interrupted.
Disk and optional wall-time budgets are resource controls, not a ten-minute limit.

Outputs include a PCM WAV/RF64 master, separate chapter mix files in the verified
cache, SRT captions and a JSON production record. Caption positions are measured
speech-chunk boundaries, not forced word alignment. Final audio is streamed from
disk, with RF64 for files beyond classic WAV limits. The current WinMM player
accepts ordinary PCM16 WAV; RF64/compressed playback and independent playback speed
are not yet accepted. Conversion can create a separately supported listening copy.

### Prepared sound and local music

Scenes may use explicitly selected local music/effect files. The renderer copies
and verifies them before decoding, preserves originals and restricts access to
approved directories. Network/UNC/device paths and unsupported media protocols are
rejected. Speech, music and effects remain separate through bounded intermediate
mixes; speech can duck the music bed in the final scene mix.

`MusicCue` explicitly selects `fluidsynth` or `musicgen`. FluidSynth uses a selected,
hash-checked SoundFont with declared licence information. Its deterministic symbolic
composition uses numeric tempo/key/mode/program settings. It does not interpret
the prose prompt or claim neural-model musical quality.

MusicGen requires a reviewed local model manifest, matching weights/configuration,
supported safe weight files and a compatible installed runtime. Its isolated Python
worker disables network operations. Missing assets fail without downloading or
substituting another engine. Use is restricted to the selected private noncommercial
profile, subject to the applicable weight/output terms.

Long cues use independently generated windows with explicit seeds and crossfades.
Production metadata labels independent-window assembly: it is not neural continuation
and does not guarantee multi-hour musical coherence. Repeated loops are not silently
used to fill a requested duration. Actual MusicGen inference and human listening
remain separate acceptance requirements.

### Conversion and effects

```console
python -m carr_library studio convert master.wav listening-copy.flac --sample-rate 24000 --channels 2
```

The installed FFmpeg build determines codecs. The typed service supports PCM WAV,
FLAC, MP3, Ogg/Opus, AAC/M4A/M4B and AIFF when local encoders exist. Lossy conversion
is not described as lossless. Typed processing includes gain/pan/routing, EQ,
high/low pass, gate, compression, limiter, denoise, echo, fades, tempo, crossfades
and measured loudness. See `audio/README.md` for exact supported parameters.
Arbitrary shell commands/filtergraphs are not accepted. A full interactive effects
editor, microphone recording, device selection and real-time DSP remain V3 work.

## Backup and restore

```console
python -m carr_library studio backup --project-dir my-audio-project --output audio-backup
python -m carr_library studio restore --backup-dir audio-backup --project-dir restored-project --manifest-sha256 DIGEST_PRINTED_BY_BACKUP
```

Keep the printed SHA256 independently of the backup. Restore verifies that digest,
the database schema, listed files and known output references before accepting the
snapshot. The destination must be new. An incomplete restore cannot be opened as
a completed project. A consistent SQLite snapshot selects immutable accepted files,
so an active render can continue independently.

`--without-audio` preserves scripts/history and archives production metadata; audio
is regenerated after restore. External voices, SoundFonts, source audio assets and
model installations remain separate. Library and studio backup are distinct
operations; a single complete backup of all settings/queue sidecars is not claimed.

## Local assistant and optional providers

The assistant executes allowlisted typed plans: create/list/import/search libraries,
create a topic, back up authorized libraries, render studio projects and convert
selected audio. Preview returns a digest. Execution binds the frozen approved plan
to explicit workspaces/read/output roots. Durable receipts prevent blindly repeating
completed writes; uncertain interrupted writes require reconciliation. Read-only
results are refreshed against current authorization rather than replayed stale data.

The guided desktop flow reviews selected files before creating/importing a library.
The CLI offers `agent preview` and `agent run`; their `--help` describes scopes.
There is no arbitrary shell/delete/publish/credential-read tool. Imported document
instructions do not become executable plans.

Provider profiles use Windows Credential Manager or a supported secure OS keyring,
with no plaintext fallback. Opening settings does not retrieve stored keys.
LOCAL_ONLY rejects provider requests before credential lookup or network access.
Saving a profile never contacts its service.

Approved requests bind destination, model, payload hash, credential revision and
reserved budget. The service rejects redirects, remote plaintext HTTP and oversized
responses. Reservations are local limits, not provider billing guarantees. Failed
or uncertain requests are not automatically retried. Independent tests use a memory
vault and controlled transport; no real paid API was called in this development
run. Vendor-specific audio adapters and production routing need separate
implementation and explicitly authorized service acceptance.

# Local reading and audio studio

This implementation follows `UNIVERSAL_SCOPE_V3_AUDIO_AGENT.md`. It extends the
existing desktop and command-line entrypoint. Library quotations remain bound
to their original source; narration edits are a separate versioned derivative.
The previous sealed application package is not replaced by this source change.

## Desktop workflow

Open **Audio > Audio Studio** or open the selected book as a narration copy.
The Studio uses named native controls and four pages: Script, Preparation,
Production and Playback. Enter text, select an installed local voice, select an output folder,
preview the production plan, then choose **Render / resume**. Work runs outside
the UI thread; Cancel preserves accepted checkpoints. Status and errors are
readable text. Ctrl+S saves, Ctrl+Shift+S saves under a new name. Autosave preserves
a recovery draft; an externally changed project file is not silently replaced.

**Literal narration** is the default interpretation. Every source line remains
spoken text, including headings, colons and text such as `[pause 2]`. Imported
book/text copies always start in this mode. Form-feed source separators become
separate source segments while the immutable original retains its exact text.
Choose **Structured production script** explicitly to enable role, pause,
music and output directions. The interpretation is saved in the project and
snapshot. Legacy independent scripts retain their prior script interpretation;
legacy imported source copies open in literal narration mode.

Preparation supports explicit literal pronunciation substitutions, editable
reading text, original/difference views, undo/redo and export of the reading copy.
These edits never approve an OCR quotation or rewrite stored library evidence.

**Back up audio project** includes the saved project, recoverable drafts, assets,
cache, generated audio, MIDI and manifests in a checksummed ZIP64 snapshot.
**Restore audio snapshot** verifies a snapshot into a fresh folder and rebinds
the output location. Stop a running render before backing it up. This snapshot
is separate from the research-library backup and from installed model/voice
assets. API keys, executable runtimes and unrelated private files are excluded.

## Role voices and final output controls

Choose **Structured production script** in **Text interpretation** before using
these forms. Imported reading copies initially use **Literal narration**; switching
interpretation is an explicit decision because structured mode reads role and
other production directions.

On the Script page, choose **Edit roles and voices**. The **Production role**
list selects one existing role. Choose its exact installed voice and then a
language advertised by that voice. The form preserves the full language code;
it does not silently select another region or the first voice in the list.
A saved voice that is advertised as unavailable stays visible. An ID marked
**not in the current list** remains unchanged too; it may be a retained engine
alias rather than proof that the engine cannot render it. Changing that role
requires an explicit advertised voice/language choice.

The named controls use the current production settings:

- **Pan** runs from -1 (left) through 0 (centre) to 1 (right).
- **Speech rate** and **Speech pitch** use the selected engine's -10 to 10 scale.
  A control absent from that voice's advertised capabilities is disabled at its
  default. An inherited nondefault value remains visible so it can be reset
  explicitly. This pitch control is separate from the audio effect measured in
  semitones.
- **Speech volume** is 0 to 100 percent.
- **Final role gain** is -120 to 24 dB with an explicit enable checkbox. It edits
  an existing final gain effect or adds that effect at the end. Earlier effects
  retain their order and settings; use **Role and master effect chains** for
  other chain changes.

On the Production page, **Final format and stems** opens the same form with
focus on **Final audio format**. Choose WAV, FLAC, MP3, Opus, M4A, M4B, AIFF, Ogg,
AAC or WMA, then set **Export separate role and background stems**. Existing
sample rate, disk budget and master processing are retained. Codec availability
is checked by the current production workflow. Output-only changes can retain
an unavailable or unlisted role unchanged; they do not select a replacement.

**Apply role and output changes** makes one undoable edit to the current draft.
**Cancel role and output changes**, Escape or closing the form discards its
pending edits. On the Preparation page, **Undo preparation edit** and
**Redo preparation edit** undo or restore the accepted form edit as one action.
If the current draft, project or selected runtime changes while the form is
open, Apply refuses the stale form; review the current project again.

After Apply, use **Save all open drafts** or Ctrl+S to save the project, then
**Preview plan** to review the changed roles and output before
**Render / resume**. Accepted changes invalidate the old render result and the
displayed preview. The immutable original, existing project format and managed
backup remain the same. The form itself starts no rendering.

The dedicated automated form qualification uses actual Windows wx controls in
addition to source and isolated installed regressions. Its current source-bound
status is recorded in
[Studio role/output validation](validation/STUDIO_ROLE_OUTPUT_CONTROLS_20261127.md);
the exact `aee11f99c4be0da3aa257ac7722923d768220e7f` [Windows run](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46067792077)
passed all211 source,211 isolated installed and39 native form checks. This form
change makes no model-quality, physical-playback or manual NVDA claim.

## Explicit script directions

Select Structured production script, then define roles before their first spoken line. No arbitrary code, shell command
or FFmpeg filter expression is accepted in a script. Unknown directions fail
with a line number. The current planner is a deterministic script parser;
general natural-language tool planning is not claimed.

```text
[role Narrator voice="sapi5:default" pan=center language=en]
[role Alice voice="sapi5:default" pan=left]
[role Bob voice="sapi5:default" pan=right]
[effect Narrator highpass frequency=80]
[output format=wav sample_rate=48000 stems=true disk_budget_mb=32768]
# First chapter
[music mood=calm tempo=90 gain=0.15 seed=7]
Narrator: This is an original script.
Alice: Hello.
[pause 2]
Bob: Hello again.
# Second chapter
Narrator: This chapter has no background music.
```

Replace voice IDs with the IDs shown in the voice list. Distinct roles can use
different installed voices. `[sound door.wav gain=0.5 pan=right]` uses an
explicitly supplied PCM WAV in the project's `assets` folder. A sound asset's
hash participates in the render identity. Unsupported voice-language or
pitch/rate controls fail instead of silently substituting a different engine.

Production writes a PCM WAV master, chapter WAV files, separate role/music/effect
stems, SRT with measured line boundaries, chapter metadata, timeline JSON,
checksums and measured peak/loudness. Word timing is not inferred. Compressed
exports depend on codecs in the explicitly installed FFmpeg build. DSP uses
typed numeric presets for EQ, filters, gate, dynamics, denoise, delay and music
ducking. A numerical level measurement does not establish listening quality.

## Local engines and large jobs

SAPI 5 uses installed Windows voices. The backend also supports reviewed local
Piper and dordiai-voices manifests and eSpeak when installed. On builds containing
FFmpeg's Flite filter, five basic English voices are discoverable. These are not
proof of natural Ukrainian or Slovak speech. Legacy SAPI 4 and ACE-Step remain
explicitly unavailable until their compatibility adapters are validated.

The procedural music engine produces actual original instrumental PCM and MIDI
from a deterministic score. MusicGen has an optional local adapter with a
checksum-verified model/runtime manifest and a private noncommercial profile;
it never downloads weights automatically. Actual MusicGen inference requires a
separately installed compatible runtime/model. No neural generation result is
claimed by procedural or synthetic tests.

Completed line/scene/music steps are keyed by their source, voice/model/settings,
asset hashes and FFmpeg identity. Valid chunks survive cancellation and restart;
corrupt chunks are quarantined and regenerated. Re-rendering an unchanged scene
reuses verified chunks. A project lock prevents concurrent writers. Long PCM
assembly is disk-backed and switches to RF64 above the RIFF size limit. There
is no ten-minute total-output ceiling. Configurable disk budgets still apply.

## Command line

```powershell
research-library --audio-voices
research-library --audio-script script.txt --audio-preview
research-library --audio-script script.txt --voice "sapi5:default" --audio-output C:\Audio\Project --ffmpeg C:\Tools\ffmpeg.exe
```

The CLI uses the same parser and renderer as the desktop. `--audio-plan` accepts
the versioned JSON plan. Optional `--dordiai-manifest`, or the paired
`--piper-executable` and `--piper-manifest`, select explicitly installed local
engines. No network provider is selected by a local script.

## API connections

**Settings > AI and API connections** manages separate named provider accounts.
Keys live only in Windows Credential Manager; JSON profiles contain opaque
references. Exported profiles omit even those references and require reconnecting.
Opening settings or saving a profile sends nothing. There is no plaintext key
fallback. Changing the destination/model/account requires reconnecting the key.

**Run a reviewed task** can use an explicitly selected OpenAI-compatible text
endpoint or ElevenLabs speech/music/sound-effects endpoint. It reviews the exact
task text, provider, model, output file and estimated USD budget before one call.
The normal mode remains LOCAL ONLY. Loopback services require explicit scope as
well. Request approvals bind exact payload/profile hashes. The transport pins a
validated address, validates TLS, refuses redirects and proxies, and does not
retry. Timeouts retain an uncertain-charge reservation; accounting records
estimates separately from unknown actual billing. A provider's response cannot
execute returned tools or shell commands. API tasks are currently a separate
reviewed workflow, not automatic remote fallback inside local audio rendering.

Reservations are checked before credentials are opened. An interrupted or
uncertain operation blocks another operation ID in that job until explicit
billing reconciliation; a confirmed failure before transmission releases its
cost reservation while retaining the attempt record. Profile removal commits
metadata before removing an unused key, so a failed settings write preserves
the existing account. Cleanup failures are reported without exposing key data.
The transport ignores inherited TLS session-key logging, rechecks cancellation
after connecting and checks an elapsed-time budget during response streaming.
Network cancellation is cooperative; the OS DNS resolver cannot be forcibly
interrupted by these checks.

Official adapter references inspected for this change:

- [ElevenLabs speech](https://elevenlabs.io/docs/api-reference/text-to-speech/convert)
- [ElevenLabs music](https://elevenlabs.io/docs/api-reference/music/compose)
- [ElevenLabs sound effects](https://elevenlabs.io/docs/api-reference/text-to-sound-effects/convert)
- [OpenAI-compatible chat request shape](https://developers.openai.com/api/reference/resources/chat)
- [Windows generic credential storage](https://learn.microsoft.com/en-us/windows/win32/api/wincred/nf-wincred-credwritew)

## Acceptance evidence and limits

`scripts/long_audio_benchmark.py` creates real 1/3/10-hour files with bounded
memory, exact frame counts, checkpoint reopen, cancellation and checksums. It
explicitly repeats a short procedural score; this is streaming validation, not
full-length original neural composition or music-continuity acceptance.

`.github/workflows/carr-audio.yml` runs the exact revision on Windows, with a
SHA256-pinned CI-only FFmpeg wheel, native SAPI, wx controls and Credential
Manager. `scripts/windows_audio_smoke.py` keeps manual NVDA, physical keyboard,
device playback, native-speaker assessment and neural inference separate from
automated success. CI does not call commercial APIs or publish generated audio.

The requirement-by-requirement integration and qualification state is maintained
in [V3_RELEASE_READINESS.md](V3_RELEASE_READINESS.md). That record separates
implemented controls and exact automated runs from real voice/model quality,
physical audio devices, manual Windows/NVDA acceptance and distribution rights.
This audio slice does not declare the complete platform qualified.

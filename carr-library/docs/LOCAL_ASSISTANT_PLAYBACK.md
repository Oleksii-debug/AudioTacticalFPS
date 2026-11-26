# Local assistant and shared audio playback

The native **Tools > Local assistant** action (`Ctrl+Alt+J`) runs reviewed local
workflows through the existing application and Audio Studio production service.
It does not introduce another studio, voice registry, provider store or reading
project format. Existing API profiles remain in their existing settings view.

## Scope and commands

Choose the library, permitted input folder and output folder. Preview displays
the complete typed plan and its permissions; **Run / resume** executes it.
The native form exposes per-operation file and output-byte budgets and a time
budget for each run or resume. Increasing a budget is an explicit user choice.
The full-library backup tool needs its separate checkbox because it includes
all workspaces in the selected application data directory.

The guided parser accepts these commands in English and Ukrainian:

```text
Create library "History"
Import folder "C:/Books/History"
Search "railway transport"
List books
Extract "EDITION_ID" as "reading.txt"
Backup "complete-library"
Produce audio: Text to narrate exactly as written.
```

The corresponding Ukrainian commands include `Створи бібліотеку`, `Імпортуй
папку`, `Знайди`, `Покажи книги`, `Експортуй текст`, `Резервна копія` and `Озвуч:`.
Unsupported requests fail during preview. This is a guided local executor;
general natural-language understanding and local LLM inference are not claimed.
There are no shell, arbitrary-code, secret-reading, publishing or remote tools.
Documents, search results and quoted command payloads stay data.

Audio prose defaults to literal narration. Speaker-like labels, bracketed text
and headings are spoken. A versioned JSON plan can explicitly select the
existing structured script parser:

```json
{
  "schema": "carr-assistant-plan/1",
  "steps": [{
    "tool": "audio.produce",
    "arguments": {
      "text": "# First chapter\n[pause 1]\nHello.",
      "input_kind": "script",
      "voice_ids": ["sapi5:default"],
      "language": "en",
      "format": "wav",
      "stems": true
    }
  }]
}
```

The host supplies the selected scope; JSON cannot expand it. A `script` object
can instead contain the existing `ProductionPlan.to_dict()` interchange with
explicit roles, scenes and cues. Voice identities must be available locally.
The normal voice selector shows actual installed voices. Optional MusicGen and
local assets retain the existing renderer's explicit installation requirements.

## Recovery and budgets

Assistant checkpoints use atomic JSON and operating-system locks in
`state/assistant`. Every audio operation receives a stable executor-generated
identity and its own output subdirectory. The adapter delegates synthesis,
mixing, format export, checksums and chunk recovery to `audio.production`.
Resuming verifies each completed artifact's size and SHA-256 before reusing it.
Changed output is reported, never silently overwritten or treated as complete.

The complete-library backup integration uses the pure
`assistant.validation.validate_state_document(basename, raw)` interface for
job and control JSON. It validates schemas, identities, approved plan hashes,
scope, statuses and artifact records without opening saved paths. Locks and
temporary files are excluded. Restoring to a different data directory may set
`requires_reapproval: true`; original paths and the approved plan are preserved.
Such jobs remain visible with an attention status and cannot resume until a new
plan has been explicitly reviewed and submitted.

Audio receives the approved working-disk budget before rendering. Complete
library backups are created in a temporary sibling by the existing backup
service. A receipt then checks every database, original, reader-state and
sidecar byte before the final directory is published with its manifest last.
Later resumes recheck the complete tree, including unexpected additions and
empty directories. This does not replace the existing backup format or restore
implementation.

The existing library snapshot routine has its own disk limits and does not
offer cooperative cancellation within every copy operation. An assistant
cancel/deadline during that snapshot takes effect at the next verification or
publication boundary. Oversized or cancelled staged results are removed without
publishing the requested final backup. This limitation is separate from the
audio worker's per-chunk cancellation.

## Playback

The existing Audio Studio Production page now has **Pause / resume playback**,
**Seek to seconds**, **Seek playback**, **Read playback position**, and a named
read-only time/state control. Automatic position updates do not steal focus;
the explicit Read action focuses the readable status.

The shared `audio.player.WavePlayer` uses Windows MCI or an explicitly installed
`ffplay`. RIFF and RF64 PCM16 preflight uses the production reader and never
loads a whole multi-hour waveform. Windows position comes from MCI; ffplay's
elapsed position is explicitly approximate. Device or decoder errors remain
visible. RF64 structural validation is not proof that every Windows MCI driver
can play that container; actual device compatibility and listening are separate
acceptance. No device recording or background playback starts on application
launch, import or project open.

## Reuse and verification

The local assistant models, guided parser, job store and executor, the native
assistant dialog and WAV player were adapted from repository PR103 commit
`fc84a791930dcff419d2859ab3a040c0f4481261`. The complete-backup receipt and its
independent tests came from `eee9be236ac70ac5190dc50abb78a5fda418d51e`. The duplicate
provider, voice, studio and reading schemas from that branch were not adopted.

The integrated tests exercise real SQLite creation/import/search, immutable
Unicode extraction, bounded scopes, prohibited tools, cancellation/reopen,
artifact tampering, complete-backup payload verification, and an actual local
Flite/FFmpeg narration through the production adapter. Player tests validate
real RIFF/RF64 bytes and use explicit device-call doubles for pause/seek state.
They do not establish physical playback quality.

The Windows audio workflow includes these regressions. Its native smoke also
runs actual SAPI/FFmpeg production through the assistant, verified resume and
the real wx assistant preview/run controls. Windows/NVDA, physical devices,
native-language quality and neural inference are only accepted when their
separate actual evidence exists; source tests alone do not close those gates.

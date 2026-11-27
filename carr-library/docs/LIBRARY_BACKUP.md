# Audio in checked library backups

The existing [checked library backup](COMPLETE_BACKUP.md) includes managed Audio
Studio projects saved through **Save in library**, together with the database,
registered originals, reader bookmarks, reading copies, recovery drafts, keyboard
preferences, evidence-note recovery, OCR presets and assistant checkpoints.
External projects remain supported by Audio Studio's
separate verified ZIP64 backup. The library backup never scans external folders.

## Preserved audio state

Managed projects use `audio-projects/<32-hex-project-id>/`:

| Namespace | Preserved state |
| --- | --- |
| `studio/project.json` | The exact saved version-1 or version-2 local studio document, immutable original, reading edit history, narration/script mode and source references. |
| `studio/project.json.recovery.json` | Exact recovery draft and its saved-project baseline SHA256, including a stale draft that requires explicit review. |
| `audio/assets/*.wav` | Local prepared sound recordings. |
| `audio/cache/` | Named SHA256 speech/music WAV chunks, checkpoint JSON, MIDI and music provenance. Quarantine is excluded. |
| `audio/render-journal.json`, `audio/plan-<sha256>.json` | Durable production status and plans. |
| `audio/renders/<sha256>/` | Completed WAV/RF64 masters and stems, supported format exports, SRT/VTT subtitles, chapter metadata, MIDI and production receipts. |
| `restore-manifest.json` | Prior standalone project restore provenance, when present. |

The collector reuses the production snapshot's member allowlist. Arbitrary files,
private profiles, credentials, models, executable helpers, locks and temporary
working files are excluded. Empty subtitle files remain valid artifacts. The
restored computer needs its own configured voice engines and reviewed runtimes.

A managed project's stored absolute output suggestion is preserved byte-for-byte
in the snapshot. On reopening, the managed controller resolves output to that
project's `audio/` folder in the new library. Its recovery baseline still matches
the original saved bytes. Restoration never redirects a production into the old
library or executes text from a project.

## Integrity and concurrent work

Backup acquires the library writer lock, then each sorted project's draft lock
and production `audio/.render.lock`. These acquisitions are nonblocking; an active
writer or renderer receives a clear retry message. Let the job complete or cancel
it before trying again. Saved partial production checkpoints remain resumable.

Every included file is streamed in chunks of at most 1 MiB and receives an exact
byte size and SHA256. File identity and complete inventories are checked again
before publication. Unexpected changes abort before the completion manifest is
published. Symbolic links, reparse points, parent traversal, Windows-reserved
names and case/Unicode aliases are rejected.

Production intentionally links sealed cache chunks into completed renders. The
backup permits these internal links only when **every** link to the inode occurs
in the bounded inventory of the same project. External or unaccounted aliases
are rejected. The backup and restored files are independent copies with one link
each after staging is removed.

Reader/editor locations and library-linked studio pages must still match their
exact workspace, edition, historical sealed revision, original digest, page kind,
index and page-text hash. Studio source offsets must reconstruct the immutable
source text and its page separators. Editing a reading copy cannot authorize a
forged original or silently select a newer source revision.

## Versions, budgets and interruption

Database schema versions and backup-manifest versions are distinct. Existing
version-1/2 backups remain supported. Reader-only backups retain the existing
version-3 `filename` entries directly beside the database. Managed audio upgrades
the snapshot to version 4; assistant checkpoints use that same version-4 namespace.
All included reader/audio/assistant files have `path`,
`size_bytes` and `sha256` entries under a separate `sidecars/` folder. The two
schemas are validated exactly; restoration never guesses mixed layouts.

`create_backup` and `restore_backup` accept `max_sidecar_files`,
`max_sidecar_bytes` and a `cancelled` callback. Defaults allow 100,000 artifacts
and 128 GiB of combined sidecar content. Callers may lower these values or raise
the total allowance to 1 PiB. Individual audio/media files are capped at 32 GiB,
studio/production JSON at 64 MiB, and managed project folders at 1,000. Existing
reader bounds remain: 8 MiB reading state, 32 MiB per reading copy/draft and 64 MiB
combined, 128 KiB keyboard preferences, 64 KiB OCR presets. The manifest is capped
at 8 MiB. The database and registered original-file limits remain unchanged.
Semantic keymaps add 64 KiB, evidence-note recovery 8 MiB and optional progress
preferences 16 KiB. Assistant checkpoint limits and the distinction between
preserved plan history and renewed execution permission are documented in
[checked backups](COMPLETE_BACKUP.md).

A failed or cancelled copy removes private staging. Backup publication writes
its completion manifest last. Interrupted restore publication retains
`.restore-incomplete`, and the application refuses to open that partial library.
A retry requires a fresh destination; existing libraries and backups are never
silently overwritten.

Tests exercise combined reader/editor/studio recovery, version compatibility,
exact source binding, held writer locks, path and link rejection, corruption,
bounded cancellation and interrupted publication. Real FFmpeg tests restore both
a completed production and a cancelled two-scene job through the actual managed
controller, verify the new output root and reuse the previously generated cache.
The speech fixture is synthetic; these tests do not claim voice quality, native
Windows or manual NVDA acceptance.

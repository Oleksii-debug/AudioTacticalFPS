# Primary desktop state in complete backups

## One current writer, explicit historical readers

The current complete-library writer emits the typed version-4 manifest. Each
sidecar has an explicit producer, bounded schema, relative filename, byte count
and SHA-256. Unknown state fails visibly with its filename; it is not silently
dropped or accepted as arbitrary JSON.

The version number alone does not identify every historical envelope. The common
reader distinguishes exact, complete manifest shapes before any restore. It
retains database/original archives 1–2, historical reading-only and sidecar
archives, and the exact former state envelopes 3–6. An invalid typed version-4
archive cannot fall back to the older opaque-state validator. See
[the historical media format](BACKUP_FORMAT_V6.md) for the version-6 compatibility
boundary.

## Preserved producers

| Producer | Retained data and limits on its meaning |
| --- | --- |
| Reader and Reading Desk | Bookmarks and positions bound to exact sealed source identities; reading copies, recovery documents and edit history retain original text. They do not acquire quotation authority. |
| Current preferences | Root keyboard, keybinding, progress, OCR preset and evidence-draft files retain their own validators. |
| Historical preferences | Exact schema-1 state/ui/shortcuts.json and state/ui/progress.json remain at their original paths. Old menu paths are passive review data and are never retargeted to current command IDs. Dialog-local progress keeps its separate existing producer; backup activates no sound policy. |
| Studio | Projects, revisions, unfinished autosave, job/control/timeline records, retained selected cue inputs, PCM recordings and publication receipts. Restored jobs require a newly selected output directory. |
| Supplied-worker transcription | The accepted workspace-hashed TranscriptionService job directories, retained originals, settings, protocol/runtime declarations, accepted receipts, timed/text output and user edits. No separate ASR store is created. |
| Shared resources | The operator's typed resource policy. Runtime coordination, leases and native lock ownership are not copied. |
| Assistant | Exact approved plans, source scopes and checkpoints. Saved paths do not become new execution permissions. |
| Translation | Pasted translation/1 and source-bound translation/2 jobs, Basic v2 and Advanced v3 profile declarations, approved request scope, language inventory, accepted chunks, usage reservations and decoded-text evidence. |
| Local engines and profiles | Passive declarations and opaque credential references. External engine/model files and vault secret values are not included. |

Every source-bound translation keeps its original root identity, canonical source
anchor and job UUID. A moved or restored pending job requires a fresh preview and
approval before further external requests, including restoration into the old
now-empty path. Completed evidence can be exported offline after the existing
source checks. Accepted text must still match its usage-ledger decoded-output
hash; changing text and its own self-hash cannot replace that evidence. Historical
Basic receipts that lack such proof remain explicitly unrecorded legacy evidence.

## Snapshot and restore boundaries

The library writer guard covers the snapshot. The existing ASR and Studio job
locks are acquired before reading their mutable checkpoint files and held until
backup publication. A running child or pending native cleanup owner therefore
requires the user to pause or wait and retry; backup does not force cancellation
or read a live Windows status file.

Default backup excludes reproducible generated-audio cache files.
include_generated=True adds only completed registered cache files with their
producer receipts, signatures and matching hashes. Selected media inputs and
completed recording originals remain retained with either setting.

Metadata have a 512 MiB aggregate budget plus their individual format limits.
Retained media and generated cache media each have a separate 32 GiB aggregate
budget. The snapshot shares a 100,000-entry bound and the whole-backup byte budget
includes database, originals, pending sources, metadata and media. Binary copies
stream in bounded blocks. Historical opaque-state envelopes retain their original
32 GiB state limit; the compatibility reader does not enlarge it.

After copying, original file bytes are hashed again. This detects equal-size
changes even when modification times are restored or Windows creation time does
not change. Denied root lookup, disappearance after discovery, unlisted files,
links, hardlinks, portable-name collisions and changed inventories fail before a
completed archive is published. Pending-import byte budgets include pinned
unpublished assets; immutable publication receipts remain database-bound.

Restore verifies the archive before publishing a destination. Studio input
rebinding changes only the private restored projection; archive bytes and its
manifest digest remain unchanged. Old external cue paths need fresh review,
previous output destinations authorize no new writes, and external publication
receipts need verification against user-selected actual files. Runtime/model
declarations remain declarations until the normal service checks them.

Cancellation before publication creates no completed destination. An interruption
during destination publication retains the incomplete-restore marker, and the
application refuses to open that destination as a complete library.

## Acceptance evidence

The focused storage suites exercise actual SQLite backup/restore and saved
producer objects. Their synthetic provider transport never contacts Google;
the existing supplied-worker fixture uses actual child processes without a
recognition-quality claim. Media tests use the selected actual FFmpeg runtime
and distinguish retained inputs from generated caches.

The reconciliation adds typed and historical media round trips, old/current
preference coexistence, same-path source-approval revocation, decoded-output
receipt tampering and busy-ASR-lock regressions. These cases require execution
against the exact composed source and installed wheel. Earlier donor passes do
not qualify this new composition. Native Windows, manual NVDA, physical recording
devices, codec quality and human listening remain separately identified gates.

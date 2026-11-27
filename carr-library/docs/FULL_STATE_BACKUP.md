# Full library and production-state backups

Use the Library backup command or the existing `--backup` entrypoint to write a
new local backup directory. Keep the returned manifest SHA-256 separately. A
restore requires that exact digest and creates a new data directory; it never
merges into or replaces an existing library.

## What the snapshot preserves

The library database includes every workspace, catalog entry, historical source
revision, saved quotation, note, topic, tag, saved query, bibliography record and
stored metadata revision. Registered original files are copied and verified.

Manifest version 5 combines the accepted canonical `state/` and pending-import
components with the earlier typed `sidecars/` component. Each component has one
producer; an import journal or pending original is never captured twice as two
independent authorities.

| State | Backup and restore behavior |
| --- | --- |
| Reading positions and bookmarks | Preserve exact workspace, edition, revision, location and source digests. Reading still revalidates these identities before displaying a page. |
| Keyboard and OCR preferences | Preserve validated command overrides and named, ordered recognition presets. Restoring preferences does not install a voice or OCR model. |
| Audio projects and history | Preserve both accepted project stores, historical revisions, narration derivatives, voice settings and required imported audio assets. Generated caches are optional. |
| Live voice-reader recovery | Preserve canonical reading copies, original text, edit history, pronunciation state, selected voice and cursor/playback position under `state/speech/`. |
| Audio drafts | Preserve each editor window's checksummed recovery record, including exact unfinished text, title, language, output format, project identity and original base revision/fingerprint. Legacy single-project drafts remain readable. |
| Local music registrations | Preserve nonsecret model/SoundFont paths, exact hashes and rights metadata. External assets remain separate and must pass validation before later use. |
| Local voice registrations | Preserve strict executable/manifest paths and reviewed hashes. Hash-bound Studio registrations become inactive until explicitly revalidated; external voice assets are not copied. |
| Production jobs | Preserve project snapshots, progress, receipts and external destination history in both stores. Restored jobs require a new output destination and a new render job. |
| Assistant workflows | Preserve typed plans and durable results. Restored plans require a new review and current folder selections before execution. |
| Provider profiles | Validate the strict nonsecret profile schema. Restore clears Credential Manager references and requires reconnecting remote accounts. |
| Provider usage history | Preserve reservations, request hashes and uncertain charge states. Restored ledgers are read-only; a backup cannot authorize another call against an older spending allowance. |
| Translation jobs | Preserve original text, accepted translated chunks, provenance, exact approvals and request hashes. Restore disables the saved account and requires fresh approval before any further request. |
| Progress and engine preferences | Preserve canonical UI preferences and nonsecret engine configuration. Restored engine registrations require explicit revalidation before activating an executable or model. External installed packs and their rights remain separate from this snapshot. |
| Import requests, batches and review records | Preserve queue identities, expected source digests and review bindings. Rebind `data_dir` and explicitly listed source fields only through the canonical manifest's hash-bound source mapping. |
| Import checkpoint journal | Include the canonical consistent SQLite snapshot and all required pinned source bytes, including planned batches whose originals have not yet entered the catalog. Resume uses the copied original even after the selected external file disappears. |

Without typed sidecars, the canonical writer retains its version 2/3/4 choices;
an existing import journal produces version 4. A mixed library produces version
5. Restore retains version 1 and 2 support and strictly distinguishes the two
historical version 3 schemas: `sidecars`, or `state` plus
`generated_audio_included`. It accepts canonical version 4 unchanged. A forged
mixture of the two version 3 schemas is rejected rather than guessed.

The `include_generated` option applies to generated caches in both stores.
When false, the manifest says so and those caches can be regenerated. Required
source/audio assets, authored projects, recovery drafts, job receipts and source
hashes remain included. Semantic embedding caches are rebuildable and excluded.

## Active jobs and resumable work

The backup takes the library writer lock and short studio/assistant state locks.
It snapshots accepted state between publications. If a state publication already
owns its lock, the backup reports that it is busy and can be retried. It does not
copy unfinished render work, temporary files, lock files or invalidated cache
diagnostics. Import metadata files must remain byte-identical while copied;
atomic replacement during the snapshot causes a visible retry error.

Canonical pending backups verify a planned source against its original manifest
and retain those exact bytes before reporting success. Restore substitutes only
the explicitly bound local location fields; document identities, hashes,
citations, queue status and review attestations remain unchanged. Historical
sidecar-version-3 backups keep their original external source strings and resume
from retained bytes where those bytes were included by that earlier format.

Completed external audio export folders are separate from private project state
and are not copied by this library backup. Restored production jobs retain their
previous output path as history and refuse to resume into it. The older Studio
store clears active output/result fields; canonical jobs retain their original
receipts and destination behind an execution guard. Start a new render in a new folder to reuse intact accepted
chunks. This protects existing exported audio from being overwritten.

Engine-configuration paths are recorded without following them: the backup does
not collect external model weights, executables or voice packs. OS credentials
remain separate. Files already inside the canonical application-state component
are included according to its manifest and cache setting; that is not permission
to redistribute a licensed pack. Keep separately reviewed installations available.
A missing voice or model remains an explicit configuration error.

## Verification and limits

Every component has a relative portable path, exact byte count and SHA-256
digest. Typed sidecars also use an explicit path allowlist. Canonical legacy
state can retain opaque data; recognized executable job/profile schemas receive
their structural checks and restore guards. The implementation rejects traversal, symbolic links, reparse
points, multiple hard links, unexpected files, duplicate manifest paths and
unknown secret fields in provider profiles. It validates reading, project,
draft, workflow and connection schemas before publishing the backup or restore.
Reading positions, bookmarks and Studio source links must also match an exact
sealed page in the copied database. Historical revisions and archived libraries
remain valid; an unrelated source cannot be accepted merely by recalculating the
sidecar checksum.

Per-window recovery drafts must refer to a saved project included in the same
snapshot, or explicitly identify a project that has not yet been created. A
draft based on an older revision is retained unchanged when another window has
saved a newer one. Restore neither rebases that draft nor drops invalid authored
text; recovery keeps the original conflict visible before a later save.

Copying streams in 1 MiB blocks with cancellation checks. Explicit limits are
100,000 typed sidecar files and 64 GiB of sidecar bytes; canonical state has a
100,000-file/32 GiB budget. Ordinary typed sidecars are at most 32 GiB each and
typed recovery JSON at most 32 MiB. Pending imports retain the canonical 8 GiB
component, 512 MiB journal and row/checkpoint limits. The complete manifest is
at most 8 MiB. Existing database and registered-original limits also apply. A request beyond a limit
fails with an explanation; it never produces a silently incomplete snapshot.

Restore verifies the complete staged file inventory and all digests before
creating the destination. Explicit root rebinding and restore guards are then
validated again. Publication retains `.restore-incomplete` until the entire new
root is in place. The application rejects a root with that marker after a crash
or an interrupted publication.

Regression coverage includes both historical version 3 layouts, canonical
version 4, mixed version 5, exact reading/project
recovery, drafts, nonsecret profiles, protected provider history, corrupt and
missing bytes, traversal and links, resource limits, a busy state writer, and a
real pending import resumed from restored retained bytes after its external
source file was deleted. These are application recovery checks, not a claim that
an external disk, a scheduled backup or an encrypted archive has been tested.

Transcription recovery preserves retained media, checked results, immutable
worker receipts and separately labelled text edits in canonical state. The
engine registration gains `restore_requires_revalidation: true`; each typed
job gains `restore_requires_review: true`. Completed text and exports remain
readable without opening the former computer's runtime or model paths. A new
attempt requires explicit local-engine registration and retained-input review.
Model weights and private worker sources remain outside this backup.

Import requests with QA011 publication receipts are validated against their
original immutable request identity before restoration changes any root or
source path. The `publication` and `attempt_id` fields are then removed; a past
execution receipt never authorizes a new root. This also applies to historical
version-3 sidecar imports. Registered format suffixes come from the same strict
MIME mapping as intake, including the additional supported document formats.

Canonical source revalidation from commit
`9ed89d6255457ac5fdbf923f4512fc42aae99a38` is retained: after inventory/copy,
every canonical state source is streamed again and compared with its manifest
hash. Equal-size changes with unchanged Windows timestamps therefore fail
before backup publication. This verification remains cancellable.


## Explicit transcription cache inclusion (canonical PR196 reconciliation)

The normalized format-1–5 restore boundary now rejects optional ASR recognition
cache entries unless the manifest explicitly enables generated media. The same
predicate recognizes the current worker and retained worker-history cache
subtrees. Required retained source audio, accepted transcript artifacts, edit
history and ordinary worker results remain part of recovery without that option.
A forged or legacy manifest cannot turn omitted cache data into approved media.

This integrates the narrow canonical PR196 rule into this host's existing
`backup_compat.normalize_manifest` boundary; it does not replace the native
producer or the separate older sidecar format. All 128 manifest-layout cases and
the actual tampered-backup rejection case are retained, with only the existing
test inventory projection adapting `state/` filenames to this host's manifest.

Runtime declarations stay passive after restore. External interpreters, model
weights, credentials and published output folders do not become executable or
approved through this change. The incremental chain uses the same normalized
manifest checks. New composed execution evidence must be recorded at the
candidate SHA; the canonical component's older runs are not qualification of
this larger integration.

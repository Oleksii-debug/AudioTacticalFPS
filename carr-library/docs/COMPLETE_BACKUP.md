# Checked library backups

Choose **Library, Back up all libraries** and a new folder outside the current
library. Keep the returned manifest SHA256 separately from the backup. To recover,
choose **Restore backup to a new folder**, select the backup, and supply that
independently retained checksum. An existing destination is never overwritten.

## Included state

A backup preserves all workspaces, including archived libraries; their reference
catalogs, retained original files, sealed historical revisions, saved evidence,
research notes, topics, tags and saved queries. It also includes these local files
when they exist:

- Reader resume positions and named bookmarks, with their exact source, revision
  and page-text identities.
- Saved reading/narration copies, edit histories, literal pronunciation rules
  and separate recoverable drafts. Independent scripts remain independent scripts.
- Customized main-window keyboard shortcuts.
- Named OCR-language selections and their orientation/model-policy settings.
- Progress notification preferences: Off/Beep, tone volume, notification interval,
  mute and background behavior. These are local output preferences, not live job
  queues or a record of unfinished work.
- API connection profiles: friendly labels, provider, endpoint, model and
  capabilities. Credential references are cleared in the exported copy; the
  original connected profiles are unchanged.
- The API operations ledger, including approval digests, operation IDs, request
  and output hashes, estimated costs, and reserved, uncertain, complete and
  explicitly reconciled records. Request text and API keys are not ledger fields.

The library database and saved profiles/settings are frozen under the same local
writer lock used by those features. The API ledger is captured through SQLite's
backup API in one read transaction, including committed WAL pages. This supplies
one consistent point in time for approval and operation records while a running
paid request can still record its completion. Its later completion need not
appear in the earlier snapshot; the captured reservation remains protected.
This does not capture text still only in an open control: save it or allow its
recovery draft to finish before starting the backup.

Every included file has an exact byte count and SHA256 in the checked manifest.
New backups use version 5, version 6 when saved local transcription or resource
policy state is present, or version 7 when incompatible legacy application state
must be preserved for review. Each has typed sidecars, explicit LOCAL ONLY
restoration and a separate typed pending-import component. Even an empty queue
has an explicit empty component. Restore accepts the exact supported version-1
through version-7 layouts and validates their database schema. Older
backups contain only the state supported by their original format; unknown or
mixed manifest fields are rejected.

The enclosing manifest version and the database schema version are separate.
Current snapshots contain catalog schema V5 with immutable import-publication
receipts. Exact released catalog schemas V3 and V4 still restore unchanged;
opening the restored library performs the checked transactional upgrade. A
migration never invents ownership receipts for historical source revisions.

## Unfinished imports and prepared requests

The pending component includes supported import batches, durable requests,
review attestations and the SQLite import journal, together with all original
bytes they reference. It retains queued, paused, failed and quarantined work,
prepared requests that have never launched a child, and completed journal entries
needed for replay or a partially finished batch. Missing or changed pinned source
bytes fail the backup; the program does not omit those jobs and claim completion.

Batch/request guards are acquired without waiting. Pause a running import before
making a complete backup. The root writer lock prevents catalog publication
while its snapshot is taken. Committed SQLite WAL pages enter the journal through
SQLite's backup API; transient lock files, partial writes and WAL companions do
not become delivered files. Unknown files and orphaned SQLite companions are
rejected, and directory traversal, byte counts and journal rows are bounded.

Restore checks file hashes, the exact journal schema, workspace and publication
bindings, checkpoint digests, source manifests and saved OCR review identities in
private staging. TXT checkpoints are compared with the captured original bytes.
The current retained-format registry also covers accepted queued HTML, Markdown,
email and other supported document types; restoration does not parse them into
new quotations.

Only the data-root and selected source paths are rebased to the new folder. A
prepared request that had neither an asset hash nor a planned manifest receives
the verified captured asset hash before it can be resumed. Existing pins are
preserved. The final rebased request and batch bytes must still fit their readers'
size limits. Original request IDs, batch IDs, work/edition IDs, journal jobs,
runtime/model identities, checkpoints, statuses and exact review attestations
remain bound to the same source.

A stopped child may have committed its source before its reply was lost. The
parent reports completion only after confirmed child termination, an immutable
SQLite receipt owned by that exact request and execution attempt, and a fresh
check of the source bytes, pages, workspace and catalog registration. A peer
import of identical bytes cannot complete the stopped request. Metadata and
configuration participate in the receipt digest; relocated paths and mutable
progress do not.

Backup keeps the immutable receipts in the catalog snapshot. Restore clears
process-bound execution attempts and candidate hints, because no child is
running in the restored folder. Historical results, request IDs and journal
identities remain. An explicit resume obtains a new attempt, validates the
retained inputs and can reuse an existing sealed revision without duplicating
it or transferring its original receipt.

Restoration never starts an import or external retry. Review the restored queue
and explicitly resume the selected work. The normal runtime and review checks
still apply. A later backup preserves both its finished history and remaining
work; no completed journal entries are filtered merely to keep an older manifest
version.

Typed assistant checkpoints and their history also remain included. Every
restored assistant job requires a new review of its current permissions, even
when restored to the same path. The old scope is readable history and does not
authorize execution.

## API restoration and possible charges

Restored profiles contain no usable credential references. Reconnect keys through
**Settings, AI and API connections** when needed. Restoring a backup never opens
the credential vault or sends a request. The default stays **LOCAL ONLY**;
neither the profiles nor the ledger authorize a remote task. A remote task and a
local loopback service still require their normal explicit review and approval.
No old approval digest is rebound to a newly connected profile.

All historical operation IDs and states are preserved, including reconciled
records. A reserved or uncertain operation remains counted against its original
approval, and a previously recorded operation ID cannot be reused. Restoration
does not retry a request, release a reservation, or turn an estimate into an
actual billed amount. Unknown charge status requires reconciliation with the
provider before considering another paid request.

An older backup cannot know about operations performed after its snapshot. It
must not be treated as proof that a later request was never charged. Preserve
the current ledger when comparing an older backup and check provider billing
before repeating work. The backup prevents loss or reinterpretation of the
records it captured; it cannot establish account-wide billing history.

The ledger must have the exact supported two-table schema and valid identifiers,
hashes, integer estimates, state/output combinations and approval references.
Unknown tables, views, triggers and indexes are rejected. The portable SQLite
copy is compacted to exclude deleted records and unused pages. Original approval
limits are represented by an opaque digest, so validation checks the supported
absolute bounds; it does not claim to reconstruct a user's smaller original
budget from that digest.

## Restoration and source integrity

Before publishing a restored folder, the program checks the manifest, database
schema and integrity, original files, state-file schemas, and every reader or
reading-copy source reference. A reference must match the stored workspace,
edition, sealed revision, page/segment kind and index, original hash, and page
text hash. An edited reading copy is never substituted for the original text or
approved as a quotation by restoration.

Archived libraries and older revisions are preserved. The restore process does
not redirect an old bookmark to whichever revision happens to be active now.
Normal reader authorization and source checks still apply when opening it.

A missing, damaged, unexpected, linked or mismatched state file stops restoration
with an error. The program does not silently omit it and report a complete
restore. Files are verified in private temporary staging before the new folder
is published. An interrupted restore retains its incomplete marker and the
application refuses to open that folder. Retry to a different new destination;
the original library and backup remain unchanged.

## Operation budgets and cancellation

The programmatic `create_backup` operation accepts optional `max_bytes`,
`max_files` and `cancelled` arguments. A byte or file allowance covers the
whole delivered backup: the SQLite database, registered originals, every selected
state file, pending component files and the final manifest. Directories and temporary staging links do
not count as delivered files. The final manifest's own serialized bytes count
toward the byte allowance. Limits must be positive integers and cannot exceed
the sum of the existing supported component limits.

The complete selected inventory and manifest are checked before publishing the
destination. A backup that exceeds either caller allowance fails without a valid
completed backup. Cancellation is checked between staging operations, for each
state-file write and publication link, and immediately before publishing the final
manifest. A SQLite snapshot or catalog-original copy operation finishes before the next
cooperative check; the callback does not interrupt those operations internally.

Cancellation during publication may leave an incomplete folder without its final
manifest. Restore rejects that folder; retry to a different new destination.
After the final manifest has been published, a later cancellation does not relabel
that completed backup as cancelled. Existing overwrite protection, component
size limits and exact version-1 through version-7 restore validation still apply.

Pending files consume the existing aggregate byte and file ceilings and the
caller's lower allowances; adding this component does not raise those ceilings.
Its additional limits are 25,000 files, 8 GiB total bytes, a 512 MiB journal,
10,000 journal jobs and 100,000 checkpoints. Each source remains subject to the
existing 64 MiB input limit. The component metadata is part of the final manifest,
not a second delivered manifest. A source present in both catalog originals and
pending originals counts twice because two regular files are delivered.
Pending copy/hash loops check cancellation per chunk; one bounded source read
or SQLite operation may finish before the next cooperative check.

## Boundaries

The current typed adapters do not copy arbitrary neighboring files, API
credentials, runtime model weights, temporary render files or arbitrary external
documents. Incompatible historical state is preserved as opaque local data under
the separate review rules below; those historical bytes are not claimed to be
sanitized or free of old secrets. A selected
pending import contributes only its validated, referenced source bytes. Unreferenced
retained files without a supported request, batch or journal identity are excluded.

Audio Studio projects and their output directories are deliberately stored
outside the private research library. Use **Back up audio project** for their
separate checked ZIP64 snapshot, including project/recovery files, assets, cache,
rendered audio, MIDI and manifests. The library backup does not follow output
directory references or silently collect those external files. Installed
voice/music models also require their separate installation/backup process.
Synchronization and scheduled/encrypted backups are not supplied by this action.

Existing bounded backup budgets still apply: 512 MiB database, 10,000 registered
originals, 8 GiB original bytes; reader state up to 8 MiB; narration copies/drafts
up to 32 MiB per file and 64 MiB combined; keyboard preferences up to 128 KiB; OCR
presets up to 64 KiB; progress preferences up to 8 KiB; API profiles up to 512 KiB
and 1,000 profiles; the API ledger up to 64 MiB with at most 100,000 approvals and
100,000 operations. These are checked resource budgets, not measurements of
large-library capacity. Future streaming/scale work must preserve the same source
and publication guarantees.

Backup destinations and their parents must be stable private local directories
on a filesystem that supports hard links. This workflow coordinates application
writers; it is not a guarantee against an unrelated process maliciously replacing
directories during a backup. Native Windows/NVDA acceptance is tracked separately
from portable file/database regression tests.


## Typed local transcription and shared resource policy (conditional version 6)

A library containing local transcription profiles/jobs or the saved cooperative
resource policy uses complete-backup manifest version 6. A library without those
files still writes the exact version-5 shape unless it contains preserved legacy
state, which selects version 7. Readers accept only the supported exact schemas;
unknown fields and versions fail closed.

The version-6 transcription component streams retained original audio/video,
the job's accepted recognition receipts and every requested accepted export,
separate saved edit history, inert runtime/model inventory records, and the
small resource allowance policy. All payload hashes and sizes are checked against
both the outer manifest and their typed cross-file bindings. Retained input
identities, original recognition, edited derivatives and historical runtime
provenance stay separate. Original source paths are historical labels and are
not opened during backup or restore.

The component shares the existing complete-backup aggregate byte and file limits.
It cannot raise those limits. Large libraries or recordings that exceed the
remaining allowance fail explicitly before publication; no job is silently
dropped to make a snapshot fit. A job that is retaining a source, rendering
recognition or saving an edit holds a shared state lock. Backup reports that
state as busy and leaves the previous work intact.

External Python installations, supplied worker source/helpers, model files,
temporary worker caches and old launch controls, and live resource lease files
are excluded. Their exclusion is recorded in the typed manifest. Accepted host
results and edit history remain readable without those external installations.
Unfinished recognition may need to repeat an unaccepted worker cache after
restore; the backup does not claim to retain every temporary native-engine byte.
External Audio Studio projects still use their separate explicit project archive.

Restore revokes every runtime profile's execution trust and marks saved jobs for
review. It does not load a model, start a worker, recreate a live lease or submit
a remote operation. Incomplete jobs require a freshly inspected, explicitly
trusted compatible runtime and an explicit rebind/resume. A completed job remains
readable from its verified retained input and host receipts without re-execution.
Recognition and reading edits remain unqualified derivatives, without citation
authority. The saved resource allowances are cooperative policy, not a hardware
capacity measurement.

## Canonical backups and preserved application state

The earlier canonical producer used different version-3 and version-4 manifest
field sets from this implementation. Restore recognizes those exact shapes and
checks their catalog schema, originals, pending imports and authenticated raw
state inventory. The numeric version alone never selects compatibility.

Legacy Studio, speech, assistant and translation state remains in
preserved-state/files with a sealed review.json inventory. It is not installed
as active current state. The restored folder displays a review notice before
opening. Recorded engine paths, jobs, provider approvals and quotation authority
are not activated, and no external path is opened. These bytes may retain private
text or old references from the original backup; preservation does not sanitize
them. Semantic migration and resumability remain separate reviewed work.

A subsequent version-7 backup retains that whole inert component together with
all supported current typed state. Both share the existing aggregate file and
byte ceilings. Current transcription and resource policy continue through their
typed adapters, which exclude external engines, model files and live leases.
A mixed legacy/current translation layout with independent cost ledgers fails
explicitly for review rather than combining authorization or dropping records.

The complete format, exact canonical producers, interruption behavior and
verification boundaries are documented in
[CANONICAL_STATE_COMPATIBILITY.md](CANONICAL_STATE_COMPATIBILITY.md).

The desktop passes its Cancel action into both backup and restore. A cancellation
before publication stops the work; a late cancellation or display failure cannot
relabel an already committed result. Recovery still uses a new destination.

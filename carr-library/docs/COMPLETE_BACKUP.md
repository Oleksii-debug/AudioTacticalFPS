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
- Customized semantic keyboard shortcuts and the earlier keybindings file.
- Separate recoverable evidence-note drafts, with exact source and saved-note
  baseline hashes. Restoring a draft does not commit or approve it.
- Optional progress-notification preferences, including mute and quiet settings.
- Named OCR-language selections and their orientation/model-policy settings.
- Managed Audio Studio projects, recovery drafts, local production assets,
  cached speech/music, subtitles, masters, stems and render checkpoints.
- Durable local-assistant job and control records, with the exact approved plan,
  source identities, checkpoint history and original path scope.
- Import requests, batch selections, durable extraction checkpoints and review
  attestations, plus the exact pinned original bytes required to resume them.
- Authenticated historical state from the alternate canonical backup format,
  retained as inert recovery data with its original manifest and byte hashes.

The copy holds the application writer lock and the actual preference, assistant,
draft and render guards used by these features. A concurrent cooperating write
cannot change the database or these saved files halfway through the snapshot.
This does not capture text still only in an open control: save it or allow its
recovery draft to finish before starting the backup.

Every included file has an exact byte count and SHA256 in the checked manifest.
Version 5 combines the current typed sidecars with the pending-import component.
Completed imports also leave journals; populated libraries therefore normally
produce version 5. Libraries without import or historical-state components
retain the existing version-2, reader-only version-3, or managed version-4 shape.

Restore and read-only verification accept exact version-1 through version-5
schemas. The earlier version-3 `reading_state` entry, version-3 filename/root
`sidecars`, and version-4 path/folder `sidecars` remain distinct supported
formats. The alternate canonical version-3 `state` folder and version-4
`state` plus `pending` folder are also recognized by their complete exact
field sets, including `generated_audio_included`. Mixed shapes are rejected.
Older backups contain only the components their original format captured.

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

A reading copy handed to Audio Studio retains the saved revision's provenance
and immutable original. Later reading-editor changes do not invalidate that
handoff. Its optional library location must still match the historical database.
Evidence drafts retain their original source and saved-note baselines; recovery
checks them again before applying an edit.

Assistant records are restored byte-for-byte. Their original data directory,
input folders, output folder, artifact paths and plan hash are never rewritten.
In a new data directory, **Saved jobs** shows that the plan needs review and
disables direct resume. Selecting it copies its steps into an unapproved goal;
choose the current library and folders, edit explicit path arguments as needed,
then use **Preview** and **Run** to authorize a new job. The historical record is
kept unchanged. Missing external paths or one damaged local job do not hide the
other saved jobs; corruption still prevents a successful complete backup.

A missing, damaged, unexpected, linked or mismatched state file stops restoration
with an error. The program does not silently omit it and report a complete
restore. Files are verified in private temporary staging before the new folder
is published. An interrupted restore retains its incomplete marker and the
application refuses to open that folder. Retry to a different new destination;
the original library and backup remain unchanged.


## Resuming pending imports

The pending component contains a standalone SQLite online snapshot of
`imports.sqlite`, canonical request/batch/review records, and exact pinned
originals. Backup holds the application writer and then the existing sorted
batch/request locks with nonblocking acquisition. Active imports must stop or
pause first. Inventory checks reject newly introduced or replaced records during
the snapshot. The online snapshot includes committed WAL content and is
normalized to a self-contained rollback-journal file.

Restore checks the journal schema, SQLite integrity, exact job identities,
ordered checkpoint hashes, workspace/edition bindings, sealed publications,
review attestations, request/batch relationships, and original bytes before
publication. TXT checkpoint text must agree with its exact original segments.
Only explicit `data_dir` and source-location fields are rebound to retained
files in the new library; IDs, states, content identities and reviews survive.
Saved external paths are never opened during restoration or verification.
Runtime-bound OCR jobs still require their matching qualified runtime before
resume; weights and executables are not part of the backup.

Pending limits are 512 MiB per journal, 25,000 component files, 8 GiB combined,
10,000 jobs, 100,000 checkpoints, and the current 64 MiB bound for each source
input. Verification streams component checksums and copies only the SQLite
journal into private staging; it does not duplicate all original assets. Source
classification and exact TXT checks retain the existing bounded source-input
allocation. Restore copies to private staging and checks cancellation at each
1 MiB copy block. Snapshot source acquisition remains one bounded input at a
time, with cooperative cancellation between those operations.

## Historical state from alternate schemas

An alternate canonical backup's state files are preserved under
`state/historical-backups/<original-manifest-sha256>/`. The exact original
`manifest.json` is retained. Payloads are stored under `files/` using the
SHA256 of each original filename as its storage name; the manifest preserves the
original filename, byte count and content hash. Empty files and deeper historical
paths therefore survive without activating a filename in a live namespace.

These bytes are historical recovery data. They do not become current assistant
approvals, translation jobs, provider credentials, or Audio Studio projects.
Current services require their own explicit import/review and typed validators.
The historical component is a registered immutable backup producer: future
backup, verify and restore check its manifest identity, exact inventory and all
inner content hashes, and retain it again. A newly signed outer manifest cannot
hide corruption of an inner historical payload.

The reader also accepts the canonical common-v4 envelope (`sidecars` with
`filename` entries at the backup root and `generated_audio_included`) and the
older state/reading-v5 envelope (`state`, `reading_state`, and `pending`). These
are distinguished by their exact field sets from the current path/sidecars-v4
and v5 formats. A missing pending component is accepted only in the historical
v5 format where it was explicitly nullable. Empty common-v4 inventories remain
valid snapshots. Mixing envelopes or shadowing catalog/originals paths fails.

For these formats, declared reading positions, bookmarks, canonical reader
sessions and supported reading/preferences files additionally restore to their
current registered locations. They pass the same bounded decoder and exact
historical database/source checks as a current backup. Only declared reader
files are read; alternate assistant/provider directories are never interpreted
as current service state. All original sidecar bytes and the complete original
manifest also remain in the inert history archive. The common-v4 and legacy-v5
archive keys hash root-relative filenames; older state-v3/v4 archives retain
their existing state-relative hash convention.

The distinct canonical `progress-preferences.json` version-1 envelope maps its
integer percentage volume and background/media preferences without rewriting
the file. Imported speech modes remain muted and carry no voice identity until
an explicit current voice selection. The original bytes survive restore and
subsequent backups. This does not activate a stored executable or voice path.

Restore budgets include both the original archive and additional live reader
copies. Common-v4 metadata remain limited to 512 MiB and declared generated
media to 32 GiB; archive streaming and the existing portable path/alias checks
remain bounded. Source bytes are rehashed after copying, so same-size edits
hidden by coarse or restored timestamps cannot publish a completed restore.
Retained foreign producer metadata and media are checksum-authenticated recovery
bytes, not authorization to resume another runtime or a new codec qualification.
`tests/storage/test_canonical_common_backup.py` covers exact format roundtrips,
read-only verification, bookmark/source binding, nested reader history, pending
import resumption, inactive old profiles, budgets, mutation and cancellation.

## Boundaries

The backup does not copy arbitrary neighboring files, API credentials, runtime
model weights or temporary render files. Pending imports retain only explicitly
planned original bytes whose size, format and SHA256 match their saved request
or batch. No neighboring external files are discovered. Missing or changed
pinned originals stop a complete backup with an error; unrelated unreferenced
retained bytes are omitted. Authenticated historical state is preserved verbatim,
including whatever settings its original manifest declared.
External Audio Studio projects retain their separate verified ZIP64
backup; use **Save in library** to include an explicitly selected project in the
managed snapshot. No external directories are searched automatically. The optional
[portable snapshots and transfer](PORTABLE_SNAPSHOTS.md) feature reuses this
checked backup format, with an explicit encrypted or plain export. It is not
continuous synchronization or a scheduled-backup service.

Assistant snapshots include only `state/assistant/<job-id>.json` and its optional
`<job-id>.control.json`. A job publication takes the application writer guard,
then the assistant state guard. Backup holds these guards in the same order and
then takes sorted job guards. Only the backup's own currently held native job
lock can be borrowed, in the same process and thread. Another active job is
refused; copied thread contexts, forked processes and nested execution cannot
borrow the exception. This preserves the assistant's own last durable running
checkpoint without self-deadlock. The application writer lock is never bypassed
or reacquired through a blanket reentrancy exception.

An unfinished operation remains unfinished. External assistant outputs are not
implicitly captured; pending imports have their own typed component rather than
being inferred from assistant checkpoints. A concurrent checkpoint
writer receives a nonblocking busy/retry error and retains its preceding saved
state. Restore and re-backup preserve the original approved data directory and
external paths without granting any new execution authority.
Unknown files inside the managed assistant namespace, orphan controls, links,
malformed records and mismatched committed source references are rejected.
Recognized ordinary lock and atomic temporary files are excluded.

Managed audio takes its draft and renderer locks after the library writer lock.
Busy jobs produce a retry message. Restore preserves project/recovery bytes and
their baselines; opening a managed project binds output to its new local audio
folder, never the original library. See [audio backup details](LIBRARY_BACKUP.md)
for the supported namespaces, managed sidecar layout and explicit long-audio budgets.

Existing bounded backup budgets still apply: 512 MiB database, 10,000 registered
originals, 8 GiB original bytes; reader state up to 8 MiB; narration copies/drafts
up to 32 MiB per file and 64 MiB combined; legacy keyboard preferences up to
128 KiB, semantic keymaps up to 64 KiB, evidence drafts up to 8 MiB, progress
preferences up to 16 KiB and OCR presets up to 64 KiB. Assistant state permits at
most 1,000 jobs and 1,000 matching controls, 64 MiB per job, 1 KiB per control and
64 MiB combined; the caller's overall sidecar file/byte budget also applies.
These are checked resource budgets, not measurements of
large-library capacity. Future streaming/scale work must preserve the same source
and publication guarantees.

`verify_backup(source, expected_manifest_sha256)` performs the same manifest,
database, original-file, sidecar and source-binding checks as restore without
publishing a library. It copies SQLite into private temporary staging and streams
audio and other sidecars read-only without duplicating a multi-hour production.
The source backup receives no locks or recovery writes. Its pending journal is
also opened only through a verified private copy, including for WAL-mode headers.

The assistant forwards pause, close and deadline signals into backup. Registered
sidecars check cancellation at each 1 MiB block. The worker releases snapshot
locks before recording its paused checkpoint; closing its dialog does not write
a competing control file. Private cancelled staging is removed before a final
backup is published. SQLite snapshotting and original-asset copying still do not
check cancellation inside every copy operation; a pause there takes effect at
the next cooperative checkpoint. Existing completed backups are never replaced.

Backup destinations and their parents must be stable private local directories
on a filesystem that supports hard links. This workflow coordinates application
writers; it is not a guarantee against an unrelated process maliciously replacing
directories during a backup. Native Windows/NVDA acceptance is tracked separately
from portable file/database regression tests.

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
- Customized main-window and Reading Desk keyboard shortcuts, including
  preserved legacy preference files.
- Recoverable research-note drafts, bound to their saved evidence identities.
- Named OCR-language selections and their orientation/model-policy settings.
- Audio Studio projects saved in the library, their exact recovery drafts, and
  the input sounds used by saved, recovery and undo-history scripts. Input WAVs
  are checked by their content, including portable uppercase or non-WAV names.
- Completed managed audio, accepted render/cache records and resumable music
  checkpoints. The default complete backup includes them. An API caller can
  explicitly use `include_generated=False`; saved projects, recovery drafts and
  input sounds are still included.
- Validated local-assistant checkpoints when that producer is installed.
  Their original folder permissions remain unchanged; restoration does not run
  a saved plan or authorize it against a different data folder.

The copy is taken under the same local writer lock used by these features. A
concurrent cooperating write cannot change the database or these saved files
halfway through the snapshot. This does not capture text still only in an open
control: save it or allow its recovery draft to finish before starting the backup.

Every included file has an exact byte count and SHA256 in the checked manifest.
New backups use manifest version 4 and one explicit sidecar inventory, including
when that inventory is empty. Restore also accepts versions 1 and 2 and the
three previously defined version-3 layouts: a single reading-state descriptor,
the reader/editor sidecar list, and the typed application-state list. Ambiguous
mixtures are rejected. Every format must match its exact database schema; older
backups contain only the state that existed in their original format.

The reader also recognizes the historical managed-audio version-4 layout. Its
exact field set and `path` entries under `sidecars/` distinguish it from the
current `filename` entries under the backup root with
`generated_audio_included`. Mixed forms are rejected, and verification never
rewrites a legacy manifest. New backups use only the current writer/registry.

In Audio Studio, **Save** on a new project with no external output folder saves
inside the library. **Saved library projects** reopens these projects. Explicit
**Save As** keeps the standalone-file workflow and separate audio snapshots.
Externally saved projects and output folders remain outside library backups.

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

Managed Studio output folders are rebound to the restored library in memory.
The saved project and recovery bytes remain unchanged so that recovery still
authenticates its original saved revision. An unrendered project also receives
its own empty audio folder. A stale recovery draft is preserved without silently
changing its saved-base hash.

A missing, damaged, unexpected, linked or mismatched state file stops restoration
with an error. The program does not silently omit it and report a complete
restore. Files are verified in private temporary staging before the new folder
is published. An interrupted restore retains its incomplete marker and the
application refuses to open that folder. Retry to a different new destination;
the original library and backup remain unchanged.

`verify_backup(source, expected_manifest_sha256)` applies the same schema,
source-binding and file checks without restoring a library or copying audio to
a second temporary tree. Media is hashed in bounded chunks. Known production
hardlinks are permitted only inside a fully accounted managed project; backup
and restored copies are independent files. The completion manifest is published
only after temporary payload links are removed, so a process exit immediately
after completion still leaves a verifiable backup.

## Boundaries

The backup does not copy arbitrary neighboring files, API credentials, runtime
model weights, temporary render files or external documents. Pending import
queues/journals and unpublished retained originals are not included in this
version; complete those imports before using a backup to move all work to another
computer. Existing typed studio project adapters are supported only when their
own validators are installed. Rendered audio in external user-selected folders,
sync and scheduled/encrypted backups are not included. Unrecognized files under
an application state directory stop the backup with a visible error.

Existing bounded backup budgets still apply: 512 MiB database, 10,000 registered
originals, 8 GiB original bytes; reader state up to 8 MiB; narration copies/drafts
up to 32 MiB per file and 64 MiB combined; current keyboard preferences up to
64 KiB (legacy preferences up to 128 KiB); OCR presets up to 64 KiB. The complete
reader/preferences inventory is limited to 512 MiB. Managed media is streamed
under a 128 GiB default total sidecar budget, a 32 GiB per-media-file budget,
64 MiB per audio JSON document and 100,000 total sidecar files. Optional stricter
caller limits, whole-backup byte limits, cancellation and available-disk checks
apply before completion. These are checked resource budgets, not measurements of
large-library capacity. Future streaming/scale work must preserve the same source
and publication guarantees.

Backup destinations and their parents must be stable private local directories
on a filesystem that supports hard links. This workflow coordinates application
writers; it is not a guarantee against an unrelated process maliciously replacing
directories during a backup. Native Windows/NVDA acceptance is tracked separately
from portable file/database regression tests.

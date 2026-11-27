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
- Validated local-assistant checkpoints when that producer is installed.
  Their original folder permissions remain unchanged; restoration does not run
  a saved plan or authorize it against a different data folder.
- Pending import requests and batch plans, extraction journals/checkpoints,
  validated review records and the retained original bytes needed to resume.
  Missing or changed required inputs stop the backup before publication.
- Typed transcription state: retained media, job/status/result receipts,
  retained outputs and separate edit revisions. Completed and partial result
  claims are reverified against their receipts; other retained result files stay
  inert. A running recognition job must be stopped before backup; restoring does
  not execute its worker.

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

Pending import locations are rebound to the new data folder after their source,
workspace, job and review identities have been validated. Restore does not resume
jobs automatically. Process-attempt identifiers and unverified publication
candidates are discarded; durable import-ownership receipts remain in the checked
catalog database. Explicit resume rechecks retained inputs and journal state.
Older supported database schemas restore unchanged and migrate when opened.

A missing, damaged, unexpected, linked or mismatched state file stops restoration
with an error. The program does not silently omit it and report a complete
restore. Files are verified in private temporary staging before the new folder
is published. An interrupted restore retains its incomplete marker and the
application refuses to open that folder. Retry to a different new destination;
the original library and backup remain unchanged.

## Boundaries

The backup does not copy arbitrary neighboring files, API credentials, runtime
model weights, rebuildable recognition caches or unrelated external documents.
Required originals belonging to supported pending import plans are captured and
verified by that producer; this is not permission to copy other neighboring data.
External studio projects and rendered audio use their separate project backup
and export workflow. Sync and scheduled/encrypted backups are not included.
Unrecognized files under an application state directory stop the backup with a
visible error.

Existing bounded backup budgets still apply: 512 MiB database, 10,000 registered
originals, 8 GiB original bytes; reader state up to 8 MiB; narration copies/drafts
up to 32 MiB per file and 64 MiB combined; current keyboard preferences up to
64 KiB (legacy preferences up to 128 KiB); OCR presets up to 64 KiB. The complete
sidecar inventory, including transcription media/results/edits, is limited to
512 MiB. The separate pending-import component is bounded by 8 GiB and 25,000
files, including a journal up to 512 MiB, at most 10,000 jobs and 100,000
checkpoints. Optional stricter whole-backup budgets and cancellation apply.
These are checked resource budgets, not measurements of large-library capacity. Future streaming/scale work must preserve the same source
and publication guarantees.

Backup destinations and their parents must be stable private local directories
on a filesystem that supports hard links. This workflow coordinates application
writers; it is not a guarantee against an unrelated process maliciously replacing
directories during a backup. Native Windows/NVDA acceptance is tracked separately
from portable file/database regression tests.

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
- Managed Audio Studio projects, recovery drafts, local production assets,
  cached speech/music, subtitles, masters, stems and render checkpoints.

The copy is taken under the same local writer lock used by these features. A
concurrent cooperating write cannot change the database or these saved files
halfway through the snapshot. This does not capture text still only in an open
control: save it or allow its recovery draft to finish before starting the backup.

Every included file has an exact byte count and SHA256 in the checked manifest.
The reader/editor/settings files use manifest version 3; libraries with none of
these files retain the existing version-2 format. A snapshot that also contains
managed audio uses version 4 with streamed sidecars in a separate folder.
Restore accepts supported version-1 through version-4 backups and validates their
exact database schema. The earlier version-3 `reading_state` entry and the
current version-3 `sidecars` list remain supported as distinct exact schemas.
Combining the two is rejected; new backups use the sidecars list. Older
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

A missing, damaged, unexpected, linked or mismatched state file stops restoration
with an error. The program does not silently omit it and report a complete
restore. Files are verified in private temporary staging before the new folder
is published. An interrupted restore retains its incomplete marker and the
application refuses to open that folder. Retry to a different new destination;
the original library and backup remain unchanged.

## Boundaries

The backup does not copy arbitrary neighboring files, API credentials, runtime
model weights, temporary render files or external documents. Pending import
queues/journals and unpublished retained originals are not included in this
version; complete those imports before using a backup to move all work to another
computer. External Audio Studio projects retain their separate verified ZIP64
backup; use **Save in library** to include an explicitly selected project in the
managed snapshot. No external directories are searched automatically. Sync and
scheduled/encrypted backups remain separate work.

Managed audio takes its draft and renderer locks after the library writer lock.
Busy jobs produce a retry message. Restore preserves project/recovery bytes and
their baselines; opening a managed project binds output to its new local audio
folder, never the original library. See [audio backup details](LIBRARY_BACKUP.md)
for the supported namespaces, version-4 layout and explicit long-audio budgets.

Existing bounded backup budgets still apply: 512 MiB database, 10,000 registered
originals, 8 GiB original bytes; reader state up to 8 MiB; narration copies/drafts
up to 32 MiB per file and 64 MiB combined; keyboard preferences up to 128 KiB; OCR
presets up to 64 KiB. These are checked resource budgets, not measurements of
large-library capacity. Future streaming/scale work must preserve the same source
and publication guarantees.

Backup destinations and their parents must be stable private local directories
on a filesystem that supports hard links. This workflow coordinates application
writers; it is not a guarantee against an unrelated process maliciously replacing
directories during a backup. Native Windows/NVDA acceptance is tracked separately
from portable file/database regression tests.

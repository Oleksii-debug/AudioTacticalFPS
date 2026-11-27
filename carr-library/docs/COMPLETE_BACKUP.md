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

The copy holds the application writer lock and the actual preference, assistant,
draft and render guards used by these features. A concurrent cooperating write
cannot change the database or these saved files halfway through the snapshot.
This does not capture text still only in an open control: save it or allow its
recovery draft to finish before starting the backup.

Every included file has an exact byte count and SHA256 in the checked manifest.
The reader/editor/settings files use manifest version 3; libraries with none of
these files retain the existing version-2 format. A snapshot that also contains
managed audio or assistant jobs uses version 4 with streamed sidecars in a separate folder.
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

## Boundaries

The backup does not copy arbitrary neighboring files, API credentials, runtime
model weights, temporary render files or external documents. Pending import
queues/journals and unpublished retained originals are not included in this
version; complete those imports before using a backup to move all work to another
computer. External Audio Studio projects retain their separate verified ZIP64
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

An unfinished operation remains unfinished; external outputs and pending import
journals are not captured by this checkpoint integration. A concurrent checkpoint
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
for the supported namespaces, version-4 layout and explicit long-audio budgets.

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
The source backup receives no locks or recovery writes.

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

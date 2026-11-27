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
- Live-reading positions at an exact synthesized chunk boundary, with the source
  text length and selected voice controls. Restoring never starts playback;
  explicit resume rechecks the source and starts the saved chunk again.
- Saved reading/narration copies, edit histories, literal pronunciation rules
  and separate recoverable drafts. Independent scripts remain independent scripts.
- Customized main-window and reader keyboard shortcuts and recoverable evidence-note drafts.
- Explicit progress-feedback settings, including Off/Beep/Speech preference,
  volume, mute and quiet/background behavior; restoration itself plays no sound.
- Named OCR-language selections and their orientation/model-policy settings.
- Explicit dordiai-voices runtime declarations, restored disabled. Neither backup
  validation nor restoration opens or executes the declared private runtime.
- Saved audio projects, immutable script revisions, accepted speech/music chunks,
  chapters and master audio through the existing checked studio backup format.
- Nonsecret provider settings and usage receipts, plus the local assistant's
  durable result history.
- Import requests, batches, review receipts and their consistent SQLite journal,
  including retained originals that have not yet entered the library catalog.

The library database and its retained originals are copied under their local
writer lock. Import receipts use their existing short request/batch locks;
a busy receipt causes a visible retry error. Other saved files are checked for
unchanged identity and bytes while copied. This does not capture text still only
in an open control: save it or allow its recovery draft to finish before starting
the backup.

Every included file has an exact byte count and SHA256 in the checked manifest.
The reader/editor/settings files use manifest version 3. Version 4 adds typed
production and recovery components without changing the version-3 sidecar
representation. Libraries with neither kind of saved state retain version 2.
Restore also accepts supported version-1, version-2 and version-3 backups and
validates their exact database schema. Older backups contain only the state that
existed in their original format.

The database for each independent component is copied using SQLite's online
backup API. An uncommitted write is not included. Completed studio chunks remain
reusable; an active render is restored as interrupted. Stored project paths are
rebound only where the studio format explicitly defines an owned output. The
original authored script and external input paths remain unchanged.

Provider restoration retains spending reservations and request hashes, clears
OS-vault credential references, disables every provider and selects LOCAL_ONLY.
A reserved request is retained as uncertain, preventing a restore from silently
authorizing another paid call. Reconnect and review a provider explicitly before
using it. Restored assistant write receipts require review and a new approved
plan; restoration itself never runs a tool. Existing result text is preserved as
history. Library-scoped assistant backups omit independent studio, provider and
agent state and global preferences because selecting a library does not authorize
those other projects and settings.

## Restoration and source integrity

Before publishing a restored folder, the program checks the manifest, database
schema and integrity, original files, state-file schemas, and every reader or
reading-copy source reference. A reference must match the stored workspace,
edition, sealed revision, page/segment kind and index, original hash, and page
text hash. An edited reading copy is never substituted for the original text or
approved as a quotation by restoration.
Live-reading offsets also require the exact authenticated source's Unicode text
length. A forged length or a position outside that text stops restoration.

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
model weights, temporary render files or external documents. Pending imports
whose source was already retained can resume from those exact bytes after the
external file disappears. A queued item that never retained its source still
requires the original selected file. Original external paths are history, never
new restore output destinations.

Import batches retain their original external path syntax. The current queue
engine requires absolute paths native to the restored operating system; moving
a batch between incompatible operating systems is rejected before a new data
folder is published. Moving between folders or computers on Windows retains
the original path history. An already retained import request can still use its
verified original bytes; a never-retained source must remain available at its
explicitly selected path.

OCR model policy and selected languages survive in saved presets/import records.
Managed model packs themselves remain separately installed, including packs in
the shared per-user model directory or an explicitly configured external root.
Restore never scans that external root. Reinstall the exact reviewed packs on a
new computer; unavailable models remain an explicit engine error. Voice packs,
semantic models, MusicGen weights and other runtimes follow the same rule.
Derived semantic cache entries are rebuilt from the restored authorized sources.
Scheduled, synchronized and encrypted backups are separate capabilities.

Existing bounded backup budgets still apply: 512 MiB database, 10,000 registered
originals, 8 GiB original bytes; reader state up to 8 MiB; narration copies/drafts
up to 32 MiB per file and 64 MiB combined; keyboard preferences up to 128 KiB; OCR
presets up to 64 KiB.
Live-reading positions are bounded to 1 MiB and 1,000 sources; dordiai runtime
declarations to 64 KiB. Recovery components add at most 100,000 files, 64 GiB total,
32 GiB per file, 512 MiB per auxiliary SQLite database and 1,000 audio projects;
the manifest is bounded to 64 MiB. The existing studio limits also apply.
These are checked resource budgets, not measurements of
large-library capacity. Future streaming/scale work must preserve the same source
and publication guarantees.

Backup destinations and their parents must be stable private local directories
on a filesystem that supports hard links. This workflow coordinates application
writers; it is not a guarantee against an unrelated process maliciously replacing
directories during a backup. Native Windows/NVDA acceptance is tracked separately
from portable file/database regression tests.

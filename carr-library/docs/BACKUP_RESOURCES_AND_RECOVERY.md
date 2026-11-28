# Full and incremental backups

Use **Library → Back up all libraries** for a complete snapshot, or **Library →
Create incremental backup...** for a generation that stores changed files and
depends on a previously selected backup. Both routes use the same library
snapshot, source validation and recovery code.

Keep the manifest SHA256 displayed after creation separately from the backup
folder. A folder name, a checksum found inside that folder, or a successful
previous restore does not replace this independently retained checksum.

## Choose resources for this operation

The native **Backup options** dialog offers **Use reviewed resource limits for
this operation**. Leave it unchecked to retain the ordinary full-backup limits.
Select it to review every count, byte, time, memory and temporary-disk field.
Byte fields use whole MiB; counts are whole numbers. **Include generated audio
stored inside this library** controls the existing optional generated-audio
inventory. It does not include an arbitrary external export folder.

The review covers the database, one retained TXT/PDF original, all retained
originals, pending files and journal, jobs, checkpoints, source segments/pages
and the manifest. Larger originals are supported through the streamed TXT/PDF
routes. Other containers keep their parser safety bounds. The finite manifest
decoder remains bounded.

Memory and staging space are requirements as well as limits: the operation
checks current availability before copying and accounts for its own staged
data. Choose values that fit the selected computer and volume. These checks are
cooperative accounting, not an operating-system quota. Incremental verification
also needs room for the materialized logical snapshot and its required
generations, even when the new generation stores little changed data.

Resource choices apply to this explicit operation. The archive cannot raise
them, and restoring a large snapshot requires reviewing sufficient values again.
Cancelling the review leaves the previous choice unchanged. Cancellation before
final publication does not publish a completed snapshot; if destination
publication already began, an incomplete folder may remain. A completed snapshot remains
completed when a later UI closes; retain its reported checksum. Keep interrupted
output separate until you inspect its status and choose a fresh destination.

## Create and restore a full backup

1. Choose **Library → Back up all libraries**, review options and choose a parent
   folder. The application creates a new timestamped backup folder.
2. Wait for **Backup complete**, then retain its folder and displayed checksum.
   Inspect the scope report for included, excluded and external components.
3. To recover, choose **Library → Restore backup to a new folder**. Review the
   restore resources, select the backup and supply the independently kept
   checksum.
4. Choose a parent folder for a new restored library. Existing library and backup
   folders are not overwritten.
5. Read **Recovery details**. The final question offers to open the restored
   library; choosing No keeps the current library open.

The primary command and its installed console entry are equivalent:

```text
python -m carr_library --data-dir "LIBRARY_FOLDER" --backup "NEW_FULL_BACKUP_FOLDER"
research-library --restore "FULL_BACKUP_FOLDER" --data-dir "NEW_RESTORED_LIBRARY_FOLDER" --manifest-sha256 "FULL_MANIFEST_SHA256"
```

Add `--backup-include-generated` to the full creation command when you explicitly
want its supported generated audio.

## Create an incremental generation

1. Choose **Library → Create incremental backup...** and review this operation's
   options.
2. Select its immediate base folder and enter that generation's separately
   retained manifest checksum.
3. In **Required backup generations**, use **Add required base…** for each earlier
   generation required by that immediate base, ending with the full backup.
   Supply each folder and its independent checksum. If the immediate base is
   already a full backup, leave this list empty.
4. Review **Logical snapshot limit (MiB)** and choose **Use selected base chain**.
5. Choose a parent folder for the new generation. Keep the resulting checksum
   separately when **Incremental backup complete** appears.

A generation records the current logical inventory: unchanged files are inherited,
changed files are stored, and files absent from that inventory are not restored.
Every required parent remains necessary. Moving a whole chain is allowed when
you explicitly select its current locations and its unchanged manifest hashes.
Do not delete an ancestor because the newest folder looks complete.

At most eight incremental layers may follow a full backup. Create a new full
backup to begin another chain. This bound and the byte/time limits are checked
again during verification and restore.

Use either the primary dispatcher or the standalone command:

```text
python -m carr_library backup-incremental create --data-dir "LIBRARY_FOLDER" --output "NEW_DELTA_1_FOLDER" --base "FULL_BACKUP_FOLDER" "FULL_SHA256"
carr-backup-incremental create --data-dir "LIBRARY_FOLDER" --output "NEW_DELTA_2_FOLDER" --base "DELTA_1_FOLDER" "DELTA_1_SHA256" --ancestor "FULL_BACKUP_FOLDER" "FULL_SHA256"
```

Repeat `--ancestor FOLDER SHA256` for all required earlier generations. Creation
requires an existing ordinary library; a mistyped library path does not create a
replacement library. Add `--include-generated` only when requested.

## Verify or restore an incremental chain

Use **Library → Verify incremental backup chain...** to check the selected
generation and all explicitly selected ancestors without opening a recovered
library. Use **Library → Restore incremental backup to a new folder...** to
recover into a new folder. Both actions ask for the selected checksum, required
generations, logical snapshot limit and operation resources.

```text
python -m carr_library backup-incremental verify --source "DELTA_2_FOLDER" --sha256 "DELTA_2_SHA256" --base "DELTA_1_FOLDER" "DELTA_1_SHA256" --base "FULL_BACKUP_FOLDER" "FULL_SHA256"
carr-backup-incremental restore --source "DELTA_2_FOLDER" --sha256 "DELTA_2_SHA256" --base "DELTA_1_FOLDER" "DELTA_1_SHA256" --base "FULL_BACKUP_FOLDER" "FULL_SHA256" --output "NEW_RESTORED_LIBRARY_FOLDER"
```

Missing parents, wrong hashes, changed parent bytes and extra undeclared files
are refusals. Select the correct complete chain; the program does not search for
a different parent or trust a supplied archive to authorize one. Incremental
restore reports its new folder; open it explicitly through **Library → Open
existing library folder**.

All three incremental commands accept `--max-bytes` for the logical snapshot;
its default is 64 GiB. This is separate from the resource policy's staging space.
Neither limit means every computer can complete a snapshot of that size.

## Resource flags

The following flags work with primary full backup/restore and with both
incremental command routes. Supplying any resource flag selects an explicit
typed policy; omitted fields keep its displayed defaults. With no resource
flags, the full-backup route retains its historical behavior. The incremental
wrapper still applies its own bounded operation defaults.

| Flag | Meaning |
| --- | --- |
| `--backup-reviewed-resources` | Select the displayed typed defaults explicitly. |
| `--backup-database-mib` | Database snapshot byte limit. |
| `--backup-original-mib` | One retained TXT/PDF original byte limit. |
| `--backup-assets` / `--backup-originals-mib` | Original count and aggregate original bytes. |
| `--backup-journal-mib` | Import journal byte limit. |
| `--backup-pending-files` / `--backup-pending-mib` | Pending file count and aggregate bytes. |
| `--backup-jobs` / `--backup-checkpoints` | Saved job and checkpoint counts. |
| `--backup-segments` | Segments/pages per original. |
| `--backup-manifest-mib` | Manifest byte limit. |
| `--backup-seconds` | Finite allowance for this complete operation. |
| `--backup-memory-mib` / `--backup-staging-mib` | Required available memory and staged-disk allowance. |

For example, append `--backup-original-mib 256 --backup-segments 4000
--backup-seconds 600` only after reviewing the remaining limits and current
machine capacity. This example selects limits; it is not a throughput or
large-corpus qualification.

## What recovery does not activate

Recovered library data, saved queues and pending checkpoints remain passive.
Recovery starts no import, speech, playback, transcription, translation or
external engine. Current hardware, source roots and execution permissions still
require the relevant explicit review. A stored queue never grants permission
to play automatically.

Prior import worker-attempt, publication-candidate and scan-review authority is
revoked. Retained checkpoints preserve recorded source data; valid saved rows
can be continued through a new explicit action. Extending a restored external
source traversal requires a fresh reviewed plan. See [Import Center](IMPORT_CENTER.md)
for page and work-slice controls.

After a failure or cancellation, inspect the reported status and keep the
original library plus every selected backup generation unchanged. Recovery
always targets a new folder; it does not repair an incomplete destination by
silently continuing execution. Automated source, installed-package and native
keyboard results are reported separately from manual screen-reader or physical
audio acceptance.

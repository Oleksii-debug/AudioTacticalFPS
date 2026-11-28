# Incremental library backups

## Workflow

Use **Library → Back up all libraries** for a full base. Keep its manifest SHA256 separately.

Use **Library → Create incremental backup** to select that full backup or a previous incremental layer. Paste its independently retained manifest checksum. For a layered base, the dialog asks for each required parent directory using the checksum authenticated by its child. The app never follows a path embedded in a backup or silently searches other folders.

Choose an existing parent folder. The application creates a new dated directory. Its completion report includes the new checksum, base checksum, snapshot checksum, logical bytes and stored bytes. Keep every required layer and the full base. An incremental layer by itself cannot restore unchanged files.

**Verify incremental backup chain** authenticates the selected chain without executing saved work. **Restore incremental backup to a new folder** verifies it, privately materializes a complete canonical snapshot, and calls the existing full restore API. Existing folders are never overwritten. Open the resulting library through the normal Open existing library folder action. Imported originals, history, evidence, supported drafts and journals use the same typed validation as a full backup. External engines, local worker trust and remote execution approvals remain subject to the canonical restore review.

All three native commands run in the existing desktop background job and use its cancellation. Cancel before publication leaves no completed output. An interruption during exclusive directory publication can leave an incomplete folder without its final manifest; it is rejected on verification. Existing bases are never modified.

## CLI

The installed launcher exposes the same service:

```powershell
carr-library backup-incremental create --data-dir C:/Library --output D:/Backups/Delta1 --base D:/Backups/Full FULL_SHA256
carr-library backup-incremental verify --source D:/Backups/Delta1 --sha256 DELTA_SHA256 --base D:/Backups/Full FULL_SHA256
carr-library backup-incremental restore --source D:/Backups/Delta1 --sha256 DELTA_SHA256 --base D:/Backups/Full FULL_SHA256 --output C:/RestoredLibrary
```

For a later layer, create with the immediate parent in `--base FOLDER SHA256` and each older parent in repeated `--ancestor FOLDER SHA256`. Verify/restore accepts repeated `--base FOLDER SHA256` in any order. The command requires an existing source library for creation. JSON output reports completion receipts; nonzero exit reports failure.

Creation optionally accepts `--include-generated`. The default excludes regenerable audio caches as in the full-backup API. `--max-bytes` sets the complete logical snapshot allowance, default 64 GiB. Existing typed limits for database, media, sidecars and pending inputs still apply.

## Storage and integrity

A layer has three components:

- `manifest.json`: strict `carr.library-incremental.v1` descriptor, required parent checksum, complete snapshot checksum and ordered changed-file inventory.
- `snapshot.json`: the exact canonical full-backup manifest.
- `files/`: only files whose exact filename, size or SHA256 differs from the parent snapshot.

No shared hardlinks remain after staging is removed. Unchanged files are inherited only when all three recorded properties match. Removal is represented by absence from the complete new snapshot. A restored source is never retargeted merely because its title or text resembles another source.

Verification rejects missing bases, wrong digests, changed inherited bytes, changed stored bytes, extra namespace entries, links, invalid paths and invalid typed content even if someone recomputes outer checksums. Every parent is explicitly selected. The chain is bounded to eight incremental layers plus a full base; creation refuses an additional layer and asks for a new full backup.

This is incremental **storage**, using the current full snapshot producer for consistency. Creation first builds a full private snapshot. Verification/restore can materialize selected layers privately. They therefore need staging space in addition to the final changed bytes; this version does not claim incremental SQLite I/O or a new backup database. File copying and hashing are bounded and cancellable. Actual base bytes are checked again after snapshot work and before publishing a new layer.

## Acceptance status

The implementation reuses `library_backup`, typed sidecars, pending-input recovery and existing restore authority. Dedicated tests exercise actual SQLite databases, retained Unicode originals, reader Undo/bookmarks, two-layer restore after deleting the source, corruption and resigned-invalid typed data, cancellation, insufficient space, competing destination ownership, base drift during creation, native action scope and CLI receipts.

At this source checkpoint these new tests are **NOT_RUN** in the authoring environment because the executor is unavailable. Exact-head CI is required before changing that status. Native human NVDA and power-loss qualification are separate from source tests.

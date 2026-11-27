# Incremental storage for complete library snapshots

## What is implemented

A selected local snapshot repository shares immutable full-file content objects between complete backups. Every independently hash-addressed snapshot lists all of its files and directories, its complete-backup manifest digest, exact file sizes, and all SHA-256 content identities. There is no dependency on an earlier snapshot manifest and no automatic deletion or garbage collection.

Capture uses the existing complete library backup engine and its typed validators. Restore reconstructs a private complete backup, validates it with that same engine, then invokes its fresh-directory restore. Existing handling of originals, exact quotations, reading state, pending import review, assistant state, audio projects and other registered typed producers remains authoritative. New typed producers join without a second incremental schema for their contents.

Files already in the object store are fully rehashed before reuse. Corrupt or linked existing objects fail closed and are never overwritten as a silent repair. A missing object can be created from a newly verified source capture; an older snapshot with a missing object cannot be verified or restored until its exact bytes are again available. Cancelled capture may leave unreferenced immutable objects, but never a completed snapshot manifest. No automatic cleanup removes such objects.

The final snapshot manifest is published with create-only atomic semantics. A late stop request does not relabel a committed snapshot or restored library as cancelled. Private staging cleanup failures after publication are returned as explicit warnings with the completed receipt.

## Desktop

Library → Incremental library snapshots opens native named controls for the repository, separately retained checksum and new restore destination. Four actions save, list metadata, verify all content or restore a new library. Existing background execution, installation generation lease, cancellation, progress and stale-library callback protection remain in use. Closing requests cancellation of unfinished work.

A snapshot history entry validates metadata identity only. Its checksum should be compared with the independently retained checksum before relying on a verification or restore. The Create result displays that checksum for separate retention; selecting history does not silently approve a restore.

## Commands

~~~text
python -m carr_library snapshots create --data-dir DATA --repository NEW_OR_EXISTING_REPOSITORY
python -m carr_library snapshots list --repository REPOSITORY
python -m carr_library snapshots verify --repository REPOSITORY --snapshot-sha256 RETAINED_SHA256
python -m carr_library snapshots restore --repository REPOSITORY --snapshot-sha256 RETAINED_SHA256 --destination NEW_FOLDER
~~~

Commands emit JSON. The list command explicitly reports object_verification as NOT_RUN. Creation reports new stored objects and bytes alongside reused file references and logical bytes. Restore returns a typed completed receipt and any postpublication private cleanup warning. A missing source directory never creates an empty library implicitly.

## Disk space and bounds

Storage deduplication is by entire file content. A changed SQLite database is a new object; there is no page-level delta. Capture still creates and verifies a complete temporary backup, and restore reconstructs a complete temporary backup before the existing restore engine copies it. This reduces retained storage, not the complete-capture cost or temporary disk-space requirement.

The snapshot inventory accepts at most 200,000 files and 200,000 directories, a 64 MiB manifest, at most 10,000 completed snapshots per repository, and a default 256 GiB total logical byte budget. Explicit CLI file/byte budgets may be lowered; the total byte ceiling is 1 PiB. The existing backup engine's own smaller database, original, sidecar and typed-producer limits continue to apply. Snapshot storage budgets are enforced before publishing a completed snapshot; capture itself remains bounded by the existing complete-backup engine.

I/O is streamed in blocks no larger than 1 MiB through the existing stable native file reader. Portable path checks retain the historical 4,096-character/2,048-component compatibility bound, reject traversal, Windows aliases, case/Unicode collisions, links and conflicting parents, and preserve empty directories.

Only trusted stable local directories are supported. The existing cooperative writer lock does not protect against a hostile process replacing filesystem entries between operations. Snapshots are local and unencrypted; the existing encrypted portable synchronization feature remains separate. External model packs and credentials retain their existing recovery procedure. Interrupted typed jobs remain subject to their existing explicit reapproval/resume checks.

## Qualification

At this source checkpoint, compilation and execution are **NOT_RUN**. The new focused Windows lane requires exactly 60 source and 60 isolated installed cases, the unchanged 56-pin integrated runtime, installed module/source byte identities, and 14 real native controls/background round-trip checks. Tests include corrupt objects, full-byte changes with preserved timestamps, hard links, resealed malicious paths, fully resealed invalid typed state, no-overwrite, cancellation before and after commit, reuse, independent snapshots, empty originals and cleanup warning semantics.

The native probe uses real Windows wx controls, a real Tab transition and the actual DesktopFrame worker. Synthetic temporary folder fields are controlled. Native file-picker interaction, manual keyboard operation, NVDA, production-scale incremental storage and power-loss durability are not claimed. Actual execution results must be retained separately with the exact commit, tree, logs and artifact identities before promotion.

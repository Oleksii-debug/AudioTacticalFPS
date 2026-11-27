# Incremental library backups

Incremental backups reuse the complete library snapshot, copied-database checks, typed-state validators and fresh-folder restore. They store changed files alongside a complete snapshot inventory. Original sources, reader drafts and tabs, Studio input bindings, pending imports and restored-authority rules retain their existing full-backup contracts.

## Explicit identity and dependency

Create a normal full backup first and keep its manifest SHA256 separately. To create a layer, select that full backup or an earlier incremental layer and supply its retained checksum. Each layer records the SHA256 of its immediate parent. Keep every referenced layer and the full base.

A layer is not standalone. Restore and verification require the exact independently selected base chain. The application never searches nearby folders or chooses an apparent replacement automatically. Up to eight incremental layers may follow one full backup; then create a new full backup.

Verification admits the existing full formats 1–5, including the explicitly supported foreign root-relative envelopes, and the checked SQLite schemas 3–6. It validates a private materialized snapshot using the same complete-backup verifier before restore publishes a new library. Opening an older restored database uses the existing checked schema migration.

## Commands

Create a layer over a full backup:

    carr-library backup-incremental create --data-dir LIBRARY --output NEW_LAYER --base FULL_BACKUP RETAINED_SHA256

For a layer based on another layer, pass its parent records as repeated --ancestor FOLDER SHA256 options. Verify or restore using repeated --base FOLDER SHA256 declarations:

    carr-library backup-incremental verify --source LAYER --sha256 RETAINED_LAYER_SHA256 --base FULL_BACKUP RETAINED_FULL_SHA256
    carr-library backup-incremental restore --source LAYER --sha256 RETAINED_LAYER_SHA256 --base FULL_BACKUP RETAINED_FULL_SHA256 --output NEW_LIBRARY

The Library menu exposes Create incremental backup, Verify incremental backup chain and Restore incremental backup to a new folder. Folder/checksum choices stay on the UI thread. Every selected-header read, complete verification and publication uses the existing desktop worker; cancellation and changed-library checks occur again after each picker.

## Bounds and publication

The default logical snapshot allowance is 64 GiB, adjustable with --max-bytes. Existing component, file, source, typed-record and generated-audio bounds still apply. Source and parent paths must remain separate from the new destination. The service rejects links, aliases, unexpected members, missing parents, malformed manifests and mismatched bytes.

Changed payloads are streamed through the existing checked copier. The new layer's manifest is published last. Cancellation before that marker leaves an explicitly incomplete folder with no valid manifest. A completed manifest produces a committed receipt; cancellation arriving afterward does not authorize replay or relabel the completed copy as absent. No existing destination is overwritten.

Verification and restore can require temporary space for complete materialized snapshots, even when the stored delta is small. Space checks and existing finite producer limits apply; the result is a storage-saving layer rather than a streaming database log.

## Recovery authority

Restoration preserves historical receipts and source hashes. It does not reactivate speech engines, remote approvals, old output paths or external input references. Typed retained Studio input relocation and unresolved-input notices use the existing full restore boundary. A pending import resumes only from its validated retained source after execution receipts are revoked for the new data root.

## Reuse and evidence

The layer and CLI reuse PR152 at dc477add6db2fc0348637dd460d03b76d8e1c75f. Its full-backup adapter and UI were reconciled with the current composite format5/schema6 host, existing cancellation guards and single desktop dispatcher.

The new tests exercise actual full databases, exact source files, multi-layer materialization, old-format bases, typed reader/clip/pending recovery, corruption and resealed malformed inventories, no-clobber publication, cancellation and committed late cancellation. The native probe drives the actual Windows Library menus, background dispatcher and readable completion controls with synthetic folder selections. It does not qualify manual file picking, human NVDA use, power-loss recovery or acoustic inference.

This source checkpoint has not been executed in the unavailable host. Exact composed source, installed-package and native results must be recorded by the corresponding CI gates; source review is not a passing execution result.

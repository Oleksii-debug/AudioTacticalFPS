# Explicit capacity for complete library backups

The existing complete-backup workflow now has an optional, finite allowance for a larger library. The ordinary allowance remains the default. A larger allowance must be selected explicitly for each creation, verification or restore operation; a backup manifest cannot select it.

The complete version-4 format, pending-import version-1 format and historical version-1 through version-5 readers remain unchanged. Both allowances use the same original-file, catalog, database, source, checkpoint and typed-state validators. This change does not create a second backup format or a second snapshot service.

## Choose the allowance

| Resource | Ordinary, selected by default | Explicit `large-source-v1` |
|---|---:|---:|
| SQLite library database | 512 MiB | 64 GiB |
| Import journal | 512 MiB | 32 GiB |
| Registered original bytes | 8 GiB | 128 GiB |
| Retained pending-import bytes | 8 GiB | 64 GiB |
| Complete-backup manifest | 8 MiB | 64 MiB |
| Registered original files | 10,000 | 100,000 |
| Import jobs | 10,000 | 100,000 |
| Pending files | 25,000 | 250,000 |
| Checkpoint records | 100,000 | 2,097,152 |
| Journal validation deadline | 60 seconds | 3,600 seconds |

MiB and GiB denote powers of 1024. These are safety allowances. They do not state that a workload at those limits has been measured, completed or qualified.

Per-original format and byte limits still apply. Individual journal payloads, source checks, allowed typed-state producers, audio/translation/project receipts and schema constraints keep their existing limits. The profile does not permit an unknown state file, a changed original, a forged checkpoint or an authority-bearing local origin to enter a backup. Existing caller-supplied total-byte budgets remain additional constraints.

A small backup made with the larger allowance can still be verified under the ordinary allowance when all its actual content fits. No profile or capacity permission is stored in the manifest.

## In the desktop application

Choose **Library → Back up all libraries**. The native **Complete-backup capacity** dialog starts on **Ordinary**. Select the larger allowance only when it is needed, then choose the parent folder. The program creates a fresh child directory and presents the manifest checksum in a focused, read-only result. Keep that checksum separately from the backup.

Complete backups include validated unfinished imports and their retained originals and reviews. They also retain the existing supported library state, reading positions and bookmarks. A state entry that fails its canonical validator stops the operation.

For recovery, choose **Library → Restore backup to a new folder**. Select the allowance again, select the backup, enter its independently retained manifest checksum and choose a parent for a fresh restored library. Verification happens before canonical restore. The final question asks whether to open the restored library and defaults to **No**; completing a restore does not silently switch the active library.

Cancel in the capacity chooser approves no work and opens no folder picker. The operation then uses the existing host worker, progress display and cancellation control. Changing or closing the captured LibraryApp, or replacing its root directory at the same path, invalidates queued work and suppresses stale result callbacks. Cancellation after a successful publication does not relabel that returned committed result as a failed backup.

## Existing command line

Ordinary creation and recovery keep their existing commands:

```text
python -m carr_library --data-dir C:\ResearchLibrary --backup D:\Backups\ordinary-001
python -m carr_library --data-dir C:\RestoredLibrary --restore D:\Backups\ordinary-001 --manifest-sha256 RECORDED_SHA256
```

The larger allowance is explicit:

```text
python -m carr_library --data-dir C:\ResearchLibrary --backup D:\Backups\large-001 --backup-capacity large-source-v1
python -m carr_library --data-dir C:\RestoredLibrary --restore D:\Backups\large-001 --manifest-sha256 RECORDED_SHA256 --backup-capacity large-source-v1
```

Use a destination that does not already exist. The option is refused without `--backup` or `--restore`; unsupported profile names are refused before creating a data directory. There is no automatic escalation based on a library's size.

## Python service API

The same optional keyword is accepted by all canonical boundaries:

```python
from carr_library.application import LibraryApp
from carr_library.library_backup import create_backup, verify_backup, restore_backup

profile = "large-source-v1"
app = LibraryApp(source_root)

# app.backup delegates to the same canonical create_backup service.
manifest_sha256 = app.backup(
    new_backup_directory,
    capacity_profile=profile,
    cancelled=cancelled,
)

manifest = verify_backup(
    new_backup_directory,
    expected_manifest_sha256=manifest_sha256,
    capacity_profile=profile,
    cancelled=cancelled,
)

restored_root = restore_backup(
    new_backup_directory,
    fresh_library_root,
    expected_manifest_sha256=manifest_sha256,
    capacity_profile=profile,
    cancelled=cancelled,
)
```

`capacity_profile=None` preserves the ordinary allowance. `create_backup` accepts the same keyword directly. The profile is validated before any source or destination access.

Existing incremental/Vault, synchronization, application schedule and encrypted-transport entrypoints retain their current admission. This addition does not silently enlarge those separate workflows.

## Integrity and cancellation

The SQLite online copy uses bounded pages, observes cancellation and checks the selected database-byte allowance. The private copied database still receives the full integrity audit. Verification and restore also perform that full audit.

Cancellation is checked in both SQLite work and the Python per-page hash loop. The original exception from the caller is preserved when SQLite interrupts; the operation removes its own progress handler on exit. Hashing of copied database bytes remains bounded and observes cancellation between blocks.

Pending imports retain the existing relational validation before grouping. A temporary in-memory grouping of already validated jobs lets each source visit only its own jobs, and the copy loop maintains its existing cumulative byte total. Every prior source/checkpoint validation still runs. This does not cache or trust previous validation across operations.

Publication keeps the existing create-only boundary. A competing destination or manifest is a conflict, even when it contains the same bytes. No profile authorizes overwrite, repair, source deletion, automatic pruning or a local-origin transfer. A restored portable Studio job still requires its existing origin/output review before rendering; restored automation remains governed by its existing paused/explicit-approval rules.

## Evidence and limits

The source addition includes these whole regression modules:

- `tests/storage/test_backup_capacity.py`: 28 statically enumerated cases. Actual small ordinary/large round trips; ordinary-count refusal and explicit allowance; retained pending jobs; schema/source/checkpoint tamper; grouped-job validation; no-clobber; online-copy and hash cancellation; reset of the owned SQLite handler.
- `tests/storage/test_backup_capacity_cli.py`: three actual-subprocess cases, with the child bound to the parent's imported application and bootstrap origins.
- `tests/ui/test_backup_capacity.py`: 17 cases for ordinary default, explicit choice, cancelled selection, actual backup/restore, captured-library replacement, queued cancellation, late committed results and stale callbacks.
- The existing `tests/storage/test_crash_backup.py` retains its assertions; its instrumented full-audit fixture accepts and checks the real cancellation callback.

`scripts/windows_backup_capacity_smoke.py` requires the installed application on actual Windows. It drives real capacity/checksum/result controls with the native keyboard simulator, performs canonical backup and fresh restore on the shared worker, verifies the default-No opening decision, and checks installed module identities before and after actual host cleanup. Only system folder selections are supplied as synthetic fixture paths. Its finite watchdog covers modal work and cleanup. Physical media, power-loss recovery, manual NVDA and large-corpus measurements are separate evidence.

At this checkpoint these new sources, tests and native checks are **NOT_RUN**. Static peer review is not runtime qualification. Existing historical passes do not qualify a newly composed head.

The corpus harness can explicitly request the existing complete-backup phase with:

```text
--complete-backup --backup-capacity-profile large-source-v1
```

A capacity claim requires an actual selected-head creation, verification and independent fresh-root restore of the stated corpus. A 50,000-original retained-fixture run with bounded real import samples can qualify that reported populated backup; it does not by itself qualify two million historical journal checkpoint records or sustained ordinary-import throughput. Unsupported admission, cancellation, timeout or a failed verification must remain a refused, partial or failed outcome.

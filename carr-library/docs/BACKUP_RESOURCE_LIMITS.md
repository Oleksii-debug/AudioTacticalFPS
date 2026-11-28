# Full and incremental backup resource choices

## What changes

The existing backup commands and Library menu actions can now receive one explicit
resource policy for a larger operation. Leaving **Use reviewed resource limits**
unchecked keeps the prior limits, file formats and call paths. A backup never
authorizes its own limits: verification and restore require a separate choice on
the computer performing that operation.

The current native writer and current read-only compatibility adapters are reused.
No alternate database, archive writer, credential store or task executor is added.
The full backup still publishes a new directory with its manifest as the durable
completion marker. Incremental backups still require every independently selected
ancestor and its separately retained checksum.

## Desktop

Choose the existing backup or restore action in **Library**. The native options
dialog states what is included, offers generated-audio inclusion for a new backup,
and provides named numeric controls for the selected resource limits. Generated
audio remains excluded by default. Projects, their settings, retained original
files, evidence, reading state and recoverable pending inputs retain their existing
backup behavior.

For a larger operation, select **Use reviewed resource limits for this operation**
and review the byte, record, memory, disk and time fields. The ordinary Cancel and
window-close actions withdraw that review. The operation is admitted on the
desktop's existing background queue. It captures the selected library and its
directory identity; a stale dialog or a changed library cannot silently redirect
that work.

The incremental actions use the same resource review before reading an authenticated
archive header. The existing sequential prompts ask for the required full backup
and any incremental ancestors. Selecting folders does not grant execution of saved
jobs or external programs. Restore opens no external path recorded in an archive.

## Command line

Without resource flags, existing commands retain their previous behavior:

```text
python -m carr_library --data-dir SOURCE --backup NEW_BACKUP
python -m carr_library --restore BACKUP --data-dir NEW_LIBRARY --manifest-sha256 SAVED_SHA256
python -m carr_library backup-incremental create --data-dir SOURCE --output NEW_LAYER --base BASE SAVED_SHA256
```

An explicit policy is selected by `--backup-reviewed-resources` or by any
`--backup-...` resource value. Byte controls use whole MiB. For example, a caller
can select a larger TXT allowance and a longer operation budget:

```text
python -m carr_library --data-dir SOURCE --backup NEW_BACKUP --backup-original-mib 256 --backup-database-mib 2048 --backup-memory-mib 1024 --backup-staging-mib 8192 --backup-seconds 600
```

This example is a resource choice, not a measured capacity claim. The retained TXT
must already have its exact admitted streaming profile. Existing whole-buffer
limits for PDF, images and other formats are not expanded by this TXT option.
The aggregate original, pending, journal and manifest bounds also apply.

The same flags can be supplied to `backup-incremental create`, `verify` and
`restore`. Restore a large archive with independently chosen resource flags again.
Keep the manifest checksum separately from its archive. Use
`--backup-include-generated` for the full CLI or `--include-generated` for
incremental creation when generated audio stored inside the library should be
included.

| CLI flag | Scope when an explicit policy is selected | Typed default |
| --- | --- | --- |
| `--backup-database-mib` | SQLite snapshot bytes | 512 MiB |
| `--backup-original-mib` | One already admitted TXT original | 128 MiB |
| `--backup-assets` | Retained original count | 10,000 |
| `--backup-originals-mib` | All retained original bytes | 8,192 MiB |
| `--backup-journal-mib` | Import journal bytes | 512 MiB |
| `--backup-pending-files` | Pending component file count | 25,000 |
| `--backup-pending-mib` | All pending component bytes | 8,192 MiB |
| `--backup-jobs` | Saved import job count | 10,000 |
| `--backup-checkpoints` | Saved checkpoint count | 100,000 |
| `--backup-manifest-mib` | Manifest bytes before decoding | 8 MiB |
| `--backup-seconds` | Whole explicit operation deadline | 60 seconds |
| `--backup-memory-mib` | Available memory required at preflight | 1,024 MiB |
| `--backup-staging-mib` | Selected staging allowance and initial free-space reservation | 16,384 MiB |

The independent incremental `--max-bytes` limit still bounds the complete logical
snapshot (default 64 GiB), and the existing eight-layer chain limit remains. A larger
number in one field does not override another field, a typed-format validator,
a source seal, a schema bound or an existing native producer constraint.

## Python API

`carr_library.backup_budget.BackupBudget` is an immutable typed caller policy.
The keyword is `backup_budget` on `LibraryApp.backup`, `create_backup`,
`verify_backup`, `restore_backup`, the three incremental APIs and authenticated
incremental-header inspection. Its exact optional type is checked; an archive
dictionary is not accepted as an operation policy.

The byte-valued Python fields use bytes rather than MiB. Fractional seconds are
allowed within the declared finite range. The resource dialog and CLI preserve
their whole-MiB representation and reject silently rounded values. Omitted
policies are not replaced with the typed defaults internally, preserving the
existing independent producer bounds and legacy optional-keyword behavior.

## Cancellation, memory and staging

The first explicit operation creates one monotonic deadline. Nested full and
incremental producers retain that deadline. A reduced child staging allowance
still calls the original cancellation/deadline callback, so recursive verification
cannot restart the work clock. User cancellation takes priority over expiration.

Selected SQLite copies admit their page count before creating a destination, copy
in bounded page groups, preserve the original cancellation or timeout reason,
validate the complete copied schema and FTS invariants, and recheck the final byte
bound before publication. Database hashing and selected integrity checks remain
cancellable. Large originals continue through the existing streaming source
verifier; source hashes, exact sealed profiles and checkpoint identities remain
mandatory.

Before a full producer writes its next component, its allowance excludes bytes
already staged and reserves a second verification copy plus both bounded manifest
encodings. Sidecar/state/pending copying receives only the remaining allowance.
The final authenticated manifest rechecks the totals. Metadata accounting includes
both encoded bytes and known record counts.

Incremental creation releases materialized ancestors before creating the current
full snapshot. While a delta or a materialized ancestor remains owned, its bytes
are subtracted from the child operation's staging allowance. This prevents separate
nested checks from each borrowing the same selected space.

RAM and free disk checks are current host observations and conservative admission
checks. They are not operating-system quotas and cannot reserve resources against
unrelated applications. Filesystem errors remain errors; no overflow path, truncated
success, silent overwrite or weaker publication route is introduced.

## Saved imports and restore

The pending component preserves exact TXT source profiles and typed paged-scan
bindings alongside existing original files, import journals, accepted checkpoints
and publication identities. It verifies page/request/profile relations before
using retained originals. The selected backup allowance remains separate from the
profile that originally admitted a source.

Restored folder-scan roots are inert review data. Restored work requires an explicit
review before continuation. Translation approvals, saved output destinations and
external engine/credential dependencies retain their existing revocation/review
behavior. A larger backup policy does not authorize provider calls or external
execution.

## Acceptance status

The source changes are reviewed and accompanied by actual full/incremental restore,
SQLite, CLI and native-peer lifetime regressions. They require execution on the final
composed source and installed package. Native Windows acceptance and corpus
measurements are separate receipts. The requested 1k/10k/50k-book, multi-GB and
million-segment targets must be reported with their actual inputs, environment,
latency, memory, disk and exactness results; choosing large numbers in a policy
does not establish those results.

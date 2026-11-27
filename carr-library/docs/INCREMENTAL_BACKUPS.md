# Incremental backup storage

Open **Library → Incremental backup repository**. This command is also registered
in **Find a command** and can receive a personal shortcut. It uses the existing
complete-library backup and restore validators and the shared local snapshot
transport. It does not open a second database or create another state format.

This feature reduces retained storage by reusing identical SHA-256 chunks. Every
snapshot contains a complete file inventory and the unchanged canonical format-4
manifest. It still reads and verifies the complete source backup and, when
selected, its predecessor. It is not an incremental source scanner, scheduler,
encrypted mode, cloud synchronization service or automatic pruning policy.

## A first snapshot

1. Save pending edits in the application. Choose **Initialize a new local
   repository**, then a parent folder outside the library and a new folder name.
   Existing directories are never adopted implicitly by this command.
2. Choose **Create snapshot of all libraries and retained inputs**, select that
   existing repository and leave the optional parent token empty for its first
   generation.
3. Keep the displayed **snapshot SHA256** separately from the repository. Also
   retain the canonical manifest SHA256 if you plan to export an ordinary full
   backup. An identity copied later from an untrusted listing does not establish
   independent trust.

The native Create action includes current and archived libraries and all retained
inputs supported by the canonical producer, including eligible unfinished import
state. Generated audio is excluded by default. The command line provides the
existing explicit `--include-generated` option. Credentials, external runtimes
and old Studio output authority do not become portable because a snapshot exists.

For the next generation, supply the independently saved token of the repository's
current snapshot. The existing transport compares that reviewed parent against
its current HEAD. A different generation produces a conflict; it never silently
merges histories or chooses the latest snapshot for you.

## Verification and recovery

**List** shows the committed HEAD ancestry and explicitly says that payloads have
not been verified. Interrupted orphan manifests are not promoted by this list.
The underlying transport retains its legacy informational orphan enumeration for
reviewed recovery. If older parent metadata is missing, the list reports the
truncated history; an independently retained child token still recovers that
child's complete inventory without replaying a parent chain.

**Verify** authenticates every referenced chunk and complete file, then runs the
canonical full-backup database, catalog, retained-input and typed-state checks.
Hashes alone do not replace those domain validations.

**Export a snapshot as an ordinary full backup** publishes a new complete backup
directory through the shared exclusive rename. It preserves the byte-exact
canonical manifest and its SHA256. An existing destination, including an empty
directory that appears during publication, is never overwritten.

**Restore** invokes the canonical full-backup restoration service on a verified
private reconstruction. Choose a new folder. The current library stays open;
open the restored folder explicitly when ready. Restoration does not resume
imports, activate an engine, contact a translation provider, or grant a restored
Studio job authority to reuse an old output directory. Those existing guards
remain authoritative, including restoration to a previous filesystem path.

A child is independently recoverable, but publishing a successor requires the
selected parent and all its referenced payloads to verify first. Corrupt reused
chunks are never overwritten or repaired automatically. Previously stored data,
including interrupted private files, are not pruned by this feature.

## Interruption and disk durability

Payloads and manifests are flushed before the transport commits the current HEAD.
New immutable files use the shared native exclusive-rename primitive; there is no
link/unlink interval that could strand a second link after process death.

Cancellation before HEAD publication leaves no accepted new generation. A
completed HEAD replacement remains a published snapshot even if cancellation
arrives immediately afterward. If the subsequent directory synchronization fails,
the returned receipt includes its snapshot identity and
`durability_confirmed: false`. Keep that identity and verify it before relying
on the copy; the operation does not pretend that nothing was published.

Failure to remove this operation's private verification copy after a completed
snapshot or restore is reported as `staging_cleanup_pending`, with the retained
temporary location. The completed snapshot or restored library is not rolled back.
Preexisting snapshots, shared chunks and unrelated files are never removed.

Temporary space is required for a canonical backup and private verification
copies. Selected-parent verification is completed before the new source backup
is created. Runtime and disk-speed claims require measurement on the actual
library; no constant-time or whole-corpus scale result is implied.

## Command line

Use the Python already supplied with the application environment. Each command
has `--help`; arguments below are examples with explicit absolute local paths.

```text
python -B -m carr_library backup-repository init --repository /absolute/new-repository

python -B -m carr_library backup-repository create --repository /absolute/repository --data-dir /absolute/library

python -B -m carr_library backup-repository create --repository /absolute/repository --data-dir /absolute/library --parent-sha256 SAVED_CURRENT_TOKEN

python -B -m carr_library backup-repository list --repository /absolute/repository

python -B -m carr_library backup-repository verify --repository /absolute/repository --snapshot-sha256 SAVED_TOKEN

python -B -m carr_library backup-repository materialize --repository /absolute/repository --snapshot-sha256 SAVED_TOKEN --output /absolute/new-full-backup

python -B -m carr_library backup-repository restore --repository /absolute/repository --snapshot-sha256 SAVED_TOKEN --output /absolute/new-library
```

Create also accepts the canonical `--max-bytes` allowance and explicit
`--include-generated`. Missing repositories and source libraries are not
initialized by Create. Successful JSON output is explicitly UTF-8, including
Unicode paths under a non-UTF-8 Windows console locale.

The unencrypted path stores the canonical directory directly, allowing identical
plaintext chunks to be reused. The optional encrypted portable-transfer service
has a different envelope and remains its own explicitly selected workflow over
the same transport. Fresh ciphertext does not imply cross-snapshot plaintext
deduplication; this feature makes no such claim.

## Qualification boundaries

The focused tests exercise actual canonical backup/restore, source and installed
CLI subprocesses, reused chunks, modified and removed state, independently
recoverable generations, corrupt or missing payloads, hostile path aliases and
links, concurrent publication conflicts and actual child-process death.

The Windows probe uses the installed wheel, compares module bytes to the reviewed
source, dispatches the actual native command and drives action/hash/name dialogs
and the shared worker. System folder selections are supplied as synthetic fixture
paths; it does not automate the operating system's folder picker. Automated native
events are separate from manual NVDA acceptance, physical removable-media
qualification and power-loss testing. An exact candidate must pass its fresh
required gates before any execution or distribution verdict is assigned.

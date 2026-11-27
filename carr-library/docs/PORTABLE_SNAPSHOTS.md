# Portable library snapshots

Open **Library → Portable snapshots and transfer**. The same command is available
in the shared command palette and can receive a personal shortcut in Keyboard
shortcuts. It creates an immutable, checked snapshot of all libraries in the
current data folder, including archived libraries and every item supported by the
canonical complete-library backup format. Save open changes before publishing.

Choose a separate local folder, such as removable storage. Retain the displayed
64-character transfer code independently from that folder. A listed snapshot ID
alone does not establish trust. Inspect the snapshot with its independently kept
code before restoring it into a **new, nonexistent data directory**. The current
library remains open; use Open existing library folder to open the recovered copy.

Password encryption requires an installation profile that includes the optional
`sync` dependency group, pinned to PyCA cryptography 50.0.2. The core-only profile
remains usable without this dependency; requesting encryption there reports the
missing capability. The exact delivered profile and tested dependency inventory
are recorded by its acceptance report. Passphrases are entered for each operation and are never
saved in the library, snapshot history, command line, environment, or logs.
Choose at least 12 characters. Lost passphrases cannot be recovered. Encryption
failure never falls back to an unencrypted file. Plain snapshots require an
explicit choice. Snapshot size remains visible to an observer.

The checked envelope uses AES-256-GCM, scrypt and independently derived keys for
bounded chunks. Every chunk position, the header, completion record and total
length are authenticated. The transport rejects unsafe paths, links, unexpected
archive members, altered checksums and truncated or appended content. It streams
large backup files without loading the archive into memory.

Publishing verifies both the reviewed portable history and the local history.
If two devices diverge, it stops with a conflict. Inspect and restore the other
device's snapshot into a new folder, or publish to a separate empty folder. It
does not merge databases or implement a live cloud service. Never place an open
SQLite library in a shared synchronization folder.

Interrupted publication can reuse verified chunks of the same prepared payload.
A newly encrypted checkpoint uses a fresh random salt and produces different
ciphertext; cross-checkpoint plaintext deduplication is not promised. The separate
incremental-backup command uses the same Vault transport with canonical unencrypted
backup files. Neither command creates another snapshot storage format.

Listing a missing or empty folder does not initialize it. Explicit publication
creates the chosen vault after the user confirms the scope. Cancellation before
publication creates no accepted snapshot. If publication or restore succeeded but
the final history, durability or staging-cleanup step failed, the application
reports that outcome with the recovery code or destination. A visible output with
unconfirmed durability is not shown as successful completion in shared progress.
Inspect it before retrying. No user callback runs after the external publication
commit. Native no-replace rename preserves single-link immutable files even if
the process dies immediately after that commit.

## Command line

Use `research-library sync --data-dir /absolute/library list --folder
/absolute/snapshots` to list untrusted history. `publish` additionally requires
`--empty` or a reviewed `--parent`, plus either `--plain`, `--password-prompt`, or
`--password-stdin`. `inspect` and `restore` require `--token`; encrypted snapshots
also require the passphrase. `restore` requires a new `--destination`.

The hidden prompt is available only in a terminal. Standard input is an explicit
secret channel; no passphrase argument or environment variable is accepted.
Use `research-library sync --help` and subcommand help for exact syntax.

## Verification boundaries

The source and installed-package tests exercise real encrypted publication,
inspection and restoration, exact quotation preservation, archived libraries,
wrong secrets, corrupted archives, cancellation and divergent histories. The
Windows native probe uses real wx controls and synthetic local libraries.
Neither headless protocols nor automated native events establish NVDA or physical
device acceptance; those require the corresponding manual session.

This integration reuses the existing PR #99 transfer implementation at
`0fe4a4a3f20ffc6e95bc7b53754969c969f0ca30`, including its earlier PR #116 work,
while retaining canonical complete-library backup, shared keyboard controls,
ProgressHub and the shared exclusive-publication primitive. The envelope format
uses standard cryptographic primitives; this integration is not an independent
cryptographic audit.

The portable history is device-local authority to continue a selected vault. Its
external path bindings and secrets are not added to the canonical backup. Explicit
portable restoration creates new local history only for the separately selected
vault and retained transfer code. Restored media jobs retain canonical origin and
output reapproval rules. Unsupported external files remain outside the backup.

Source additions in this integration require fresh source, installed-wheel and
native acceptance. A prior branch's successful run does not qualify these composed
bytes; use the run identity in the release report.

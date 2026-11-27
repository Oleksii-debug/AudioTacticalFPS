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

Password encryption uses the optional `sync` dependency group, pinned to
PyCA cryptography 50.0.2. Passphrases are entered for each operation and are never
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

Interrupted publication can reuse verified prepared chunks. Cancellation before
publication creates no accepted snapshot. If publication or restore succeeded but
the final local history/durability step failed, the application reports that
partial outcome with the recovery code or destination. Inspect it before retrying.

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

This integration reuses the reviewed transfer implementation from PR #116 while
retaining the canonical backup and shared keyboard implementations. It does not
replace them with parallel copies.

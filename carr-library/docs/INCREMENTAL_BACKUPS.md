# Incremental complete-library backups

Open **Library → Incremental backup repository**. The same stable command
(`library.incremental_backup`) is available through the existing keyboard
registry and command palette.

Initialize a new local folder outside the current library. Repositories contain
readable backup data; the separate Portable snapshots workflow supports
password-encrypted copies. Creating a snapshot includes every library and the
same validated original, pending-input and typed-state inventory as the current
complete-library backup. Credentials, arbitrary neighboring files and unselected
external paths are not collected. Snapshot creation does not start import,
transcription, speech or recording work.

Each saved snapshot is a complete inventory over immutable chunks in the existing
local Vault transport. Unchanged chunks are read and rehashed before reuse.
A child snapshot restores independently of its predecessor; it does not replay
a sequence of database changes. A predecessor token is still required before
publishing a successor to an existing repository, so divergent histories fail
without changing the saved head. Keep each displayed snapshot SHA256 and full
backup manifest SHA256 independently from the repository.

Choose **Verify** to authenticate all payloads and run the existing complete
backup validator. **Export a snapshot as an ordinary full backup** materializes
the same format accepted by ordinary Restore. **Restore** always requires a new
destination, preserves existing folders and leaves the current library open.
Listing describes committed history; listing alone does not verify payloads or
establish independent trust.

## Interruption and recovery

Cancellation before publication leaves the previous snapshot unchanged. Verified
chunks from an interrupted transfer remain reusable. Publication uses the existing
exclusive native rename rather than hard-link staging. A process dying between
a complete descriptor and HEAD publication can leave an orphan descriptor; it is
not listed as a completed generation. Explicit independent-token recovery may
still validate that complete snapshot.

After publication, a storage flush, private-copy cleanup or resource-reservation
cleanup problem preserves the returned identity. Unconfirmed storage durability
and outstanding resource cleanup produce an attention state, not verified 100%
completion. Inspect the saved snapshot before relying on an uncertain flush.
Unknown repository files are not garbage-collected by any of these commands.

The shared application resource coordinator admits snapshot creation. Existing
complete-backup limits and the Vault inventory/byte bounds remain active. The
optional CLI `--max-bytes` further limits total transported bytes; it does not
raise the producer's individual file, pending-source or typed-state bounds.
This implementation saves a full temporary validated copy before chunk reuse.
It reduces retained repository storage; it does not claim zero-copy snapshot
creation or an established throughput target.

## CLI

Use `python -m carr_library backup-repository --help`. Commands are `init`,
`create`, `list`, `verify`, `materialize` and `restore`. Every command requires
an explicit `--repository`. Creation additionally requires an existing
`--data-dir`; later generations require the separately retained
`--parent-sha256`. Verification/export/restore require
`--snapshot-sha256`; export/restore also require a new `--output`.

## Source and acceptance boundary

This adapter reuses the inspected PR173 source at
`ec5133b4f102b712b5bbdb9b33527e6b080a4d6b`, adapted to this branch's current
complete-backup formats and stronger typed-state contracts. It does not import
the donor's alternate Studio, transcription or provider producers.

Strict source and installed suites cover real SQLite originals, immutable reuse,
three independent revisions, changed/deleted preferences, pending-source
recovery after deletion, passive current recording and translation recovery,
corruption, hostile paths, conflicting output, cancellation, real process death,
post-publication flush/cleanup and current CLI dispatch. The native Windows probe
drives the actual command, action/name/checksum dialogs, shared worker and focused
readable results. Physical removable media, manual NVDA and actual power loss
remain distinct acceptance.

The executor was unavailable while this composition was prepared. Newly composed
source, installed and native checks are **NOT_RUN** until their exact CI identity
reports execution. Source review alone does not qualify the current binary.

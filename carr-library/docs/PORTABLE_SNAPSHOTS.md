# Portable library snapshots

This workflow transfers a checked copy of a library data folder between local
devices. It is optional and uses no server, account, subscription or network API.
The live SQLite database stays on its local device. A portable folder contains
immutable snapshots and verified content chunks, never a live database shared
between two running applications.

## Desktop workflow

1. Save any open edits. Open **Library → Portable snapshots and transfer…**.
   The window stays modeless; the current notebook remains usable.
2. Choose a separate local snapshot folder, such as a removable drive. Select
   **Refresh history**. History is informational until verified.
3. Enter a checkpoint label. Choose optional password encryption. Publishing
   confirms that the snapshot includes all saved libraries in this data folder,
   including archived libraries, subject to the backup format's stated scope.
4. Publish. Keep the complete 64-character transfer code separately from the
   snapshot folder. The program displays the code for copying; it does not put
   private information on the clipboard automatically.
5. On the destination device, open this window in its local library. Choose the
   portable folder and enter the independently retained transfer code. Select
   **Verify snapshot**. A listed ID is never filled into the trust field for you.
6. Review the verified library names and scope warnings. Choose a new folder
   that does not already exist, then **Restore to new folder**. Encrypted data
   asks for its password again. The current library stays open; explicitly open
   the restored data folder when ready.

The initial English interface uses standard named wx controls, a keyboard
navigation order and readable status. Password entry uses a native masked
dialog. Normal close cancels and waits for the active operation. An application
exit withdraws callbacks so a finished worker cannot access destroyed windows.
Automated native checks are distinct from manual NVDA acceptance.

## Checkpoint history and recovery

Publishing requires both the reviewed current snapshot and the publishing
library's locally remembered parent to agree. Merely refreshing another
device's newer history does not authorize overwriting it. When histories differ,
restore the reviewed version into a new folder, or publish the divergent library
to a separate empty snapshot folder. There is no automatic database merge.

Old snapshots remain available. A stopped publication retains verified chunks
for retry. Retrying rechecks the current backup, prepared archive, actual chunk
bytes and history; it never trusts cache metadata alone. A completed orphan
manifest from an interrupted publication can appear in history without being
the current snapshot. This does not prevent retrying the initial publication.

Restore first validates the entire snapshot, backup schema, registered originals
and citation identities in private staging. It copies to staging on the
destination filesystem and publishes the new data directory atomically without
replacing an existing directory. Cancellation before that publication leaves the
destination absent. Cancellation after success cannot undo an already published
checkpoint; the receipt reports that outcome.

The existing underlying backup verification may finish its current database or
asset-copy phase before observing cancellation. Archive, encryption, transfer and
final restore-copy phases check cancellation between bounded blocks. Keep space
for the backup, prepared archive, encrypted payload when selected, and restore
staging. A pending encrypted transfer retains ciphertext in private cache;
ordinary staging is removed after successful operations. Filesystem power loss
and abrupt process termination can leave private staging for operator cleanup.

## Encryption and trust

Optional encryption uses `cryptography==50.0.2`, through the `sync` package extra.
Missing support is reported; an encryption request never falls back to plaintext.
Use a strong passphrase of at least 12 characters and at most 1,024 UTF-8 bytes.
Its exact characters and spacing matter. Passwords are not written to settings,
logs, snapshot metadata or command arguments. They are supplied separately for
each operation. Losing the password prevents decryption; there is no recovery
backdoor. Keep a protected independent copy of the password and transfer code.

The versioned envelope uses PyCA AES-256-GCM, scrypt with fixed bounded cost and
HKDF-SHA256 key separation. Fresh salt and nonces protect each archive; separate
keys protect control records and 1-GiB groups of 1-MiB chunks. Header, ordering,
lengths, final record and exact EOF are authenticated. It is a file envelope
around a checked backup, not encryption of the live SQLite library. Python
objects and operating-system buffers cannot promise secure memory erasure.
Archive size and generic checkpoint metadata remain visible in an encrypted
portable folder; book names, research text and the private label stay inside the
encrypted payload.

The independent transfer code authenticates the snapshot bytes against the copy
you kept; a code copied from the same untrusted folder provides no independent
trust. Hashes do not establish the identity of an unknown publisher. Local
folders and their parents must remain private and stable during an operation;
this feature is not an operating-system sandbox against a hostile process with
the same account. URL, UNC/mapped remote drive, symlink, reparse point and unsafe
portable archive paths are rejected. A provider's own synchronization client is
outside this feature's consistency guarantees; transport completed snapshots
only while the portable folder is not being changed by that client.

## Exact backup scope

The service composes the application's existing checked backup format and
inherits its supported sidecars and resource limits. It does not invent a second
SQLite schema or silently include arbitrary files. Version-2 backups include all
registered libraries, original files, saved research, academic metadata and
historical citation identities. Reading state, pending imports, studio data and
other sidecars are included only when the installed backup format explicitly
supports them. The verified preview reports limitations; preserve separate
exports for unsupported data. Global application preferences and API credentials
are not copied by this transport itself.

## Command line

The desktop is the normal user entry point. Administrative commands are also
available from the installed application. `--data-dir` always identifies an
existing local library, and every write is explicit:

```powershell
research-library sync --data-dir C:\ResearchData list --folder E:\ResearchSnapshots
research-library sync --data-dir C:\ResearchData publish --folder E:\ResearchSnapshots --empty --label "First checkpoint" --password-prompt
research-library sync --data-dir C:\ResearchData inspect --folder E:\ResearchSnapshots --token TRANSFER_CODE --password-prompt
research-library sync --data-dir C:\ResearchData restore --folder E:\ResearchSnapshots --token TRANSFER_CODE --destination C:\RestoredResearch --password-prompt
```

For later publications replace `--empty` with `--parent PREVIOUS_SNAPSHOT_ID`.
Use `--plain` to deliberately publish without encryption. Controlled automation
can supply one passphrase line through `--password-stdin`; do not place the
password directly in shell history, command arguments or environment variables.
JSON output contains a receipt or verified preview. Exit 0 means success; 2 means
failure; 3 means an output exists but its final history/durability step needs
review; 130 means interrupted. Always retain a partial-outcome receipt before
retrying.

## Development and evidence

The public repository contains source, format documentation and synthetic tests
only. The Windows optional dependency lock records exact official wheel hashes.
The separate Windows snapshot workflow runs strict source tests, verifies the
wheel against the exact Git commit, runs actual plain/encrypted native desktop
events, and reruns transfer tests against the installed package outside the
checkout. It uploads reports only, not application binaries or user data.

Relevant upstream specifications:

- [PyCA authenticated encryption](https://cryptography.io/en/50.0.2/hazmat/primitives/aead/)
- [PyCA key derivation](https://cryptography.io/en/50.0.2/hazmat/primitives/key-derivation-functions/)
- [PKWARE ZIP format](https://pkware.cachefly.net/webdocs/casestudies/APPNOTE.TXT)
- [cryptography release and licenses](https://pypi.org/project/cryptography/50.0.2/)

Third-party notices remain attached to their installed distributions:
cryptography (Apache-2.0 OR BSD-3-Clause), cffi (MIT-0), pycparser (BSD-3-Clause).

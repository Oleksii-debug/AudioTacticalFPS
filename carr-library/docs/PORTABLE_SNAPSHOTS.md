# Portable library snapshots and explicit local transfer

## Scope and current qualification

This layer reuses PR99 commit `5f18481654277425ab8af170575de978e7a2a69c` and
the application's current complete-backup verifier. It transfers a saved,
immutable snapshot of every workspace in one library data folder, including
archived workspaces. It never opens a live SQLite database on a shared drive.
There is no automatic merge, network account, watcher, remote provider, or
background synchronization.

The composed source, tests and native probe are **NOT_RUN** until they pass on
the exact integrated candidate. A modeled test or native synthetic data transfer
does not establish screen-reader usability, large-library capacity or recovery
on a user's actual removable device.

## Existing desktop and command entry points

Install `install_portable_sync_actions(self)` from
`carr_library.ui.portable_sync` in the actual
`src/carr_library/ui/desktop_app.py`, before the existing keyboard menu capture.
It appends **Library → Portable snapshots and transfer…**. The modeless dialog
borrows the frame's existing `DesktopExecutor`; closing it does not shut down
that worker. The current notebook, library selection and unsaved edits remain
under their existing owners. The user is asked to save edits before publication.

The host command dispatcher forwards `sync` to `carr_library.sync.cli.main`.
For example:

```text
research-library sync --data-dir C:\\Research publish --folder D:\\Snapshots --empty --plain
research-library sync --data-dir C:\\Research publish --folder D:\\Snapshots --parent <reviewed-head> --password-prompt
research-library sync --data-dir C:\\Research inspect --folder D:\\Snapshots --token <independent-code> --password-prompt
research-library sync --data-dir C:\\Research restore --folder D:\\Snapshots --token <independent-code> --destination C:\\RestoredResearch --password-prompt
```

Passwords use a hidden prompt or an explicit bounded standard-input channel.
They are never CLI arguments, saved settings, transfer labels or diagnostic
messages. Python/native allocations do not promise secure memory erasure.
Selected encryption requires the reviewed optional `cryptography==50.0.2`
capability; its absence or a different version is an explicit error. There is
no unencrypted fallback. The host owns the pinned dependency lock and SBOM.

The obsolete donor `installation_lease` decorator is not copied. This CLI uses
the same installed bootstrap/main contract as the other current application
commands, without adding an updater or a new authority system.

## History, trust and commit boundaries

Listing a folder reads bounded untrusted history. It cannot initialize a missing
or empty vault and does not fill the trust-code field. Inspection and restoration
require the independent full SHA256 transfer code retained by the publishing
device. Labels and timestamps cannot authorize another snapshot.

Publication compares both the reviewed current vault head and the local
remembered head. Divergent histories require explicit restoration to a new
folder or publication to another empty folder. Completed chunks and immutable
manifests are reusable only after their exact bytes are checked again.

`portable-sync-state.json` is an optional, typed, passive top-level sidecar:
a family ID, a bounded map of vault-path hashes to heads, and printable labels.
Its envelope is limited to 1 MiB and 4096 history entries. No secret or filesystem
path is retained there. Native format 5 and supported foreign-flat backups use
the same pure validator. The current backup schema and writers are unchanged.

Restoration is always to a new folder. It checks the original canonical backup
again into private preparation and uses the internal
`restore_backup(..., _final_destination=...)` projection only for typed source
paths. All writes still go to that owned preparation directory. Consequently:

- retained Scene clip inputs point to the final library, with one checked
  projection revision and the original immutable revision preserved;
- pending imports retain their exact original bytes and identities while
  execution attempt/publication authority is revoked by the existing restore;
- Book plans and historical production receipts retain their exact source
  binding, and restored jobs still require a fresh output review;
- reader tabs, empty drafts, undo histories and passive preferences are retained;
- external audio references do not become permission to read external files.

Authenticated read staging is closed before the final exclusive rename. The
same resource reservation remains held through that rename and final prepared
staging cleanup. A successful commit followed by durability, lock or cleanup
failure returns a typed receipt describing the existing output. Cancellation
after commit cannot relabel that output as absent or silently replay it.

## Shared resource and lifetime contract

Actual backup, archive, encryption, inspection and restoration use the existing
`ResourceBroker`. The caller may supply that same broker facade explicitly.
There is one reservation per operation, with one CPU slot and a declared
256 MiB memory estimate for scrypt and bounded archive/SQLite work. This is
cooperative admission, not a memory sandbox or a hardware-capacity measurement.

Disk estimates are based on bounded actual metadata or the independently bound
vault manifest, not the 1 TiB protocol ceiling. Local preparation reserves
`8 × measured bytes + 64 MiB`; the separately selected vault or restore volume
also reserves its measured output estimate. Source size is checked again after
waiting and checked backup bytes are compared against the admitted estimate.
The broker still applies its existing configured capacity and actual free-space
reserve on each selected volume.

Size enumeration reads only the current complete-backup producer roots, never
external references or model/runtime directories. It rejects links, hardlinks,
unreadable directories and more than 100000 discovered entries. The authoritative
backup still performs its own typed inventory, source checks and streaming hashes.

The service uses the existing `NativeLock` on `.carr-portable.lock`.
The existing retained-context helper keeps that lock, exact staging and broker
lease until all reported native cleanup owners are quiescent. Scrypt and file
copying remain on the owned worker; cancellation never detaches them and claims
completion. The GUI acknowledges cancelled queued Futures without retaining the
queued password, and a forced native Destroy revokes callbacks using the
captured live window ID.

## Reused archive and encryption contracts

The vault and atomic no-replace publication helper are reused from PR99. The
data-only transfer archive remains stored ZIP/ZIP64 with strict complete
namespace checks, explicit EOF/central-directory bounds, no extraction fallback
and no execution. File reads are bounded to their captured sizes and unreadable
subtrees fail the operation rather than producing a partial successful snapshot.

The optional encrypted envelope retains the donor's exact AES-256-GCM,
scrypt and HKDF-Expand protocol implemented through PyCA, including its fixed
authenticated header, chunk positions, lengths, final marker and EOF. This
adaptation does not define another cipher or wire format. The publisher's
separately reviewed exact-output-byte hardening is composed on this same API.

Primary API documentation:
[PyCA AEAD](https://cryptography.io/en/50.0.2/hazmat/primitives/aead/)
and [PyCA key derivation](https://cryptography.io/en/50.0.2/hazmat/primitives/key-derivation-functions/).

## Required verification

Portable source and installed selections:

- `tests/sync/test_archive.py`
- `tests/sync/test_crypto.py`
- `tests/sync/test_vault.py`
- `tests/sync/test_service.py`
- `tests/sync/test_service_commit_boundaries.py`
- `tests/sync/test_cli.py`
- `tests/sync/test_complete_state.py`
- `tests/sync/test_current_backup_composition.py`
- `tests/sync/test_admission.py`
- `tests/sync/test_state.py`
- `tests/ui/test_portable_sync.py`

The original named donor tests remain present. The platform-specific descriptor
cleanup body is selected explicitly on Linux at
`tests/sync_posix/test_crypto_cleanup.py`; it does not return early as a Windows
pass. The host also selects its additional exact crypto publication suite.

The actual native probe is
`tests/ui/probe_portable_sync_native.py`. Run both the explicit source and
installed forms with the pinned encrypted capability:

```text
python -I -B tests/ui/probe_portable_sync_native.py --native --encrypted --source <exact-checkout> --report <source-receipt.json>
python -I -B tests/ui/probe_portable_sync_native.py --native --encrypted --expect-installed --report <installed-receipt.json>
```

The probe uses the real desktop worker, wx controls, process/owner-checked native
confirmation buttons, real encrypted snapshots and actual separate-folder
restoration. It retains every original acceptance check and adds borrowed-worker
and direct-Destroy completion checks. Imported production bytes must match the
exact checkout and, for installed execution, their owning wheel RECORD. It saves
an initial failure receipt before native work; missing prerequisites or a watchdog
failure are not successful qualification. It deliberately makes no NVDA claim.

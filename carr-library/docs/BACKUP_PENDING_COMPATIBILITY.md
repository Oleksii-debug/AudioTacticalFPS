# Pending import backup compatibility — candidate

This change is isolated from PR136 and does not merge PR125 into carr-library.

## Source and scope

The selected host is commit 203ec0a6778f3c3d03f2599b481a0c18c9b2028a.
The pending import snapshot, validation and restoration implementation is reused
from canonical commit 33dcc42edd6781b9aeb02dcaaf016b386bcf37b6:

- pending_backup.snapshot_pending
- pending_backup.validate_pending
- pending_backup.restore_pending
- durable_state.local_path, regular and sync_directory

The host integration adds caller budget propagation and the host's registered
document suffixes. It retains the canonical journal schema, immutable checkpoint
checks, exact source bindings, retained-source checks and location-only rebasing.

## Explicit formats

| Format | Discriminator beyond the common database/asset fields | Behavior |
|---|---|---|
| Host v4 | sidecars + generated_audio_included | Existing typed sidecar contract |
| Historical managed-audio v4 | sidecars, with its exact historical path descriptors | Existing compatibility branch |
| Canonical v4 | state + generated_audio_included + pending | Typed supported state plus canonical pending import verification |
| New v5 | layout=sidecars-pending-v1 + sidecars + generated_audio_included + pending | Typed host sidecars and pending import preservation |

The full exact field set and named v5 layout are required. The number alone
never authorizes a layout. No v5 backup definition was found in the 18 fetched
branch heads inspected during this change; their occurrences of SQLite
user_version=5 are incompatible-database rejection fixtures.

A host with no pending journal/request/batch state still writes the unchanged
host v4 shape. Existing v1/v2 and explicit v3 compatibility readers remain.

## Data flow and publication

The enclosing writer guard protects the catalog snapshot and pending producer.
The producer snapshots the SQLite import journal, including committed WAL data,
and captures the referenced request/batch/review files and original bytes.
Unreferenced orphan originals are excluded. Missing or changed planned source
bytes prevent a completed backup.

Both verification and restoration authenticate pending files, journal schema,
checkpoint identities, source hashes and request bindings in private staging.
Only data-directory/source location fields and duplicate source links are
rebased to the final new root. Job, edition, work, revision and review identities
are preserved. Verification may stage pending originals and journals to run
these checks; it does not rewrite the backup.

Restoration first stages catalog originals. The reused pending restorer verifies
an already-staged matching original and returns only newly added assets for
publication. Therefore an original referenced by both a published edition and
a pending import is not linked twice.

Backup publication retires staging hard links before the completion manifest.
Restore publication keeps .restore-incomplete until all catalog, sidecar,
pending request and original files have been published and staging is removed.
A prepublication failure leaves no destination; a publication failure leaves
the destination explicitly incomplete. An existing destination is never merged
or overwritten.

## Deliberate compatibility limit

Canonical v4 state compatibility remains partial. Unknown studio/provider
state requires its owning typed validator and is rejected explicitly. This
change does not copy unvalidated state, add the alternative studio runtime,
retarget saved source identities or resolve the other PR125 merge conflicts.

## Qualification

Before the executor disconnected, 28 reused pending recovery tests and 28 new
interoperability tests passed on Linux. The broader local backup selection had
one reported failure before the connection was lost; its final result was not
retrieved. A stale assertion that omitted the new pending directory was found
and corrected in the reconstructed candidate, and an explicit overlapping
catalog/pending-original regression was added afterward.

The separate Linux/Windows workflow runs the complete selected backup suites
through scripts/ci_check.py, which rejects skips, XFAIL and XPASS. Its actual
results, rather than the interrupted local run, qualify the published candidate.
No Windows pass or complete canonical migration is claimed here.

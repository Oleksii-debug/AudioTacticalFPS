# Pending import backup compatibility

This component addresses backup data compatibility. It does not resolve the
other PR125 runtime/packaging conflicts or merge that alternative into carr-library.

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

Once the completion manifest and its directories are durable, private staging
cleanup failures do not turn a completed backup into an API failure. The API
continues returning the digest string. An optional warning callback receives
the actionable cleanup path; guarded logging is the fallback. Callback/logging
failures cannot undo completion. The desktop shows the notice in its completion
status and dialog, and the backup CLI prints it on stderr alongside the normal
digest on stdout. Prepublication errors retain their original exception and
any additional cleanup note. Restore still retains its incomplete marker if
cleanup does not finish.

## Deliberate compatibility limit

Canonical v4 state compatibility remains partial. Unknown studio/provider
state requires its owning typed validator and is rejected explicitly. This
change does not copy unvalidated state, add the alternative studio runtime,
retarget saved source identities or resolve the other PR125 merge conflicts.

## Qualification

The reconstructed a139f08f57169ee3ee33e20a4581b85c3d0c458c candidate was fetched
into a fresh checkout and its four core blobs matched the original local files.
The entire selected backup suite then passed: 288 cases, zero failures/skips.

Independent review reproduced a postpublication cleanup exception on that
candidate despite a valid archive. Commit dee60bc2897bd7cd7444bb46b5d8a83230d66d76
repairs this boundary and adds failure-injection, CLI and desktop completion
tests. The reviewer repeated the probe successfully: exact digest returned,
actionable notice delivered, archive verified/restored, same pending edition
resumed. Payload hardlink retirement still precedes manifest publication.

With the reviewed desktop menu fixture, exact commit
6352d98f8c70fe407d69d3432f047cea7f4fda3c passed 359 cases in 23.17 seconds on
Linux/CPython 3.12: zero failures/skips, exit 0. The selection includes every
backup suite, source overlap, actual OCR review recovery, cleanup failures,
desktop event protocols and backup CLI/startup policy. The Python offline test
policy and rejection of XFAIL/XPASS were active. The compact runtime and artifact
digest record is in evidence/backup-pending-linux-20261127.json.

The separate Linux/Windows workflow uses the same backup selection and the
existing strict per-suite gate. Native Windows/NVDA operation and complete
canonical state migration remain separate qualifications; this Linux result
does not claim them.

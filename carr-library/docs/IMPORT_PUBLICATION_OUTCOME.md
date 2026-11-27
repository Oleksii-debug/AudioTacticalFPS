# Import results after a stopped worker

A native import can commit its source, pages, catalog memberships and index
before its child process returns a completion response. Cancellation before
publication must still leave the request stopped. Recovery must identify the
request that actually committed, rather than infer ownership from an existing
book or a new collection membership.

## Evidence required for completion

The supervisor uses the canonical schema V5 publication receipt. The worker
inserts the exact request ID, attempt ID and publication digest in the same
SQLite transaction that seals the source. The candidate in the private request
file is only a hint: a rolled-back candidate cannot complete a request, even if
another process later publishes the same deterministic revision. Existing
receipts are immutable and belong to the first publisher.

Reconciliation verifies that receipt, the unchanged initial request and
database file identity, current catalog and source metadata, active sealed
publication, retained original, extraction configuration, ordered page hashes
and active index entries. Scoped reads have a five-second cooperative budget.
Configuration and text byte counts are checked before loading them, with the
existing format-specific segment limits. No revision watermark or changed
membership substitutes for a matching transaction receipt.

Only strict UTF-8 text matched to its complete original bytes can return
`verified=True`. PDF, DOCX and EPUB completion does not approve quotations or
printed page numbers. Missing ownership evidence preserves the stopped result;
changed or damaged publication evidence returns an explicit verification failure
and asks the user to review or resume the request.

## Replays and explicit resume

A normal replay may add a requested collection membership without duplicating
the original source. Schema V5 deliberately does not replace the first
publisher's receipt or create a new receipt for every replay. If a replay commits
a membership and then its child stops before returning, the supervisor therefore
cannot certify that replay as complete. It retains the stopped outcome even
when the membership is visible.

An explicit resume validates the existing source and registration through the
ordinary importer and can return complete without creating duplicate revisions
or memberships. This conservative boundary prevents another writer's work from
being attributed to the stopped request. It is not a claim that cancellation
rolled back a membership that had already committed.

The inner importer also distinguishes publication committed in its current call
from a terminal replay. Late cancellation cannot undo the former; registration
of new replay memberships still observes its existing cancellation guard.
Both paths validate their sealed source identity before returning.

## Durable recovery

The result is saved in the existing import request. Recovered first publications
retain `publication_recovered`, the compatible `recovered_publication` marker,
and the original `worker_state`. The extraction journal is not rewritten by this
check. A child can stop after catalog publication while the journal remains
pending; complete backup and restore preserve the source and its validated
result. Request-bound attempt candidates are cleared when a backup is rebound
to its restored data directory. The existing journal recovery remains
responsible for subsequent extraction operations.

## Validation scope

The focused regressions use real child processes and actual SQLite commits for
TXT, PDF, DOCX and EPUB. They cover pre/post-commit cancellation, timeouts and
process exit, competing commits, changed request/configuration bindings,
original corruption, catalog replacement, missing index entries, conservative
interrupted replay followed by explicit resume, and backup after publication.
Windows qualification belongs to the final composed commit's CI results, not
the existence of these portable tests.

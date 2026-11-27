# Import results after a stopped worker

A native import can commit its source, pages, catalog memberships and index
before its child process returns the completion receipt. Stopping that child
must not describe an already committed publication as an unfinished import.
Cancellation before publication must still leave the request cancelled.

The supervisor reuses the publication reconciliation from commits
`28413476a2f8d7002448aad093821bb77c5877d8` and
`8336072dc9d49166e3e0ad5f155f93f7d6b7d9e4`. This canonical adaptation keeps
the existing TXT, PDF, DOCX and EPUB adapters. It adds no parser, storage schema
or extraction journal.

## Evidence required for completion

The supervisor records a revision watermark and requested collection memberships
before launching the child. It reconciles a stopped worker only if the process
actually started. A previously published book alone does not establish that a
cancelled replay completed. A replay can qualify when a requested membership
was absent before launch and is now committed.

The reconciliation reads the unchanged request and database identity, current
catalog and source metadata, sealed publication, retained original, extraction
configuration, ordered page hashes and active index entries. Scoped reads have
a five-second cooperative budget. Configuration and text byte counts are checked
before loading them, with the existing format-specific segment limits.

Only strict UTF-8 text matched to its complete original bytes can return
`verified=True`. PDF, DOCX and EPUB completion does not approve quotations or
printed page numbers. An absent, ambiguous, changed or damaged binding preserves
the worker's original failure/cancellation result.

## Durable recovery

The result is saved in the existing import request. The extraction journal is
not rewritten by this check. A child can stop after catalog publication while
the journal remains pending; complete backup and restore retain that exact state
and the original bytes. The existing journal recovery remains responsible for
subsequent extraction operations.

The inner importer also distinguishes publication committed in its current call
from a terminal replay. Late cancellation cannot undo the former; registration
of new replay memberships still observes its existing cancellation guard.

## Validation scope

Portable tests use real child processes and actual SQLite commits for all four
formats, including cancellation, timeouts, process exit, changed request/config
bindings, original corruption, catalog replacement, missing index entries and
backup after publication. The existing donor fails the committed-membership
replay regression; the adapted verifier passes it. Windows qualification belongs
to the final composed commit's CI results, not this portable run.

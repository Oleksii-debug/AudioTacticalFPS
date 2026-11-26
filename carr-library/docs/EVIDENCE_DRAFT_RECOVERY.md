# Research-note draft recovery

The native Evidence and Notes panel keeps a separate recovery copy of an edited
research note. It does not save a quotation, approve a source, or commit a note
without the existing Save note action.

The first edit is checkpointed immediately. Later edits are coalesced for at
most one second while the editor is active; a cached final edit is flushed when
the panel is destroyed. A locked or unavailable disk is reported in the native
status control. If a checkpoint cannot be written, the editor text remains
available: save or copy it before closing. This is recovery from the last
successful checkpoint, not a guarantee against every power or hardware failure.

When the evidence is reopened, **Recover research note draft** offers Recover,
Discard, or Keep for later. The application freshly authorizes the exact
library, topic, evidence and historical source before the prompt and again
after the decision. Recovery opens an unsaved editable note. A copy whose saved
note or source baseline changed can only be reviewed in a read-only native
dialog and copied manually; it is never silently attached to changed evidence.
Archived or inaccessible evidence cannot reveal its recovery copy.

Successful note saving removes only the matching recovery revision. A failed
save retains it. New edits made while a save finishes receive a checkpoint with
the newly saved baseline. Explicitly discarding a note also discards its owned
recovery copy, and closing the panel does not recreate that discarded copy.

## Storage and backup interface

The library-root sidecar is `evidence-drafts.json`, kind
`carr-evidence-drafts`, version 1. It is limited to 8 MiB and 128 scoped records;
each recovery note is limited to 1 MiB UTF-8 and 400,000 characters. These are
recovery-storage limits, separate from the committed-note limit. No draft is
silently evicted. The file contains note text and opaque identity/digest fields;
it contains no credentials or copied source quotations.

`EvidenceDrafts(root)` uses the existing cooperative library writer lock,
regular-file checks, revision comparison and atomic flushed replacement.
`validate_evidence_drafts(raw_bytes)` validates backup contents without granting
authorization to display or apply them. A backup integrator must explicitly
include this validated sidecar; copying the SQLite database alone is insufficient.

## Executed evidence

The focused tests use actual `LibraryApp` databases and recovery files. They
exercise restart, abrupt process exit after a completed checkpoint, Unicode,
empty and invalid-note drafts, stale writer rejection, preserved newer edits,
disk/lock failure, corrupt/unsafe storage, authorization changes during a native
dialog protocol, baseline conflicts, successful/failed saves and destruction
flush. The headless UI protocol tests use explicit control doubles. Native
Windows and manual NVDA acceptance require their own executed results.

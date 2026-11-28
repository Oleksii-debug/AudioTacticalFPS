# Document batches: explicit capture, planning and production

This feature composes the existing document converter, reader processing copy,
no-clobber export writer and Audio Studio. It adds a saved review and a native
Convert-menu flow; it does not create another scheduler, source library or voice
engine. The Desktop's existing executor runs one explicit operation.

## User flow

Open **Convert → Document batches — split, name and produce...**. Capture selected
files, one selected folder including bounded subfolders, or saved reading-copy
JSON files. Capture retains exact input snapshots but does not extract or speak
them. Prepare reading text with the installed local converters, then choose
boundaries, portable names and an existing output folder.

Preview reports exact text intervals, names, source/plan digests and existing-file
conflicts. Explicitly save the reviewed plan, then choose Run. Changing visible
controls invalidates approval. Existing output files are never overwritten.
Run again verifies saved output receipts rather than rewriting completed parts.
Pause and Stop retain completed parts; resumption is explicit. A restored job
requires a fresh preview and output-directory approval.

Split modes retain all source characters, including CRLF and Unicode. Options
include whole document, character limit, blank lines, selected headings,
literal/isolated bounded-regex markers and estimated duration. Estimated
duration uses an explicit whitespace-word rate; it is not measured speech time.
Optional combination inserts exactly two LF characters between documents and
records those separators. Names satisfy the existing portable-name rules.

Text output consists of UTF-8 text plus its completion provenance sidecar.
Studio output submits the same reviewed text to the existing Studio service,
with an explicitly selected installed voice/language/format. Availability is
not evidence of language quality or entitlement. No engine or model is acquired
or activated by opening this window.

## Retention and failure boundaries

State is held below the exact state/conversion/batches namespace. The current
shared library writer boundary and .document-batches.lock exclude backup
during active publication or retained native cleanup. The native process tree
and its staging/resources remain with an actual AudioCleanupPending owner
until that owner confirms quiescence. An owned Studio registry whose cleanup
fails stays retained for retry; borrowed services remain with their caller.

The converter publishes reading text followed by its provenance completion
marker. Source, staged bytes, output inodes and parent identity are checked.
Cancellation before the marker fails the operation and rolls back only owned
partial outputs. A verified pair already committed by the current invocation
keeps its saved status if only final sync/staging acknowledgement is interrupted.
The notice distinguishes saved bytes from incomplete cleanup/durability
confirmation. Equal bytes in a foreign inode do not establish that call's commit.

A process death may leave staging hard links. Generic reading/source guards
continue to reject linked files. The converter's explicit new operation does
not silently adopt or delete an abandoned foreign/unreceipted public output.
Private batch resumptions verify the exact retained receipt graph; an incomplete
or unrelated public destination requires an explicit different destination.
Source-linked reading copies imported into this workflow are derivative input;
their embedded metadata does not grant quotation approval.

Completed status is persisted only after the reviewed output graph verifies.
A late observer cannot revoke it. An interrupted final metadata/lock-release
acknowledgement yields a notice only when the exact completed record is still
present; a different or missing record remains an error. Text/provenance writer
cleanup notices are retained in the existing bounded job message.

## CLI

Use the installed application interpreter and an existing library:

    python -m carr_library documents --data-dir LIBRARY capture FILE [FILE ...]
    python -m carr_library documents --data-dir LIBRARY prepare JOB
    python -m carr_library documents --data-dir LIBRARY preview JOB --destination OUTPUT --mode blank-lines --output REVIEW.json
    python -m carr_library documents --data-dir LIBRARY approve --review REVIEW.json --review-sha256 EXACT_PRINTED_SHA256
    python -m carr_library documents --data-dir LIBRARY run JOB

The jobs and show commands read stored metadata. They do not prove that external
output bytes are currently unchanged; run performs explicit verification.
Pause and cancel commands record requests acknowledged at existing cancellation
boundaries. They do not cancel unrelated host operations.

The native entrypoint has stable command ID conversion.batch, retains the
existing Convert and Supported formats commands, and has no new default shortcut.
Saved user bindings are handled by the current shared keyboard manager.

## Qualification scope

The reconstructed composition is source-reviewable; exact new execution is
NOT_RUN until its CI gates finish. Reused tests call actual converter children,
actual FFmpeg/Studio with declared synthetic PCM, real state stores, cancellation,
backup/restore and output checks. New tests cover real child exit around the
two-file marker, held native cleanup, changed inodes, bounded descriptor reads,
late acknowledgements, borrowed/owned registry cleanup and native-deletion
closures. POSIX FIFO tests are selected only on POSIX.

The installed Windows smoke script is scripts/windows_document_batch_smoke.py
and takes no arguments or audio evidence. Run it with -I -B -X utf8 inside the
existing outbound-denial gate. It checks installed source identities before
and after, uses real wx keys and the registered menu, actual isolated conversion
and exact sidecars, then actual direct/native-parent deletion before delivering
held genuine completed work. Synthetic file/folder picker choices are disclosed.
This is not a claim of human keyboard, NVDA, native file-picker interaction,
speech quality or interrupted native-resume acceptance.

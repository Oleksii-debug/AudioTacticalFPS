# Larger TXT and PDF sources

The existing document importer accepts TXT and PDF sources up to **128 MiB**
through bounded file reads. The other formats that require a complete in-memory
document retain their **64 MiB** source limit. No archive, office-format, OCR,
page-count or parser-resource limit is silently relaxed.

This is a finite resource profile. It is not a promise to accept an arbitrary
file size or every pathological PDF at the maximum size.

## Text and page identity

A larger TXT must be strict UTF-8 and keep its original logical form-feed
boundaries. Each original segment may contain at most **4 MiB of source bytes**;
the existing limit of **1000 segments** remains. The importer preserves CRLF,
original text and the first-file UTF-8 BOM rule. It does not invent printed
pagination, discard later segments, normalize words, or substitute an edited
reading copy.

The existing **16 MiB limit applies to each serialized checkpoint**, not to the
whole TXT. Every accepted segment is checked and published. An oversized single
logical segment is explicitly refused rather than partially imported. The
larger profile does not automatically redivide an unsegmented text file.

PDF page count, encrypted-document refusal, pixel limits, model admission,
independent source review and per-page extraction budgets remain unchanged.
The same pinned pypdf and PDFium adapters consume a held seekable source stream.
PDF extraction alone never grants quotation verification.

## Retention, cancellation and recovery

The importer binds MIME, exact byte count, SHA-256 and the selected local file
identity before accepting the source. Larger sources use the existing private
original-retention transaction: copy and hash bounded chunks, sync the private
file, and publish without replacing another original. A changed source,
unexpected hardlink, replaced path, cancelled copy or checksum mismatch cannot
mint an accepted original.

The existing isolated document worker also handles larger TXT. Its durable
request, journal, checkpoints and publication receipt are unchanged. The POSIX
address-space ceiling remains 3 GiB. Windows retains process/time isolation;
that POSIX address-space limit is not claimed on Windows.

After an actual child stop, the supervisor validates the exact transaction and
retained original before reporting completion. The read-only verification
budget is **15 seconds for larger TXT/PDF**, and the previous 5-second budget
remains for the smaller profile. Failure to complete verification within the
budget remains an explicit uncertain/stopped outcome; it is not a successful
import.

Both full backups and pending-import backups stream retained originals and
validate the MIME-specific source bound. Restore rebinds only the existing
retained-source namespace. A prepared or partly checkpointed import can resume
from its exact backed-up original after the external selected file disappears.
The existing aggregate backup, database and journal budgets still apply.

Search, saved quotations and independent PDF review resolve the authorized
retained source and verify its original hash. They do not turn a saved path or
a file-backed snapshot object into authority to read an unrelated file.

## Acceptance fixtures and evidence

Run the following suites from both source and the sealed installed package:

    python -m pytest tests/catalog/test_streaming_intake.py tests/ingest/test_large_source_intake.py

The fixtures contain a real **80 MiB prose TXT**, with more than 64 MiB of
non-whitespace UTF-8 bytes, and a structurally valid **128 MiB PDF** whose large
text stream is referenced as a document attachment. No file size, sparse
allocation or parser result is mocked. The TXT test requires every original
segment digest and the exact total extracted byte count, so a partial import or
mostly empty source cannot satisfy the positive case.

The intake suite measures Python buffer allocation. The integration suite
measures the actual child peak resident/working-set memory, checks original
hashes and exact quotes, performs real PDFium review, exercises pending and
completed backup/restore, and cancels an actual child after its SQLite commit.
The current fixture ceiling is 40 MiB of Python allocations for intake and
1536 MiB of actual child peak memory. These are executable acceptance thresholds,
not pre-recorded measurement results.

New source and tests require fresh CI on the assembled revision. This change
does not claim arbitrary-document performance, OCR/model quality, or native
screen-reader qualification from synthetic resource fixtures.

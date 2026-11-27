# Explicit large original TXT and PDF profiles

This adds a named, finite source profile to the current original store, import
request, isolated process owner, journal, catalog publisher and complete backup.
It does not change the default 64 MiB intake or the existing 128 MiB streamed
TXT identity. Existing requests and revision identifiers remain readable.

## Admission and limits

Choose **Import Center → Import selected large original**, select TXT or PDF,
an original allowance in MiB, and an owned-worker deadline. The selected file
must match that format. The choices are frozen before dispatch; the request can
be resumed without choosing a different source or silently changing its profile.

| Contract | Ordinary / prior explicit profile | New explicit profile |
|---|---:|---:|
| Generic original intake | 64 MiB | Unchanged |
| Existing streamed TXT | 128 MiB; 8,192 fragments | Unchanged identity |
| Large TXT original | No admission | At most 4 GiB and 65,536 logical fragments |
| TXT fragment | 65,536 Unicode characters | Same fragment size |
| File-backed PDF original | No admission above ordinary limit | At most 4 GiB |
| Physical PDF pages | 1,000 | Same 1,000-page ceiling |
| PDF checkpoint JSON | Existing per-record bound | 16 MiB per page; 256 MiB total |
| Large import worker | Ordinary request at most 600 seconds | Explicit request at most 7,200 seconds |
| Reader selection | At most 10,000 selected positions | Same selected count; absolute position up to 65,536 |

Both byte and record limits apply. These are admission ceilings, not a measured
throughput, memory-consumption, file-complexity or OCR-quality claim.

PDF uses held, independently seekable file readers with the installed pypdf and
PDFium APIs. A parser cannot request a single read larger than 16 MiB or an
unbounded read-all. The same page extraction, visibility checks, renderer,
pinned OCR selection, review records and rendered-page hashes remain in use.
No separate OCR implementation, custom PDF parser or alternate process owner
is introduced. Encrypted or over-budget files fail explicitly.

The external worker remains owned until its native family is quiescent.
Streaming cancellation preserves acknowledged checkpoints. A retry validates
the whole original, the frozen profile, the runtime and retained checkpoints
before publication. Parser memory still falls under the current worker and
resource policies; this feature does not add an OS sandbox or claim measured
peak RAM.

Successful PDF extraction is not independent text approval. The existing
explicit original-page review is still required for a verified quotation.

## Versioned data and complete backup

Large source requests use request version 3 and the exact
carr-large-source-profile/1 payload. Existing request versions 1/2 retain their
old profiles. TXT extraction uses strict-utf8-stream-v2; PDF uses
page_ocr-file-v1 and keeps the current OCR configuration and runtime identity.

For any new large-profile request, explicitly select the complete-backup
capacity profile:

~~~python
manifest_sha256 = app.backup(destination, capacity_profile='large-source-v1',
                             cancelled=cancelled)
~~~

The CLI equivalent is:

~~~text
python -m carr_library --data-dir LIBRARY --backup NEW_DIRECTORY --backup-capacity-profile large-source-v1
~~~

This produces complete manifest version 6 and, when pending imports exist,
pending component version 4. Both carry the same exact capacity name. A
self-rehashed manifest cannot change a request's profile or manufacture a
matching sealed source.

| Selected complete-backup allowance | Bound |
|---|---:|
| Catalog SQLite database | 64 GiB |
| Import journal SQLite database | 32 GiB |
| Registered original bytes | 128 GiB |
| Pending component bytes | 64 GiB |
| Manifest | 64 MiB |
| Registered originals | 100,000 |
| Aggregate journal checkpoints | 1,048,576 |

The ordinary backup retains its 512 MiB database/journal and 8 GiB original
limits. Existing backup shapes remain unchanged. Version 6 does not widen
individual audio, reader, assistant, translation, ASR, book or other typed
sidecar allowances.

Online database snapshots copy a bounded number of SQLite pages per progress
callback and check cancellation/selected size before no-replace publication.
Complete backup preflights actual current database, original, journal and
sidecar sizes against available disk space. Source bytes, manifest fields,
catalog bindings, journal relations and restored input identities are all
revalidated. Restoration never reactivates a prior worker attempt or external
folder traversal.

Late TXT fragments are reachable in the current Reading Desk. The profile is
derived from its exact sealed extraction configuration. Reading-copy anchors
keep the existing 10,000 selected-position limit while accepting an absolute
ordinal through 65,536, so selecting a small late range does not invent an
unbounded full-book conversion.

## CLI

~~~text
python -m carr_library import-large --data-dir LIBRARY --workspace ID --profile txt --file ORIGINAL.txt --max-mib 1024 --timeout 1800
python -m carr_library import-large --data-dir LIBRARY --workspace ID --profile pdf --file ORIGINAL.pdf --max-mib 2048 --timeout 3600 --ocr-language eng
python -m carr_library import-large --data-dir LIBRARY --workspace ID --resume-request REQUEST_ID --timeout 3600
~~~

The directory must already contain an admitted library. Opening it reuses the
read-only existing-library helper. A new prepared request ID is printed before
execution; resume cannot replace its format, byte allowance, metadata or OCR
settings. OCR options remain the current runtime's options; this command does
not download or approve models.

## Required execution evidence

The initial source/API checkpoint has **NOT_RUN** new execution status.
The following is the finite acceptance plan, not a claimed result:

- tests/ingest/test_large_source_profiles.py: strict version/byte identities,
  current owned TXT/PDF worker, actual PDF readers and independent review.
- tests/storage/test_large_source_backup.py: prepared/partial/completed
  restore and resume, exact v6/v4 fields, legacy default shapes, cancellation
  and retained-link recovery without deleting foreign links.
- tests/e2e/test_large_import_cli.py: isolated current-package CLI, frozen
  source profiles and existing-library boundaries.
- tests/ui/test_large_source_import.py: real service calls from deferred
  native handlers, frozen controls, cancellation and changed-library refusal.
- tests/e2e/test_large_source_fixture.py: actual streamed fixture generation
  and both current PDF parsers at a small size.
- tests/ui/probe_large_source_import_native.py: actual wx named numeric
  controls, keyboard focus, the real owned import worker and settled close.
  OS file-picker operation and manual NVDA are explicitly NOT_RUN.

Run scripts/large_source_acceptance.py against an explicitly selected
installed wheel. Default fixtures write more than 257 MiB of real TXT and more
than 129 MiB of real PDF bytes. Backslashes make the TXT checkpoint JSON and
catalog genuinely exceed the old 512 MiB limits. The fixture also contains
actual source positions above 10,000.

The script must prove a running child's cancellation, pending backup, removal
of the external original, fresh isolated resume, completed backup/restore,
late-page reading and exact quotations. It rejects a source import shadowing
the selected installed package and fails instead of skipping missing resources.

~~~text
python -I scripts/large_source_acceptance.py --expected-package INSTALLED/carr_library/__init__.py --workdir PRIVATE_WORK --output large-source-acceptance.json
~~~

Use a separately budgeted long lane with --txt-mib 2048 --pdf-mib 2048 for
actual originals above 2 GiB. The script requires measured free disk space and
records exact bytes, fragment counts, hashes and operation durations. Peak
RAM/VRAM remains NOT_MEASURED. Its PDF consists of a real painted page and a
large unused stream object: this proves large-original handling, not scanned
PDF quality, 1,000-page throughput or all possible PDF object structures.

Primary API references: [pypdf streaming guide](https://pypdf.readthedocs.io/en/stable/user/streaming-data.html)
and [pypdfium2 PdfDocument API](https://pypdfium2.readthedocs.io/en/stable/python_api.html#pypdfium2.PdfDocument).

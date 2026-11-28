# Import Center

Open **Library → Import Center: files, folders and queues** inside a library.
The center prepares and saves import plans for
TXT, PDF, DOCX, EPUB, supported text-document formats and PNG/JPEG/TIFF images.
It uses the same retained originals, extraction worker, review requirements and
search publication as ordinary book import.

## Import a collection of documents

1. Choose the destination collection and OCR models for the next plan. Manual
   language selection remains the default. **Choose installed or managed OCR
   models** also offers the explicit automatic/mixed-language mode described in
   [automatic OCR](AUTOMATIC_OCR_UK.md). These choices do not change an older plan.
2. Choose **Add files** or **Add folder**. For a folder, the **Include subfolders**
   checkbox controls whether nested directories are scanned.
3. Wait for the bounded file check. The saved source page lists its accepted
   candidates, exact duplicates, unsupported files and rejected sources. Checking
   a plan does not add books to the catalog. If traversal remains, select the
   saved page and choose **Continue source scan** to review the next page.
4. Select a file with the arrow keys. Its status and explanation are available in
   **Selected file details**. **Exclude or include selected file** changes an
   unstarted candidate; it does not delete a document or a catalog record.
5. Set **Queued rows per explicit work slice**, then choose **Start queued files**.
   The list and status update as files finish. The saved page remains available
   when one bounded slice ends; start another slice to continue queued rows.
   Successful imports appear in Books, Collections and Search.
6. Use **Cancel operation** to stop. Files that have not started remain queued.
   Close the center once the current worker has stopped. Reopen it later, select
   the saved batch and choose **Start queued files** to continue.

Use Tab and Shift+Tab to move between the native controls. The batch and file
lists have distinct accessible names and numbered entries. Selected-file details
and status are read-only text fields that support keyboard selection and copying.
The main window's library selector is unavailable while a batch operation runs.

## Large originals, source pages and resources

**Files per scan page**, **Traversal entries per scan page** and **Seconds per
source scan page** bound one explicit scan. Their defaults are 1,000 file rows,
20,000 traversal entries and 60 seconds. A page also has a finite metadata-byte
allowance. The saved continuation cursor resumes the same reviewed traversal;
these per-page controls do not impose a total 5,000-file library ceiling.

Use **Review import resource allowances** before creating a plan to choose the
original-byte, individual-text-record, segment/page, checkpoint-step, native-time,
worker-memory, temporary-space and library-growth allowances. The native dialog
shows every field and validates the complete profile. Cancel keeps the prior
choice; **Use current defaults** removes the explicit override. The chosen
profile becomes part of a new plan's identity and is preserved across its work
slices. Reviewing larger values does not rewrite an existing plan.

Only the streamed TXT and PDF routes accept originals above the ordinary 64 MiB
bound. Other document containers and raster images retain their own reviewed
parser limits. TXT records and PDF pages are processed with bounded readers;
retained-original digests, exact source identity and the complete publication
checks remain required. Requested memory and disk must be available. A resource
stop leaves accepted checkpoints available for an explicit continuation.

The desktop and CLI use the same bounded scan pages, current worker, import
journal and publication receipts. Measured throughput and supported machine
profiles come from the actual corpus qualification, not from increasing a limit.

## Duplicate handling and changed sources

An **exact duplicate** has the same SHA-256 content digest as an earlier file in
the plan or an already registered source in the selected library. Duplicate
checks do not cross library boundaries. Titles and author names are not used to
merge works or editions. An exact duplicate is reported and skipped; existing
collection membership and bibliographic metadata are not changed by that skip.
To choose a different identical copy within one plan, exclude the first copy,
select the other copy and choose **Exclude or include selected file** to include
it. A source already registered in the library cannot be included again through
this duplicate control.

The plan records a candidate's size, media type and digest. Before retaining or
publishing it, the worker checks the exact bytes it will process against that
record. If the source was replaced or edited after planning, the import fails
with an explanation. Create a new plan to import the changed document. A resumed
request with an intact retained original continues from those retained bytes.

Links and Windows reparse points are not traversed. Source pages and work slices
are bounded; the interface reports remaining traversal or a resource stop. The
scan and worker remain local to this computer.

## Failures and independent page review

**Retry failed or reviewed files** retries existing requests with their saved
identities. It does not create a fresh edition for every attempt. A single
failure does not erase completed imports or prevent the remaining queued files
from running.

A PDF or raster image marked **Independent page review required** still needs the existing
review workflow. Close the center and choose **Library → Review pending imported
page**. Compare the extracted text with the retained original and explicitly
record the review. The Import Center does not approve text or printed page
numbers automatically. Refresh the batch after a review, or retry it to continue
through the next page.

**Save report** writes a text summary to a location you choose. It contains file
names, outcomes and explanations, without source passages. The same guarded
export writer used by research exports prevents writing into the private library
data directory.

## Persistence and backup boundary

Import plans and native requests are saved atomically in the current private
data directory. Each planned document receives stable request, work and edition
identities before processing. Restarting the program reconciles the same
request, including an import that finished immediately before an interruption.

The current full-library backup includes its declared pending import requests,
journal checkpoints, review records and supported saved source pages, together
with the required retained or copied input bytes. Its resource policy must cover
those components; a backup cannot raise that policy itself. Inspect the manifest
for included, excluded and external data.

Restoring creates a new library folder. Saved requests and scan continuations
remain passive: opening a restored queue starts no worker and grants no source,
engine or external-operation approval. A restored traversal is marked for fresh
review, so choose the current source and create a reviewed plan before extending
that scan. Retained checkpoints preserve their recorded source data. Restore revokes prior
worker-attempt, publication-candidate and scan-review authority; valid saved rows
can run only through a new explicit action, while extending the old external
traversal requires a fresh reviewed plan. A text report is an outcome record, not a restorable queue.

## Command line

Both entry routes use the same current library and installation owner:

```text
python -m carr_library import-batch --data-dir "LIBRARY_FOLDER" --workspace "WORKSPACE_ID" plan "SOURCE_FOLDER" --review-default-budget
carr-import-batch --data-dir "LIBRARY_FOLDER" --workspace "WORKSPACE_ID" list
python -m carr_library import-batch --data-dir "LIBRARY_FOLDER" --workspace "WORKSPACE_ID" show "BATCH_ID"
python -m carr_library import-batch --data-dir "LIBRARY_FOLDER" --workspace "WORKSPACE_ID" run "BATCH_ID" --max-items 100
python -m carr_library import-batch --data-dir "LIBRARY_FOLDER" --workspace "WORKSPACE_ID" continue "BATCH_ID"
```

Replace the quoted placeholders with the existing library folder and exact
workspace/batch IDs returned by the current application. A mistyped library path
does not create a replacement library. Planning, listing, inspecting and continuing
a source scan do not import documents; `run` is the separate execution action.

Planning accepts `--source-mib`, `--segment-mib`, `--segments`, `--steps`,
`--seconds`, `--memory-mib`, `--library-mib` and `--temporary-mib`.
Specifying any of them freezes an explicit typed resource profile. With no
resource flags, existing default behavior and its identity remain unchanged.
`--ocr-languages eng+ukr+slk` selects an installed model pool;
`--auto-ocr-languages` explicitly enables the reviewed detector/regional mode.

Use `--page-files`, `--scan-entries`, `--scan-seconds` and `--page-bytes` for one
scan page. `run --slice-seconds` bounds the current worker slice;
`--retry-failed` requests retry of saved failed/reviewed rows. Exit code 2 from
`run` means that the selected page is not complete; inspect its saved status.
Ctrl+C requests cancellation and waits for the owned worker family to close.

## Workflow research and implementation boundary

The feature design draws on these official documentation pages:

- [Mendeley: adding files and folders](https://www.elsevier.support/mendeley/answer/how-can-i-add-new-references-to-my-library-in-mendeley-reference-manager)
  documents explicit file and folder import.
- [JabRef: import inspection](https://docs.jabref.org/collect/import/importinspectiondialog)
  documents reviewing candidates, selecting entries and inspecting duplicates
  before adding them to the library.
- [Zotero: duplicate detection](https://www.zotero.org/support/duplicate_detection)
  documents library-scoped duplicate review and the importance of preserving
  existing library relationships when resolving records.

These are workflow references. No upstream application code, network metadata
lookup or additional dependency is included. Exact-byte file detection is a
separate operation from bibliographic duplicate suggestions.

Automated service and native-control checks are reported separately from manual
Windows 11/NVDA acceptance. Passing automated checks does not certify screen
reader speech or every keyboard interaction on the user's machine.

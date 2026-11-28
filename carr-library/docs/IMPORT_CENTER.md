# Import Center

Open **Library → Import Center: files, folders and queues** inside a library.
The center prepares and saves import plans for the local document formats listed
under **Help → Document formats**. It uses the same retained originals, extraction
worker, review requirements and search publication as ordinary book import.

## Import a collection of documents

1. Choose the destination collection and the languages used on scanned PDF pages.
   These settings apply to the next plan, not to an older saved plan.
2. Choose **Add files** or **Add folder**. For a folder, the **Include subfolders**
   checkbox controls whether nested directories are scanned.
3. Set **Files per scan page**, **Traversal entries per scan page**, and
   **Seconds per source scan page**. Their defaults are 1,000 rows, 20,000
   traversal advances and 60 seconds. Wait for this page's file check. The saved
   page lists accepted candidates, duplicates and refusals; checking it does not
   add books to the catalog. Choose **Continue source scan** explicitly to review
   the next page. A page can be imported before the remaining folders are scanned.
4. Select a file with the arrow keys. Its status and explanation are available in
   **Selected file details**. **Exclude or include selected file** changes an
   unstarted candidate; it does not delete a document or a catalog record.
5. Choose **Start queued files**. The list and status update as individual files
   finish. Successful imports become available in the existing Books and
   Collections and Search sections.
6. Use **Cancel operation** to stop. Files that have not started remain queued.
   Close the center once the current worker has stopped. Reopen it later, select
   the saved batch and choose **Start queued files** to continue.

Use Tab and Shift+Tab to move between the native controls. The batch and file
lists have distinct accessible names and numbered entries. Selected-file details
and status are read-only text fields that support keyboard selection and copying.
The main window's library selector is unavailable while a batch operation runs.

## Duplicate handling and changed sources

An **exact duplicate** has the same SHA-256 content digest as an earlier file in
the page, an earlier saved page of this same scan, or an already registered source
in the selected library. Duplicate
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

Links and Windows reparse points are not traversed. File size and plan size are
bounded; the interface reports a limit rather than silently scanning an unlimited
directory tree. The scan and worker remain local to this computer.

## Failures and independent page review

**Retry failed or reviewed files** retries existing requests with their saved
identities. It does not create a fresh edition for every attempt. A single
failure does not erase completed imports or prevent the remaining queued files
from running.

A PDF marked **Independent page review required** still needs the existing
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

The checked complete and incremental library backups include completed catalog
records, typed import plans, interrupted requests, extraction checkpoints and
all referenced original bytes. Missing or changed originals make a complete
backup fail rather than silently omitting an unfinished source. Save the returned
manifest checksum independently and choose any required larger backup allowances
again when verifying or restoring.

Restore preserves each saved page's exact source policy and identities. Its
already reviewed rows can resume from the copied originals. Saved external folder
paths remain passive evidence: **Continue source scan** refuses a restored cursor
until the user makes a fresh source-folder selection for a new scan. Old worker
attempts and publication acknowledgements cannot authorize new execution in the
restored library. A text report remains an outcome record, not a backup archive.

## Explicit resource allowances and restart

**Review import resource allowances for new plans** opens eight named numeric
fields: original bytes, per-record bytes, logical-fragment count, checkpoint and
publication steps, native seconds, declared memory, library growth and temporary
space. **Use reviewed allowances** freezes a typed policy into subsequent pages.
**Use current defaults** restores the historical ordinary import limits for new
plans. Cancel leaves the prior choice unchanged. Existing saved pages always keep
their own policy; continuing a scan or reopening the program does not replace it.

A source limit above 64 MiB applies only to strict streamed UTF-8 TXT. PDF and other
whole-buffer adapters keep their current 64 MiB ceiling and format-specific
validation. Streamed TXT still uses logical fragments of at most 65,536 Unicode
characters. The chosen fragment and per-record byte limits remain independent.
Choose values that fit the current shared resource settings and the free space on
both the library and temporary volumes. These are declared cooperative reservations
and measured library-growth checks, not a claim of operating-system memory isolation.

Each queued row continues through its existing request and owned import worker.
When a selected step allowance ends, completed checkpoints remain saved and the
row stays ready for another explicit **Start queued files** action. It does not
publish a partial book. A completed import retains its original identity across
retries, including a child that committed just before cancellation.

A cancelled scan keeps its last admitted page and exact next-source position.
If no next source was admitted, no empty continuation is appended. If a directory
was replaced or changed since the saved cursor was captured, choose it again for
a new scan. Large directories are enumerated under the time allowance with a
bounded selection buffer; the entry control counts traversal advances, not every
name examined during that bounded enumeration. Saved-journal, cursor, depth and
metadata limits still apply. Paused duplicate classification is shown explicitly;
the catalog is checked again before importing a row.

If native cleanup is still pending, wait for that same process owner. The request
lock, shared reservation and private worker stage remain owned until native exit
and retained cleanup notifications finish. Closing the dialog does not grant a
second worker access to those resources.

The same explicit workflow is available from the installed command:

~~~text
python -I -B -m carr_library import-batch --data-dir PRIVATE_LIBRARY --workspace WORKSPACE_ID plan SOURCE_FOLDER --page-files 1000
python -I -B -m carr_library import-batch --data-dir PRIVATE_LIBRARY --workspace WORKSPACE_ID continue BATCH_ID
python -I -B -m carr_library import-batch --data-dir PRIVATE_LIBRARY --workspace WORKSPACE_ID run BATCH_ID
~~~

For a newly reviewed TXT policy, add `--review-default-budget` and the chosen
`--source-mib`, `--segment-mib`, `--segments`, `--steps`, `--seconds`,
`--memory-mib`, `--library-mib` and `--temporary-mib` values to **plan**.
**continue** preserves the saved policy and accepts only scan-page allowances.
**list**, **show**, **include** and **exclude** inspect or edit saved review choices
without starting extraction. The command requires an existing library.

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

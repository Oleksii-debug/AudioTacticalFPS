# Import Center

Open **Library → Import Center: files, folders and queues** inside a library.
The center prepares and saves import plans for the supported document formats
listed under **Help → Document formats**. It uses the same retained originals, extraction
worker, review requirements and search publication as ordinary book import.

## Import a collection of documents

1. Choose the destination collection and the languages used on scanned PDF pages.
   These settings apply to the next plan, not to an older saved plan.
2. Keep the default 120-second **Scan time limit per page**, or explicitly choose
   30–600 seconds for this scan. A larger allowance can help on a slow local disk;
   source, file-count and memory limits remain in force.
3. Choose **Add files** or **Add folder**. For a folder, the **Include subfolders**
   checkbox controls whether nested directories are scanned.
4. Wait for the file check. The saved plan lists every accepted candidate and
   explains exact duplicates, unsupported files and rejected sources. Checking a
   plan does not add books to the catalog.
5. Select a file with the arrow keys. Its status and explanation are available in
   **Selected file details**. **Exclude or include selected file** changes an
   unstarted candidate; it does not delete a document or a catalog record.
6. Choose **Start queued files**. The list and status update as individual files
   finish. Successful imports become available in the existing Books and
   Collections and Search sections.
7. Use **Cancel operation** to stop. Files that have not started remain queued.
   Close the center once the current worker has stopped. Reopen it later, select
   the saved batch and choose **Start queued files** to continue.

The scan allowance is saved with each page and restored when that queue is
selected. **Next scan page** retains the queue's destination and extraction
choices, while capturing the currently displayed scan allowance. A timeout
keeps earlier pages intact. You can increase this explicit allowance and retry;
every attempt still verifies the complete bounded source inventory.

Use Tab and Shift+Tab to move between the native controls. The batch and file
lists have distinct accessible names and numbered entries. Selected-file details
and status are read-only text fields that support keyboard selection and copying.
The main window's library selector is unavailable while a batch operation runs.

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

The checked library backup includes completed library records and retained
sources, plus validated pending import queues, requests, checkpoints and their
captured input bytes. Missing or changed planned sources fail backup rather than
being silently omitted. Restored imports remain paused until **Review restored
imports** approves the exact captured selection. External folder paths are not
followed; old scan continuations are disabled. The saved scan allowance remains
available as metadata. Sources beyond the captured pages need a new folder scan.
A text report is an outcome record, not a restorable copy of the import queue.

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

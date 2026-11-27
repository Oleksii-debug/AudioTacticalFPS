# Bounded folder scans and saved import pages

The Import Center has an explicit **Plan first folder page** action and
**Continue folder scan**. Select the source folder, destination collection,
recursion and OCR configuration. A page records its own real directory-entry,
supported-file and rejection counts. Review or exclude its files before using
the existing **Start queued files** action. Scanning never starts an import.

Each native page defaults to 500 supported files; the user can choose 1–5,000
and 1–3,600 seconds. The service default remains 5,000 files, at most 20,000
processed directory entries per page, and 60 seconds. A page can contain no
supported files and still have a continuation through later entries. The UI
therefore distinguishes “more entries may remain” from a completed scan.
No percentage is invented from an unknown whole-folder total.

Ordinary `plan_import_batch` and the previous Add files/Add folder actions retain
their version-one behavior and bounds. The additive service APIs are:

```python
first = app.plan_import_batch_page(
    workspace, [selected_folder], collections=(collection,),
    recursive=True, config=approved_ocr_config, file_budget=500,
    entry_budget=20_000, timeout=60, cancelled=stop_requested,
)
following = app.continue_import_scan(
    workspace, first["batch_id"], timeout=120, cancelled=stop_requested,
)
```

File/entry/time budget overrides affect only a newly scanned successor. Reopening an
already saved successor returns the same accepted plan and choices. Existing
saved pages remain independently selectable in the Import Center.

## What is bounded and authenticated

The scanner reuses the current immutable original-byte intake and no-link
policy. It streams a directory enumeration into a bounded lexical heap instead
of storing a manifest of every filename. Serialized source-row metadata is capped
at 8 MiB during collection, before the batch adds its identities and cursors. A
metadata refusal leaves the saved position unchanged; retry with a smaller page.
The persistent traversal stack carries
only directory identities and positions. Descending into a folder discards the
parent's temporary name list. Explicit overlapping selected roots are not
repeated. Each request keeps the existing source-size, maximum depth, rejected
row and metadata limits.

The unchanged directory is enumerated again when a later lexical window is
needed. This bounds memory, but a very large single directory can still cost
time to enumerate; the cooperative per-request deadline remains effective.
The deadline can be increased explicitly, without removing memory limits.
Changing or replacing a traversal directory causes a clear refusal and requires
a new selected-root scan. It never silently skips a changed listing.

Duplicate detection covers the current page and the authorized current catalog.
Two not-yet-imported pages can both contain the same bytes. Their existing
execution service rechecks the catalog before each real import, so the second
completed duplicate is not silently published as another import. A rejected or
excluded earlier source does not suppress an eligible later file.

## Durable version-two batch contract

Version-one journals keep their exact key set. A version-two journal adds the
strict `scan` object with:

- selected root spellings, recursion-bound input/output cursors and actual counts;
- the page's file, entry and time budgets;
- its page number, predecessor and optional saved successor IDs;
- a separate `resumable` flag for live versus restored traversal.

A successor ID is a deterministic digest of the predecessor ID and its exact
next cursor. A completed child is published with the shared no-overwrite
filesystem helper, under the application writer and the real native batch
locks. The parent is linked afterward. If the process stops between these two
publications, retry authenticates the existing child and re-syncs its directory
before linking it; it does not rescan or replace the child.

The relations validator checks root/configuration/scope/collection agreement,
cursor continuity, page ordering and the exact saved child ID. A missing linked
page, conflicting child or changed original is refused. The first unlinked
child left by an interrupted parent update is accepted only through that exact
deterministic relationship.

Cancellation before child publication leaves the predecessor unchanged.
Cancellation after publication does not relabel the saved child as absent;
the parent link is finished or remains explicitly recoverable. The complete
chain still has the existing 1,000 saved-batch bound, and each batch keeps its
16 MiB metadata / 10,000-row ceiling. These are reported safety limits, not
claims about corpus capacity.

## Backup, restore and external roots

Full-library backup uses the existing typed pending-import producer and existing
writer/batch/request locks. It retains all saved page journals, validated
lineage, prepared request state and the source bytes required by their rows.
It does not recursively copy roots that the user has not scanned.

When version-two scan pages are present, the pending component uses an explicit
version-two manifest. Its `scan_pages` summary declares that undiscovered
external inputs are not included and that restored traversal is inactive.
The summary is revalidated against the hashed actual batch records.
Existing version-one pending components remain supported unchanged.

Restore rebases only already retained row inputs into the new library and marks
every archived scan cursor `resumable=False`. Original selected-root strings
are historical metadata. Reopening an already saved child, including a proven
orphan, does not touch those old paths. Creating a new external scan page from
an archived cursor is refused; the user selects roots for a new first page.
Saved queued files can still import after the external originals are deleted.
Rebackup preserves the same inactive traversal and complete saved lineage.

This distinction is necessary: a backup can preserve all existing plans and
their required inputs while the original unscanned remainder remains external.
It must not be described as a copy of unknown undiscovered files.

## Acceptance and remaining qualification

Portable acceptance exercises actual 5,003-file and 20,001-entry directories;
saved page succession across reopening; the real import worker; reviewed
exclusions; cancellation; changed directories and unsafe links; conflicting
child refusal; post-move directory-sync failure and recovery; native lock
contention; complete backup/verify/restore/rebackup; deleted external originals;
and inert restored traversal. UI handler tests use the actual application
services and deferred callbacks, including changed-library and late-callback
isolation.

The relevant strict suites are:

- `tests/ingest/test_scan_pagination.py`
- `tests/e2e/test_import_scan_pages.py`
- `tests/storage/test_scan_pages_backup.py`
- `tests/ui/test_import_scan_pages.py`

These cardinality and correctness fixtures are not throughput qualification for
all document formats. Source/installed/Linux/Windows execution and native
screen-reader acceptance must be reported separately with their actual results.
See [the source-size and corpus plan](V3_LARGE_SOURCE_AND_CORPUS_PLAN.md) for the
remaining large-source and realistic corpus measurement contract.

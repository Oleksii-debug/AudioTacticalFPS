# Continue a large import scan

Import Center now saves source review in bounded pages. The original limit of 5,000 files remains the maximum for one page; it is no longer the limit for the selected source tree. Each page is an ordinary saved import batch in the same queue, with stable row, request and edition identities.

## Desktop workflow

1. Open **Library → Import Center** and select the destination collection, OCR settings and recursion for a new scan.
2. Set **Files per scan page**, **Traversal entries per scan page** and **Seconds per source scan page**, then choose **Add files** or **Add folder**.
3. Review the saved page. Exclude individual rows if needed. **Start queued files** imports only the selected saved page.
4. Select a saved page whose summary reports remaining sources and choose **Continue source scan**. This saves or reopens its exact next page; it does not start imports.
5. Repeat source review and explicit import until the summary reports that the selected traversal is complete.

Continue retains the original source roots, recursion, workspace, collections and OCR settings. Changing the controls for a new plan does not retarget the existing scan. Page budgets may be changed explicitly for the next page. Previously completed imports and exclusions remain on their original saved pages and can be selected in the existing queue list.

The page summary separates page-row counts from cumulative reviewed rows and complete source traversal. An empty page can represent bounded traversal progress through directories. Cancellation preserves completed pages and saves the fully checked prefix of the current page after source checks finish. A file interrupted while being verified remains before the cursor and is reconsidered on explicit continuation.

## Existing command-line entry

Use the installed application entry; this helper does not introduce another public worker or execution queue:

```text
python -m carr_library import-batches --data-dir LIBRARY_ROOT --workspace WORKSPACE_ID plan SELECTED_FOLDER --page-files 1000 --scan-entries 20000 --scan-seconds 60
python -m carr_library import-batches --data-dir LIBRARY_ROOT --workspace WORKSPACE_ID list
python -m carr_library import-batches --data-dir LIBRARY_ROOT --workspace WORKSPACE_ID show BATCH_ID
python -m carr_library import-batches --data-dir LIBRARY_ROOT --workspace WORKSPACE_ID exclude BATCH_ID ROW_ID
python -m carr_library import-batches --data-dir LIBRARY_ROOT --workspace WORKSPACE_ID run BATCH_ID
python -m carr_library import-batches --data-dir LIBRARY_ROOT --workspace WORKSPACE_ID continue BATCH_ID --page-files 1000
```

Plan also accepts repeated `--collection ID`, `--ocr-languages eng` and `--no-recursive`. Plan and continue accept `--page-bytes` for the metadata budget. Explicit run accepts `--file-timeout` and `--retry-failed`; existing quarantine and independent page-review rules still apply. `include BATCH_ID ROW_ID` explicitly includes a never-started excluded or duplicate row.

Commands emit a JSON result. Explicit run returns code 2 when the selected page remains incomplete, ordinary errors return 1, and keyboard interruption returns 130. A complete page can still have a subsequent source page: inspect its `scan.cursor.complete` field. Plan, continue, show and list do not import source content.

## Bounded work and durable replay

Defaults are 1,000 reviewed rows, 20,000 traversal advances, 60 seconds and 4 MiB of row metadata per page. Explicit upper bounds remain 5,000 rows, 20,000 traversal advances, 600 seconds and 8 MiB per page. Resource admission consumes the same page time allowance; it cannot grant a second full scanning allowance after waiting. If less than 50 ms remains, no new source scan starts and the unchanged cursor is saved with a time outcome.

The scan uses the existing resource broker and holds its lease through saved-page publication. It keeps only a bounded sorted directory window, an ancestor stack and one page of rows. Directory names may be enumerated again on a later page under its time and cancellation budget; no live directory iterator or operating-system offset is persisted. The selected roots and active directory frames are bound to lexical scope and exact filesystem identities. If a directory changes during review, continuation refuses instead of silently advancing through a different tree. Make a fresh explicit source selection to review that changed tree.

The existing 1,000 saved-batch journal budget still applies. This is bounded continuation, not an unlimited in-memory plan. The ordinary source-format size limits, no-link rules, private-library exclusion and explicit authorization remain in force.

A page has one deterministic next-page identity. If the next page was committed before the process stopped or its reply was lost, repeating Continue reopens that same page with the same row and request IDs. It does not scan again or append a competing page. Duplicate rows can refer to an exact original row in an earlier page of the same scan, or to an existing catalog original. Cross-page references bind the workspace, scan chain and complete source manifest. Catalog duplicate checks are also repeated by the existing import path before work starts.

## Backup and restore

Full and incremental backups use the existing typed pending-import adapter. Saved page rows, cross-page relationships and retained source manifests remain validated. Backups do not follow unreviewed cursor paths or execute scans.

Restoration retains the cursor only as passive review information and marks every restored scan as requiring a fresh source selection. **Continue source scan refuses that restored external cursor.** Already planned rows remain tied to their retained originals and can be imported explicitly through the existing queue after review. Restoring a backup does not start scanning, extraction, OCR or import.

## Verification boundaries

The source regression suite includes an actual 5,007-file tree across a child-process restart, child death immediately after next-page publication, interrupted source verification, cross-page duplicates, actual retained-original imports, admission-budget boundaries and typed restore with the original external tree deleted. CLI cases use actual guarded application children, including restore refusal and subsequent explicit import.

The native probe `scripts/windows_import_scan_smoke.py` requires installed modules by default. Its optional `--source-root` makes source testing explicit. It exercises native page controls and keyboard actions, actual isolated TXT imports, exact quotations, unchanged original bytes, saved-page reopen and released resource leases. Only the folder-picker result is supplied by the fixture. Windows execution and screen-reader acceptance must be reported from their actual gates; source review does not establish them.

# Import Center scan pages

Import Center can inspect more than 5,000 selected documents without placing an
unbounded queue in memory. Prepare the first plan with **Add files** or **Add
folder**, review its rows, then choose **Next scan page** to inspect and save the
next page. A page can be imported independently; every earlier page remains in
**Saved import batches**. Next uses the original collection, recursion and OCR
choices. The controls for new plans do not change an existing continuation.

This extends the existing folder walker, intake validator, `ImportBatchStore`
and isolated import worker. It adds no import engine, database schema, queue
directory or network access. Existing version 1 batch journals remain readable
and executable. Version 2 journals add typed scan metadata and deterministic
links between ordinary saved batches.

## What a continuation binds

A saved continuation includes the exact selected roots, workspace, collections,
recursion and OCR settings, an inventory fingerprint and the next position.
The inventory covers names and ordinary file/directory identities, sizes and
filesystem modification/change timestamps within the bounded scan. The same
walker checks that inventory before and after inspecting each page. A changed
inventory asks for a new scan and leaves previous saved pages intact.

The fingerprint is a local filesystem consistency check. It is not a hash of
the unread contents of every future source or a quotation credential. Every
file on the current page still passes the existing intake snapshot, and its
exact SHA256/MIME/byte-size manifest is saved. The import worker rechecks that
manifest and retains the bytes it actually checked. No page approves PDF/OCR
extraction or confers citation authority.

Workspace and destination collections are authorized before and after scanning.
Replacing the library/database/assets identity invalidates the operation.
Cancellation is sticky and each scan page has one deadline, including both
inventory checks and source reads. The default remains 120 seconds. The named
**Scan time limit per page (30–600 seconds)** control permits an explicit bounded
allowance for slow local disks or large pages. The selected allowance is saved
with the page and shown when that queue is reopened. After a timeout, increase
the allowance if appropriate and retry **Add folder** or **Next scan page**;
content choices and source validation are unchanged. Failure or
cancellation before saving the page leaves the previous journal unchanged.
Per-batch process locks serialize cooperating writers. Repeating Next after a
lost reply finds the same deterministic saved page, preserving its row, request,
work and edition identities, including completed imports.

Exact duplicate preflight is scoped to the current page and the selected
library's already registered assets. The existing worker checks again before
publication, including duplicates imported from another page in the meantime.
Equal bytes do not merge bibliographic identities.

## Bounds and incomplete scans

Each page reads at most 5,000 supported sources through intake, one source
snapshot at a time. The existing 64 MiB per-source and 16 MiB journal limits
remain. An 8 MiB metadata estimate can end a page earlier; its next position is
retained. Directory inspection remains bounded to 20,000 entries per traversal,
256 selected roots, depth 64 and 8 MiB of accumulated path bytes. Ordinary
rejection details remain capped at 100 with an explicit remaining count.

An oversized directory is not imported from an arbitrary enumeration prefix.
The saved record and report explicitly identify unscanned or partly scanned
source scopes. Select smaller source folders to inspect those scopes. Reaching
the end of the available pages does not mean an incomplete structural scan
covered all selected files. Rejected entries at a page boundary remain
reachable on the next page, even when no further supported documents exist.

## Restart, backup and API

Scan pages survive an application restart in the existing private
`import-batches` directory. A source plan references external selected files;
it does not copy them into the library during scanning. The checked complete
library backup captures those planned bytes into its owned pending-import
component, together with validated queues and request journals. Missing or
changed planned bytes cause backup to fail explicitly. Restoration keeps the
saved scan allowance, rebinds sources to the captured bytes and disables old
folder continuations. Choose **Review restored imports** before execution;
the old external paths are provenance, never permission to read those folders.
Select folders again to scan source files that were never captured.

```python
first = app.plan_import_batch(workspace, selected_paths,
                              collections=(collection,), recursive=True,
                              scan_timeout=300)
if first['scan']['next_offset'] is not None:
    following = app.next_import_scan_page(workspace, first['batch_id'],
                                         cancelled=cancel_event.is_set)
    completed = app.run_import_batch(workspace, following['batch_id'])
```

Each returned record contains `scan.page`, `scan.next_offset`,
`scan.next_batch_id`, `scan.previous_batch_id` and `scan.unscanned`. The lower
level `batch_scan.scan_page` exposes the same bounded read-only scanner; callers
must supply the preceding inventory fingerprint with a nonzero offset.

New V2 records also contain `scan.timeout_seconds`. Existing V1 records are
unchanged; V2 records without this optional field use 120 seconds. Calling
`next_import_scan_page(..., scan_timeout=None)` reuses the preceding allowance.
An explicit 30–600 second value changes the next scan's resource budget without
changing its source fingerprint, extraction options or deterministic page ID.
Reopening an already saved next page returns its original record and allowance.

# Continued import scans and EPUB book lists

This source candidate addresses the fixed scan/list ceilings in V3-07 through
the current import and reader services. It does not change original-byte size
admission, extraction approval, or the search continuation contract.

## Import Center

New Import Center plans use plan_import_batch(..., paged=True). A saved page
contains at most the existing 5,000 supported-file attempts and 20,000 newly
visited entries. **Continue scan** creates the next ordinary import batch from
the same explicitly selected roots, OCR settings, collection and workspace.
Each page is reviewed before its queued files are started. No following page
is scanned automatically.

The older one-shot scan_paths and version-1 batch API retain their contracts.
Version-2 batches add typed scan metadata. The next batch ID is allocated before
continuing: a reply lost after publication reopens that exact durable page.
Completed imports, source manifests, request IDs and edition IDs keep the
existing runner/replay and cross-batch duplicate protections.

Directory iteration keeps only the active depth-bounded stack, one current
source snapshot and one bounded result page. A saved cursor records directory
identity, metadata and the hash/count of its visited name prefix. Reopening
streams that prefix without reopening earlier source bytes. A changed active
directory or changed enumeration order fails explicitly, preserving earlier
pages. Directory order is filesystem-specific; this is not a claim of one
portable global alphabetical scan. Displayed rows within each page are sorted.

Cancellation discards only the unfinished page. Prefix replay checks
cancellation and a finite time budget; page work yields at its configured
budget. This design does not claim a 50,000-book throughput result or constant
resume cost: reopening a very large directory re-enumerates its preceding
names. Explicit nested selected roots retain their own depth allowance and are
not admitted a second time through their parent selection.

Typed backups copy the manifest-bound scanned source files using the existing
pending-import producer. The journal declares unscanned sources as
external_not_copied. Restore sets review_required: those external roots
cannot be continued until the user reviews/selects sources again. Restored
scanned files can still be imported from their verified retained bytes.
No backup producer follows the unscanned paths.

## EPUB chooser

The existing **Read EPUB book** action now uses list_ebooks_page. Previous and
Next are native chooser entries using the existing bounded PageHistory.
Only current-page rows and cursor metadata are held in memory; source text is
not cached for navigation.

Pages use title/edition-ID keysets in the existing SQLite ordering. A streamed
snapshot binds the active EPUB revision and metadata set plus the library,
topic and page size. A changed catalogue requires a fresh list instead of
silently skipping or retargeting a book. The existing SQLite deadline and
authorization guard applies before snapshot reads. Row count and metadata
bytes are bounded separately; the selected source is still verified by
read_ebook_section when opened.

The legacy list_ebooks API remains at its documented 500-edition bound for
compatibility. The desktop action uses continuation instead of that API.

## Validation status

The companion source inventory declares 42 new cases and 98 cases in the five
selected suites, preserving all preceding reader and Import Center assertions.
Fixtures include 5,001 real source files, over 20,000 real directory entries,
503 transactionally published editions of a retained synthetic EPUB, actual
isolated imports, lost-reply restart, duplicate publication prevention and
backup/restore without external source access.

Status is **NOT_RUN** for this candidate while the executor is unavailable.
These are runnable fixtures, not recorded test results. The existing native
EPUB section probe is unchanged. Actual new native chooser/button execution,
manual keyboard/NVDA acceptance, real corpus scale and performance remain
separate checks.

# Source comparison and catalog quality

The Research menu contains **Compare sources in active topic** (`Ctrl+Alt+M`) and
**Check catalog quality**. These commands use the currently open library. The
existing Academic outline export and reference-metadata review commands retain
their own established workflows.

## Compare saved sources

Open a research topic, then choose **Compare sources in active topic**. The native
dialog shows exact edition and revision groups, each saved quotation, its source
citation, researcher note and tags. Repeated titles remain distinct source rows;
related evidence retains its separate topic and evidence identity.

Toggle **Include subtopics** or choose one exact tag. A removed tag remains an
explicit empty filter; it never silently expands to all evidence. **Open selected
evidence and notes** returns to the existing editor and observes its unsaved-note
check. **Refresh comparison** reads the current library again.

CSV and Markdown exports take a new authorized snapshot, verify retained source
bytes and PDF review state, and retrieve current notes. A damaged or revoked
source causes the scoped operation to fail even when a tag would hide it.
Quotations and researcher notes have separate labels. CSV text fields use JSON
encoding, preserving their original content without allowing text to become a
spreadsheet formula. The output publisher refuses private library paths and
preserves the previous destination when cancelled.

A comparison permits up to 10,000 workspace topics, 1,000 selected topics, depth
32, 5,000 evidence items, 1,000 source revisions, 500 distinct tags, 100 tags per
evidence item and 16 MiB of stored evidence/notes/tag text. Exports have a 64 MiB
output bound. These are explicit resource limits; a larger request fails with an
explanation instead of showing an incomplete result.

## Review the catalog

The quality check reports missing title, authors or language; editions without an
active collection; registered asset reuse; and possible metadata duplicates. A
similar title, ordered author list and language is a review hint. The dialog does
not merge books, attest source bytes or change saved quotations.

Choose a collection to reduce the scope. All pages and memberships are read in
one authorized SQLite transaction. The existing catalog-report preflight rejects
more than 10,000 selected sources, 10,000 active collections, or 16 MiB of selected
UTF-8 metadata and active collection names before author lists are materialized.
Catalog rows are paged by stable edition identity. The native dialog shows 200
issues per page and retains full edition identities in details.

**Edit selected edition metadata** opens the established metadata form for that
exact edition. It uses the existing optimistic version check and refreshes the
catalog after saving. Saved quotation metadata remains historical.

## Accessibility and verification

The dialogs use native wx controls with explicit accessible names. Read-only
summaries, details and statuses are multiline text controls so keyboard users can
focus and review them. Collection choices and source rows expose disambiguating
identities or ordinals. Source review, filtering, exports, cancellation, scope
changes, damaged sources, multiline quotations and unsaved notes have portable
regression coverage; the same suites run in both required Windows source passes.

Manual Windows/NVDA speech acceptance remains a separate observed check. Portable
control tests do not establish the exact spoken output or human listening result.

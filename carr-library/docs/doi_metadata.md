# Identifier metadata and native citation documents

## One acquisition workflow

Use **Academic → DOI and ISBN lookup and open-access sources** for online
identifier lookup. It uses `carr_library.acquisition`, the same scoped service
used by the CLI and library facade. Opening the dialog does not authorize a
request. Select the provider and approve the particular metadata lookup.

The service supports the configured Crossref, Unpaywall, Europe PMC, OpenAlex
and Open Library flows. Review the returned title, authors, edition, source
location and rights information. Unpaywall requires an explicitly supplied
contact address. A metadata record, reported open-access label or link is not
permission to retrieve or reuse a document.

Saving a bibliographic reference and acquiring a document are distinct explicit
actions. The former records metadata and provenance. The latter requires a
selected source and rights decision, preserves the exact received bytes and
passes their expected digest, size and MIME type into the existing import path.
Library/source scope, network destination checks, bounded redirects, duplicate
handling, cancellation and publication receipts remain those of that service.
See [ACADEMIC_ACQUISITION.md](ACADEMIC_ACQUISITION.md) for its actual contracts.

The former `academic_integration.doi` client, its separate cache and its
standalone dialog are retired. The old Python UI function
`show_doi_preview(frame)` forwards to the existing acquisition dialog. It does
not create a second provider registry, automatically repeat a request or import
a preview without review.

## Citation document with endnotes

**Academic → Citation document with endnotes** opens the retained document
workflow. The default shortcut is **Ctrl+Alt+C** when available under the shared
keyboard manager. It uses the same installed QuickJS citation engine and styles
as the main citation-style workbench; it adds native Word endnotes, stable
citation identifiers and a bounded regeneration snapshot.

Select references from the active academic library, a current style and one of
the installed English, Ukrainian or Slovak locales. Use the Citations section
to add optional locators in document order. Check a locator only after comparing
that exact location with its source. Publication page ranges are not citation
pinpoints. Stored author strings remain literal when their personal-name
structure is unknown.

**F6** formats locally. **Ctrl+Tab** switches the References, Citations and
Preview sections. Preview and status use ordinary read-only native controls.
Cancel stops further publication; an active QuickJS evaluation remains subject
to its bounded CPU/memory budget. Close cancels queued work and prevents late
callbacks from touching the closed window.

Save chooses a new DOCX filename. It captures that target before rendering and
uses the shared atomic export writer. A newly appearing file or changed parent
is preserved; it cannot be overwritten by a stale selection. The library's
private storage is excluded, current source/reference bindings are rechecked
before publication, and a completed publication is distinguished from a
temporary-file cleanup notice. No original source or quotation is modified.

A source or bibliographic change invalidates the completed result. Reloading
references retains citation IDs where possible but withdraws old locator-check
flags when the source snapshot changes. Exported snapshots preserve the exact
processor/style identity and distinguish corruption checks from authentication.
Legacy external-engine documents require explicit review and new formatting
before moving to current processor pins.

See [academic_integration.md](academic_integration.md) for the typed API,
native endnotes, source-preserving regeneration, limits and resource licenses.
Source and installed tests must run on the final assembled commit; these
contracts do not assert manual keyboard, NVDA, Word add-in or real-publication
quality acceptance.

# Catalog tools for academic research

The Library tools dialog adds four practical workflows to existing imported
sources: catalog review, collection bibliography, source inventory and a
source-backed literature-review matrix. It is opened with **Ctrl+Shift+L**.
All work stays within an explicitly selected library and runs locally.

## Research and reuse decisions

The [Zotero bibliographic format documentation](https://www.zotero.org/support/dev/data_formats)
documents RIS, BibTeX and CSL JSON interchange. The new source-catalog exporter
supports those three formats, using Python's JSON support, the repository's
existing MIT rispy writer and its existing BibTeX escaping. No upstream
application source, new parser or new runtime dependency was introduced.

[Zotero collections](https://www.zotero.org/support/collections_and_tags)
allow one item in multiple collections. Our existing catalog has that same
many-to-many relationship, so bibliography scope follows the selected collection
without copying or moving a source. Each edition remains a distinct record even
when another edition shares its title or retained bytes.

[Zotero's duplicate detection documentation](https://www.zotero.org/support/duplicate_detection)
describes candidate detection from bibliographic fields. This increment reports
the narrower fact available in the imported-source catalog: multiple edition
records share a registered original SHA-256. The report does not inspect file
bytes again or assert that those editions should be merged. It supplies review
information and never deletes evidence or collection memberships.

The [CSL specification](https://docs.citationstyles.org/en/stable/specification.html)
provides literal name representation and bibliographic variables. Catalog
authors are exported as ordered literal names; commas, semicolons and the word
"and" inside a name do not create extra people. The current source catalog does
not contain a publication type, date, DOI, ISBN or publisher. Exports use a
generic type and omit unavailable facts. Reference-file metadata review preserves the same source-quotation authority.

## What each export contains

| Export | Scope | Content |
| --- | --- | --- |
| RIS, BibTeX, CSL JSON | Entire library or one active collection | Current metadata for every imported source edition, including editions without saved evidence |
| Source inventory CSV | Entire library or one active collection | Current title, ordered authors, language, edition, volume, scoped source identifiers and original digest |
| Review matrix CSV | Visible topic trees in one library, optionally filtered by current source collection membership | Saved exact quotations, separate personal notes, topic paths, tags, historical citation metadata, page/segment locations and immutable revision identifiers |

Bibliography and inventory never include quotations, research notes or document
contents. A catalog metadata correction changes subsequent bibliography exports;
it does not rewrite old evidence or its citation in the review matrix.

The review matrix authenticates candidate saved evidence through the existing
`SavedEvidenceStore` and historical source loaders before filtering by current
collection membership. It does not silently omit damaged or unverified records.
Each part of a quotation spanning multiple pages/segments has its own row and
source-slice number. It never joins separate passages into an invented quote or
labels an unpaginated text segment as a physical page.

## Interchange behavior

All files are UTF-8. CSL JSON and inventory CSV preserve Unicode metadata
losslessly. RIS rejects metadata containing control characters or newlines that
could become extra RIS tags; the error offers CSL JSON or CSV. BibTeX escapes
syntax and states that metadata line breaks and tabs become spaces.

CSV uses the same explicit, reversible JSON cell encoding as existing topic
exports. Columns ending in `_json` are decoded with `json.loads`. Names remain
strings, nullable values remain null, and author/tag/topic-path lists remain
arrays. Text beginning with a spreadsheet formula marker stays data. This
representation preserves source CR/LF and Unicode without adding characters to
the stored quotation.

## Bounds, cancellation and integrity

Catalog exports permit at most 50,000 editions and 32 MiB of generated UTF-8.
The review matrix permits 10,000 topics, 256 levels in a topic path, 50,000
candidate saved evidence records, 100,000 source slices and 64 MiB of generated
UTF-8. Application operations have a 60-second cooperative time budget. A native
source read already in progress must return before cancellation can complete.
Limits raise an explicit error; no partial file is published.

Report counts cover their whole stated scope. Detail samples disclose their
displayed and total counts, including shortened author lists and long fields.
Stored text, matching index rows, recorded printed-label reviews and recorded
source reviews are reported as separate facts. The report does not establish
fresh citation eligibility or certify the internal FTS index.

The dialog uses the existing desktop worker and checked export writer. It
disables duplicate actions while work is active, offers cancellation, rejects
stale workspace/collection callbacks and restores a readable focus target.
The standard Save dialog requests overwrite confirmation. Source-library files,
linked destinations and changed export targets remain protected by the existing
writer. No database schema or backup format changes are needed.

## Verification

Run the required tests from `carr-library`:

```console
python scripts/ci_check.py tests/catalog/test_bibliography.py tests/catalog/test_catalog_reports.py tests/research/test_review_matrix.py tests/e2e/test_catalog_tools.py tests/ui/test_catalog_tools_dialog.py
```

The Windows workflow includes these suites. Native wx interaction and manual
NVDA checks are separate evidence; headless handler tests alone do not establish
screen-reader accessibility. The keyboard sequence is Library tools, choose
scope, Refresh report, read the report, choose an export format, export, cancel
or close, then return to the original library. Existing research-note drafts
remain in their original controls throughout this read-only flow.

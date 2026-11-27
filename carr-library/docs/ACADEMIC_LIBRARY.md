# Academic library and reading lists

The Academic Library holds bibliographic references independently of imported
source documents. A book or article can be entered before its full text is
available. Creating a reference never fabricates an asset, extracted page,
quotation, source review or verified printed-page label.

## Available workflows

- Create and edit book, article, thesis, report, webpage and other references.
  Fields include title, ordered authors, publication year, journal or collection
  title, publisher, volume, issue, bibliographic page range, DOI, ISBN, URL and
  abstract. An optional document link must identify an existing edition in the
  same library.
- Maintain a reading list using unread, reading, read and on-hold states; low,
  normal and high priority; a zero-to-five rating; progress; tags and separate
  personal notes. These are the user's assessments, not automatic paper-quality
  or source-authenticity verdicts.
- Search Unicode metadata, authors, identifiers, abstracts, tags and notes.
  Search words are casefolded and combined with AND. Tag filtering matches the
  complete entered tag. Results are paginated and can be ordered by title,
  descending year, reading priority or most recent update.
- Inspect counts for reading states, archived records, linked documents and
  missing author, year or identifier metadata.
- Find potential duplicates. Exact normalized DOI and ISBN matches are labeled
  separately from conservative title/author/year suggestions. ISBN-10 and its
  equivalent ISBN-13 compare together. Different libraries remain separate.
  Nothing is automatically merged, deleted, replaced or linked to a source.
- Preview RIS, BibTeX or CSL JSON, review warnings and duplicate suggestions, select
  records, and import the selected batch atomically. Duplicate status is checked
  again at commit. Skipping duplicates is an explicit caller option.
- Export selected references or all active references as RIS, BibTeX, CSL JSON,
  CSV or readable text. Archive and restore records without losing their IDs.
  Optimistic edits reject an outdated form instead of overwriting newer notes.

## Source integrity and privacy

Bibliographic `pages` describes the publication's page range. It never supplies a
physical PDF page, a checked printed label or an evidence locator. An edition
link does not update catalog metadata, historical source metadata, retained
files, extracted text or saved evidence. Those remain governed by the original
source-verification workflows.

Catalog editing, bibliography import and export are local. The separate
**Academic → Preview DOI metadata (opt-in)** command requires explicit network
permission and returns metadata proposals without automatic import or paper
downloads; see [Academic integration](academic_integration.md#doi-metadata-preview).
Bibliography import does not fetch DOI metadata, resolve URLs,
download full texts or open attachment paths. Explicit URLs accept HTTP/HTTPS
without embedded credentials; storing a URL does not access it. DOI registrants
and ISBNs use ASCII digits; ISBN checksums are verified. Identifier normalization
does not create a missing identifier. DOI comparison folds ASCII A-Z only,
preserving distinctions between non-ASCII code points. Resolver URL paths are
strictly UTF-8 percent-decoded once; bare DOI percent signs remain literal.
Resolver URLs with credentials, a query or a fragment are rejected explicitly.
Registrant subprefixes and legacy shorter numeric registrants are supported;
syntax validation does not assert that a DOI is registered or resolves.

Private research notes are excluded from every export unless
`include_notes=True` is explicitly selected. RIS, BibTeX and CSL JSON export the
bibliographic fields, without private tags or reading assessments. CSV and text
are reading-list exports and include tags and reading fields. CSV uses labeled,
reversible JSON cells for untrusted text so formulas cannot execute in a
spreadsheet. BibTeX uses the existing escaped-field exporter; generic theses do
not acquire an invented doctoral degree. Metadata line breaks and tabs become
spaces in BibTeX, as stated in its header. RIS preserves multiline abstracts and
explicitly selected notes through safe continuation lines.

## Import compatibility

Inputs must be UTF-8, at most 8 MiB and at most 2,000 records. Malformed or
unfinished batches, conflicting single-valued fields, invalid identifiers,
unsupported URLs, duplicate JSON keys and resource-limit breaches produce an
explicit error before any record is written. Unsupported fields are named in
the preview rather than silently treated as supported data. Attachment paths
are explicitly reported as ignored.

RIS uses the already vendored MIT rispy parser/writer at
`b7aae3b2069ced3fb75287711300f2edf0bcac21`; vendored code and licenses are
unchanged. A local subclass preserves wrapped abstracts/notes and literal
semicolon-containing URLs, after a strict framing check. The separate original
`catalog.ris_preview` contract remains unchanged.
The publication-title order is T2, then JF, then JO. Different alternate or
abbreviated journal titles are reported in the preview rather than causing an
otherwise valid batch to fail.

CSL JSON accepts an array or a single reference object. It supports literal
authors and structured family/given names, date-parts or a literal publication
date, identifiers and common bibliographic fields. Structured names become
display names with a preview warning; export represents them as literal names
without guessing name components. More precise publication dates are imported
as their year, with a warning. Unsupported item types become `other` with an
explicit warning. This catalog interchange profile retains the supported fields
and reports conversions for review.

BibTeX import uses the pinned local parser. Citation keys and unsupported fields
remain in private reference notes; bibliography import never opens attachment
paths or URLs. **Academic → Format bibliography and citations** provides the
separate CSL formatter and editable Word export. It does not install a Word
add-in or update fields in a running Word document. See
[Academic integration](academic_integration.md) for styles, runtime requirements,
source checks and regeneration limits.

## Persistence and service contract

`storage.academic_schema.ACADEMIC_SCHEMA` is appended to the frozen V3 schema by
the host's V4 storage integration. `academic_reference` has a workspace-scoped
identity, same-workspace edition foreign key, strict enums and numeric bounds,
canonical author/tag JSON, immutable identity/time-of-creation guards, and
archive-only removal. `validate_academic_database(db)` streams every reference,
including archived workspaces, and checks decoded fields, exact search-cache
derivation, timestamps and document links. It is part of V4 integrity and backup
validation; the service does not perform a migration itself.

`AcademicLibrary(db, workspace)` requires an active caller snapshot, foreign keys
and an active workspace. Writes additionally require recursive triggers. It
never commits the caller's transaction. The principal methods are:

```python
reference = service.create(title="A title", authors=("An author",))
reference = service.update(reference.id, expected=reference, reading_status="reading")
rows = service.list(query="methods", status="reading", limit=200, offset=0)
preview = service.import_preview(data_bytes, format="csl-json")
result = service.import_selected(preview, (0, 2), skip_duplicates=False)
text = service.export(ids=None, format="ris", include_notes=False)
service.archive(reference.id, archived=True, expected=reference)
```

`Reference`, `ImportEntry`, `ImportPreview`, `ImportResult` and `DuplicateGroup`
are frozen transfer objects. Import entry indices are zero-based; entry fields
use an immutable mapping. `ImportResult` reports imported IDs, skipped indices
and duplicate IDs. `list` defaults to 1,000 rows with explicit pagination;
`export(ids=None)` includes all active records, not just the visible page. Bulk
duplicate/export operations explicitly reject libraries above 100,000 records
rather than silently truncating them.

## Design references

Official product documentation was inspected to select useful academic-library
workflows, rather than copying another application's implementation:

- [Zotero duplicate detection](https://www.zotero.org/support/duplicate_detection):
  identifier and metadata comparisons within one library, reviewed resolution.
- [JabRef reading status, priority and ranking](https://docs.jabref.org/finding-sorting-and-cleaning-entries/specialfields):
  explicit reading and assessment fields.
- [Zotero searching](https://www.zotero.org/support/searching):
  metadata/tag searching and continuously evaluated saved criteria.
- [JabRef import inspection](https://docs.jabref.org/collect/import/importinspectiondialog):
  selection and duplicate inspection before adding records.
- [Official CSL input schema](https://github.com/citation-style-language/schema/blob/master/schemas/input/csl-data.json):
  bibliographic item, name and date interchange shapes.
- [DOI Handbook](https://www.doi.org/doi-handbook/html/):
  Basic Latin case equivalence, numeric registrants and URL percent-encoding rules.

The first expansion implements the workflows described above. Native Windows
and screen-reader acceptance must be recorded on the integrated final source;
backend tests alone do not establish NVDA behavior.

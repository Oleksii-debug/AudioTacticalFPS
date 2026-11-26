# Source comparison matrix

## Purpose and boundary

`render_literature_matrix(analysis, *, format='csv', include_trace=False,
cancelled=lambda: False)` consumes the existing `CatalogAnalysis` and
`SourceInsight` records from `catalog/insights.py`. It produces CSV, Markdown or
plain text for comparing the actual document catalog. It does not add a second
bibliographic database or create standalone reference records.

The report shows source title, authors in their original order, language,
edition, volume, active collection names, indexed and total text-unit counts,
physical PDF pages, unpaginated logical segments, matching recorded PDF review
counts, stored evidence-link counts, visible topic paths, evidence tags and
catalog checklist items. Input source order is retained. Distinct editions stay
separate even when their retained bytes match; repeated edition identities are
rejected rather than double-counted.

The analysis must come from the caller's authorized snapshot. Rendering does
not reauthenticate a source and cannot grant quotation, printed-page or review
authority. Every format includes the analysis service's `NOTICE`. Recorded
review counts are deliberately not labelled as fresh verification. No source
quotations, private research notes, evidence payloads, attachment paths or
filesystem locations are read. There is no option to include those fields.

Trace output is explicitly opt-in and adds library, collection, source edition
and revision IDs plus the retained asset digest identifier. A digest displayed
as metadata is not a newly verified checksum. Default output omits these trace
columns. Topic names remain the analysis service's full visible paths, including
its disambiguating topic-ID suffixes.

## Reused components

- Python's standard-library `csv.DictWriter` implements CSV quoting and field
  framing. No new CSV parser or spreadsheet engine is bundled.
- The existing `research/evidence_export.py::_encoded` helper provides the
  project's reversible JSON encoding for CSV text, including control and format
  character escaping. The source strings themselves are never changed.
- The analysis service's `display_value` provides readable catalog text for
  Markdown and plain text, with explicit missing values and semicolon-separated
  lists. These are metadata views, not exact source-quotation exports.
- The analysis service supplies the existing report notice, issue descriptions,
  immutable row types and metadata/source limits. The exporter never opens a
  database, file, parser subprocess or network connection.

Primary documentation consulted on 2026-11-26:

- [Python CSV documentation](https://docs.python.org/3.11/library/csv.html)
- [Python JSON documentation](https://docs.python.org/3.11/library/json.html)
- [GitHub Flavored Markdown specification: backslash escapes](https://github.github.com/gfm/#backslash-escapes)
- [OWASP: CSV Injection](https://community.owasp.org/attacks/CSV_Injection)
- [Zotero: Searching](https://www.zotero.org/support/searching)
- [JabRef: Groups](https://docs.jabref.org/finding-sorting-and-cleaning-entries/groups)

This module reuses repository code and Python standard-library facilities; it
introduces no third-party runtime package or vendored license obligation.

## CSV contract and spreadsheet handling

The first CSV record has `record_type=report` and carries actual library and
collection scope, the source count, notice and analysis warnings even when the
scope contains no sources. Following `record_type=source` records are comparison
rows. The file always has one consistent header and column count.

All untrusted textual values are JSON-encoded in explicit `*_json` columns.
Scalar strings begin with a literal JSON quote; lists begin with a bracket;
missing values are `null`. This protects leading formula characters, full-width
variants, whitespace/control prefixes, delimiters and embedded newlines without
silently changing metadata. `text_encoding=json-v1` documents the representation.
`json.loads(cell)` recovers each original scalar or list. Count columns are
validated nonnegative integers, and `csv.DictWriter` quotes all fields.

This is an intentionally encoded interchange CSV, not ordinary unquoted display
cells. Spreadsheet users will see JSON quotation marks or list brackets. They
should retain that encoding when opening the report; decoding or rewriting the
cells in another program changes the safety boundary. As OWASP explains, there
is no universal CSV neutralization strategy for every spreadsheet and every
downstream save/reopen transformation. Tests verify the produced CSV's framing,
prefixes and exact Python CSV/JSON round trip; they do not claim every external
spreadsheet configuration has been tested.

## Markdown, plain text, cancellation and bounds

Markdown and plain text show ordinary names, semicolon-separated lists, `Not
recorded` for missing metadata and `None` for empty lists. They contain no JSON
quotation marks, `null` values or encoding instructions. The catalog display
helper normalizes line breaks and renders unsafe controls visibly. Markdown
then escapes ASCII punctuation, including pipes, HTML delimiters, links, images,
backticks and backslashes. Plain text uses labelled source records, suitable for
a native multiline reader and screen reader navigation. Both include scope and
the data/authority notice.

Rendering validates the complete analysis before returning a report, including
identity uniqueness, count relationships, Unicode and finite tuple inputs.
The source limit and 16 MiB input metadata budget are reused from the analysis
service; output is bounded to 32 MiB. A single field is limited to one million
characters so legitimate deep visible topic paths fit within the total budget.
Text lists are bounded to 10,000 values and authors to 100. Limits fail explicitly
rather than silently truncating sources, names, topics or tags.

The cancellation callback is checked before input access, during validation and
rendering, on output writes and before return. Cancellation raises
`InterruptedError`; no partial report is returned. File selection and atomic
publication remain with the existing export service and native UI controller.

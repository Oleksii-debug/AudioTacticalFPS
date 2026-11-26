# Library overview, catalog checklist and source comparison matrix

The source comparison matrix summarizes imported sources and their research
coverage without exporting quotations or personal notes. For a verified
evidence-by-evidence matrix containing quotations and separate notes, use the
existing **Library tools** dialog and its review-matrix export. Both workflows
use the same source catalog and saved research; they do not create a second
library or rewrite quotations.

These tools help you understand an imported source collection, find incomplete
catalog records and prepare a table for a literature review. They read the
current library and its stored research links. They do not change your books,
evidence, notes, topic tree or search index.

## Open a report

Open the library you want to work with, then choose one of these commands in the
**Research** menu:

| Command | Use it to |
| --- | --- |
| **Library overview…** | Read source totals, available and indexed text counts, language coverage, collection membership and use in visible research topics. |
| **Source metadata and indexing checklist…** | Find sources with missing catalog information, missing active text, absent search-index rows or missing recorded PDF reviews. |
| **Export source comparison matrix…** | Save a source-by-source comparison as CSV, Markdown or plain text. |

Each command first opens **Report scope**. Choose **All sources in this library**
or a numbered collection. A collection report includes only the editions
currently assigned to that active collection. One source can belong to several
collections and still appears only once in the report.

The overview and checklist open as readable text. Use the usual keyboard and
screen-reader text navigation; press Escape to close the report. Report actions
preserve the current research draft. If the active library, topic or chosen
collection becomes unavailable while the action is running, open the command
again in the intended scope.

Reports describe a consistent snapshot. Rerun the command after importing,
editing metadata, changing collection membership or saving new evidence.

## Understand the counts

| Field | Meaning |
| --- | --- |
| Source editions | Imported catalog editions in the selected scope. Different editions are separate rows. |
| Active text units | Text units in each edition's active imported revision. |
| Indexed text units | Active text units with a stored search-index row. This count does not assess transcription quality or prove index contents correct. |
| Physical PDF pages | Active text units with a physical PDF page position. This is not a printed page number. |
| Unpaginated logical segments | Text units without physical page positions, such as TXT and DOCX segments. They are not presented as printed pages. |
| Recorded PDF page reviews | Active review records matching the current edition, revision, physical page and stored raw-text digest. |
| Stored evidence links | Saved evidence records connected to an edition through visible topics. A record spanning several pages or segments counts once. |
| Visible research topics | Topic paths for those stored links. A topic below an archived ancestor is hidden, even if the child's own archive flag is clear. |
| Evidence tags | Tags attached to those visible saved evidence records. These are evidence tags, not newly created source-level tags. |

Saved evidence from a previous metadata revision remains a historical link to
the same catalog edition. Its count does not disappear merely because the book's
title or authors were corrected. A PDF review from that previous revision is
not transferred to the new revision.

**A recorded review count is not a fresh check of source authenticity, quotation
text or printed page numbers.** These reports do not open retained source files,
render PDFs or run OCR. If an original file is missing or damaged, its stored
catalog information and otherwise valid stored links can still appear. Use the
source and evidence workflows when you need to read and verify an original or
copy an exact quotation.

Malformed or unresolvable stored evidence produces a warning. Such records do
not contribute evidence counts, topic labels or tags. Open **Evidence and Notes**
to investigate; the report does not repair or delete the record. The warning
counts unavailable links in visible topics in the selected library and does not
reveal their text or notes.

## Work through the checklist

The checklist gives each listed edition's title, authors and **Source ID**,
followed by concrete actions. Use **Books and Collections** to edit catalog
metadata or assign a source to a collection.

Missing author names or a language code mean no value is recorded. They do not
prove the source is wrong. Collection membership is optional. Add information
only when it is known; do not invent a title, author or language to clear an
item.

An absent active revision calls for checking or resuming the source import.
Missing index rows call for investigation of search readiness. The checklist
does not rebuild the index or alter the imported text. A missing PDF review
record calls for the existing explicit review workflow when you need verified
evidence; the checklist itself grants no approval.

An empty checklist means these inventory checks found no items. It is not an
assessment of the source's scholarship, completeness, relevance or reliability.

## Export a source comparison matrix

Choose **Research > Export source comparison matrix…**, choose the report scope,
then choose **CSV table**, **Markdown table** or **Plain text**. The native Save
dialog suggests `source-comparison-matrix.csv`, `.md` or `.txt` respectively.
Choose a destination outside the application's private library data directory.

Every matrix includes the selected library and collection, report limitations,
source titles, authors in their recorded order, language, edition, volume, active
collections, the stored counts described above, visible topics, evidence tags and
checklist items. Each source has a numbered row or section. The desktop export
does not include edition IDs, asset hashes, revision IDs or filesystem paths.
Topic labels include their topic identifiers to distinguish equal names. The
separate checklist includes Source ID when you need that catalog reference.

**Quotations and research notes are excluded.** Titles, authors, topic names,
tags and collection names are still your research metadata; review the file
before sharing it.

Markdown and plain text use readable names and lists. Missing values are shown
as **Not recorded**, with **None** for empty lists. Line breaks and display
control characters in supplied metadata are made safe for report presentation;
the catalog values themselves are unchanged.

### Read the CSV safely

CSV is UTF-8 and has a header row. The first data row has
`record_type=report`: it contains the scope, source count, limitations and
warnings, including for an empty collection. Subsequent rows have
`record_type=source`. Filter on `record_type=source` when comparing editions in
a spreadsheet or analysis tool.

Numeric count columns contain numbers. Text column names end in `_json`, and
their values use reversible JSON notation. For example, a title is encoded as
`"A history of railways"`, authors as `["First Author","Second Author"]`, and
an absent value as `null`. Arrays preserve the source order where that order is
meaningful. A CSV reader removes CSV quoting; a JSON reader then recovers the
original text or list from these fields.

This deliberate encoding keeps titles and names beginning with characters such
as `=`, `+`, `-` or `@` from becoming spreadsheet formulas. Do not strip the JSON
wrapper and paste untrusted text into formula-interpreting cells. Choose plain
text or Markdown when your priority is reading the matrix without these encoded
columns. The CSV is a comparison report; it is not a RIS, BibTeX or CSL-JSON
bibliographic interchange file.

## Cancellation and large libraries

Use **Cancel current operation** while a report is being prepared. Cancelling
the scope, format or Save dialog creates no export. Cancelling before final file
publication preserves an existing destination file. If publication has already
finished when a cancellation or library change is detected, the status reports
that the matrix was saved; it does not pretend that the file was undone.

Reports have explicit reading and output limits and a time budget. If a limit
is reached, the operation fails with a message instead of returning an apparently
complete, silently shortened report. Choose a smaller collection and rerun it.
The ordinary overview and checklist do not create files.

## Design references

The distinction between collection membership and source identity follows a
common library workflow: Zotero documents that one item can belong to several
collections without being duplicated. Its documentation also distinguishes
collections from tags. See [Zotero: Collections and
Tags](https://www.zotero.org/support/collections_and_tags).

The checklist is an editorial aid for recorded metadata. JabRef provides a
separate integrity-checking workflow for bibliographic entries; see
[JabRef: Check integrity](https://docs.jabref.org/finding-sorting-and-cleaning-entries/checkintegrity).
These references inform the workflow. This feature does not embed either
application, exchange data with it or claim that metadata checks authenticate a
document.

## Verification scope

`tests/e2e/test_catalog_insights_workflow.py` exercises real `LibraryApp` imports,
SQLite transactions, retained TXT/PDF bytes and saved evidence. It covers library
and collection isolation, archived topic ancestors, spans counted once,
historical links, fresh-revision review separation, damaged originals,
unresolvable evidence warnings, absent index rows, cancellation during an actual
SQL read, explicit limits, CSV text round trips and read-only behavior.

Controller and native dialog checks live with the UI tests. Python workflow
tests alone do not establish Windows/NVDA usability or verify a user's actual
sources.

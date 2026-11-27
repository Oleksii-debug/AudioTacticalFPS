# Academic bibliography and research outlines

The catalog exchange and research-outline commands operate on the currently open
library. They use local files and existing source verification; they require no
account or online API.

## Compare saved sources in a topic

Open the intended library and research topic, then choose **Academic → Compare
sources in active topic**. The comparison contains the topic's
already saved evidence. **Include subtopics** and the exact tag filter control
the visible scope. Select a source version and an evidence item to read its exact
quotation slices, source citations, locations, researcher notes and tags.

Edition and historical revision identify each source row. Equal titles do not
merge editions or revisions. A theme count describes researcher-supplied tags;
it does not establish agreement, reliability or an AI-generated conclusion.
The service rechecks retained originals, historical metadata and source-review
authority for every scoped evidence item, including evidence hidden by a tag
filter. Damaged sources or revoked review prevent a successful report.

**Open selected evidence and notes** returns to the existing evidence editor.
Its unsaved-note confirmation and Save All behavior remain in force. **Refresh
comparison** reloads current saved notes and tags. CSV and Markdown export also
obtain a new verified snapshot; they never publish a stale displayed cache.
Replacing an existing export needs the normal destination confirmation, and a
changed destination is rejected before publication. CSV cells carry reversible
JSON strings/arrays so quotations remain exact and cannot become formulas.

This view uses the existing library database and backup format. Its explicit
bounds are 10,000 workspace topics, 1,000 selected topics, depth 32, 5,000 evidence
items, 1,000 source revisions, 500 themes and 16 MiB of stored text. Exceeding a
bound fails the operation rather than silently omitting results. Reads and
exports use the desktop job runner and its **Cancel current operation** control.
The existing **Research → Export topic and subtopics** command remains available.

## Review catalog quality and correct a selected book

Choose **Academic → Check catalog quality**. The view reports missing titles,
author names or languages; editions outside active collections; shared registered
asset digests; and possible duplicate metadata. Choose a collection, an issue
filter and a page of results to inspect the exact affected edition IDs.

**Edit selected book metadata** opens the existing guarded metadata editor for
that edition. Title and authors apply to its work; language, edition and volume
apply to the selected edition. A concurrent metadata change requires a fresh
review. Original assets and previously saved quotation metadata remain unchanged.
After saving, run the check again to inspect the current catalog.

Similarity is a review hint: no records are merged or deleted automatically.
The catalog check does not reverify source-file bytes or disambiguate real people.
It reads one authorized database snapshot, retains edition/volume distinctions,
and uses the existing catalog limits, including the 10,000-source preflight.
Cancel, closing the window or changing the active scope suppresses stale results.

### Integration provenance and acceptance boundary

The topic comparison and catalog-quality views adapt the bounded implementations
from [canonical source 966e5cc](https://github.com/Oleksii-debug/AudioTacticalFPS/tree/966e5ccf892243ead6942c635af5fbab47e72551/carr-library/src/carr_library).
Only the new views and two menu actions are composed here. Existing acquisition,
bibliography import, outline export, author representation, source authority and
database/backup schemas remain the selected application's implementations.
Both new commands are available to the existing command palette and shortcut
settings. They introduce no default shortcut that could replace a saved binding.
`tests/ui/native_academic_comparison_smoke.py --require-windows` exercises the
installed native views, note preservation and exact metadata correction. Final
composed Windows and NVDA acceptance are separate from portable source tests;
they are not implied by the donor's qualification.

## Review metadata from a reference manager

1. In **Books and Collections**, select the existing book to update.
2. Choose **Bibliography → Review reference metadata for selected book**.
3. Select a local RIS or CSL-JSON file and choose its reference record.
4. Read **Reference review**. It lists current values, proposed values, fields
   the catalog cannot store, and any conversion notes.
5. For each desired change, choose the reference candidate and press its named
   **Use reference title/authors/language/edition/volume** button. The editable
   fields retain their current values until you choose a replacement. You may
   correct them before saving. Names remain in their supplied order.
6. Press **Save metadata**. **Cancel** discards the proposed changes.

Title and authors apply to all editions of the same work; language, edition and
volume apply to the selected edition. The save checks whether another operation
changed the original catalog entry. Previously saved quotations retain their
historical citation metadata, and original source files are preserved.

The current catalog stores title, authors, language, edition and volume. Reference
types, identifiers, publication dates, publishers and other fields are reported
as unsupported rather than inserted into unrelated fields. A reference file
does not create a verified source or approve an extracted quotation. No URLs or
attachments from it are opened. Structured CSL names are converted to a readable
literal for review; their original name-part structure is not stored.

Reference preview accepts UTF-8 RIS or CSL-JSON, up to **4 MiB and 5,000 records**.
Malformed, ambiguous JSON keys and oversized input fail before any save. Split
larger files into smaller batches. This is a documented, limited exchange
profile, not a lossless replacement for a reference manager's complete database.

For standalone references and reading lists, open **Academic → Academic library**.
Its reviewed import also accepts BibTeX; the selected-book metadata chooser above
continues to accept RIS and CSL-JSON. See [Academic library](ACADEMIC_LIBRARY.md)
and [Academic integration](academic_integration.md#reviewed-bibliography-import-and-safe-desktop-export)
for the respective fields and limits.

## Export a bibliography

Choose **Bibliography → Export library or collection bibliography**, select all
books or one collection, and choose **CSL-JSON**, **RIS** or **BibTeX** in the save
dialog. You do not need to save quotations first. This uses the same catalog
exporter as **Library tools** (**Ctrl+Shift+L**), which also offers source inventory
and literature-review matrix exports described in [Catalog tools](CATALOG_TOOLS.md).

Each edition produces a separate reference, even if two editions have the same
title or original bytes. Only known catalog metadata is included. Author strings
remain individual literal names; publication dates and more specific reference
types are not guessed. Bibliographies exclude source text, research notes, file
paths and original-file hashes. They are not library backups or formatted
APA/Chicago reference lists.

For styled references, choose **Academic → Format bibliography and citations**.
The separate CSL workflow also creates editable Word documents. Optional
**Academic → Preview DOI metadata (opt-in)** requests require explicit network
permission and return proposals for review without automatic imports or paper
downloads. See [Academic integration](academic_integration.md) for supported
styles, local runtime requirements and Word-regeneration limits.

The export uses one consistent catalog snapshot and accepts up to **50,000
references and 32 MiB of UTF-8 output**, within a 60-second cooperative time budget.
Choose a smaller collection if it exceeds those limits. A large export may
exceed the preview-import limits above. RIS
requires single-line values; CSL-JSON preserves catalog text without imposing
that RIS restriction. BibTeX uses the application's existing TeX escaping and
whitespace conversion.

## Export a complete research branch

Open the parent topic, then choose **Research → Export topic and subtopics** or
press **Ctrl+Shift+E**. The Markdown document follows the selected topic's sibling
order and includes all its visible descendants. Archived branches and unrelated
topics are excluded. Numbered paths retain the hierarchy beyond six heading
levels.

Each saved quotation is authenticated against its historical source and metadata
before export. Citations, exact quotations, research notes and tags remain
separate. A damaged source or invalid saved reference stops the whole export.
The document states its topic/evidence counts. Limits are **1,000 topics, 10,000
evidence records and 16 MiB** of saved input/output; use a smaller branch if needed.

**Ctrl+E** continues to export only the active topic in the existing formats.
Cancellation or failure before publication preserves the selected destination.
Exports are saved outside the private library folder through the existing atomic
file publisher.

## Reuse and interoperability references

- [Zotero supported data formats](https://www.zotero.org/support/dev/data_formats)
  lists CSL-JSON, RIS and BibTeX exchange.
- [Zotero library and collection export](https://www.zotero.org/support/kb/exporting)
  documents these useful scope choices.
- [CSL 1.0.2 item-data schema](https://github.com/citation-style-language/schema/blob/v1.0.2/schemas/input/csl-data.json)
  defines generic `document` items and literal author names.

These catalog-exchange commands reuse the existing licensed rispy reader/writer
and source catalog. Their local RIS/CSL-JSON review does not require a network
service. See the
[reference exchange profile](../src/carr_library/catalog/REFERENCE_EXCHANGE.md)
and [Catalog tools](CATALOG_TOOLS.md) for the reviewed intake adapter, shared
catalog exporter and upstream provenance.

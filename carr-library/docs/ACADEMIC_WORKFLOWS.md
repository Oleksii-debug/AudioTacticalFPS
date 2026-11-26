# Academic bibliography and research outlines

These desktop commands operate on the currently open library. They use local
files and existing source verification; they require no account or online API.

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

The existing licensed rispy reader/writer is reused. No new runtime dependency,
database schema or network service is introduced. See the
[reference exchange profile](../src/carr_library/catalog/REFERENCE_EXCHANGE.md)
and [Catalog tools](CATALOG_TOOLS.md) for the reviewed intake adapter, shared
catalog exporter and upstream provenance.

# Academic research workflows

This guide covers the academic workbench in **0.2.0.dev1**.

Open the **Academic** menu with **Alt+A**. These four workflows use the existing
local library, retained originals, research topics, notes and tags. They add no
runtime dependencies or database schema changes.

| Command | Start from | Result |
| --- | --- | --- |
| Compare sources in active topic — **Ctrl+Alt+M** | An open research topic with saved evidence | Compare source versions, themes, citations and notes; export CSV or Markdown. |
| Export research outline with subtopics | An open research topic | An ordered Markdown or plain-text outline. |
| Import bibliographic metadata from RIS | A library with an already imported edition | Review a local RIS record and apply explicitly selected metadata fields. |
| Check catalog quality | An open library | Review missing metadata, unfiled editions and possible duplicate records. |

## 1. Compare sources and themes

Save verified search passages into a research topic. Add your interpretation in
**Evidence and Notes**, and use **Ctrl+Shift+T** to tag selected evidence with
themes such as `method`, `chronology` or `contradictory evidence`.

Open that topic, then press **Ctrl+Alt+M**. **Include subtopics** is initially
enabled. Choose a source version, then a saved evidence item to read its exact
quotation slices, citations, topic path, tags and separate research note. The
theme summary counts source versions and evidence items for each tag. A tag
filter matches one exact, case-sensitive name; **All themes** removes it.

Sources are grouped by edition and historical revision, never by title alone.
Two books with the same title remain separate. Quotations saved before a
metadata correction retain their earlier title, author and citation. A
multi-page quotation keeps its individual page slices and locations.

**Open selected evidence and notes** returns to the existing evidence editor.
Its unsaved-note protection remains active. **Refresh comparison** reads current
saved notes and tags again. Changing display filters uses the loaded snapshot;
every export independently rechecks the current authorized sources.

**Export comparison as CSV** writes one row per source version, with scoped
evidence IDs, topic paths, quotation slices, citations, notes and themes in
separate columns. Columns ending in `_json` contain reversible JSON values:
this preserves Unicode and line breaks and prevents formula-like source text
from executing in a spreadsheet. Grouped evidence arrays use the same order.
**Export comparison as Markdown** includes a comparison table and detailed
source sections. Quoted text is encoded in protected blocks.

## 2. Export an ordered research outline

Choose **Academic, Export research outline with subtopics**, or use the outline
button in the comparison dialog. Select Markdown or plain text, choose whether
to include subtopics, and optionally enter one exact tag name. A blank tag field
includes all saved evidence in the chosen topic scope.

The outline follows the existing topic order, including sibling reordering.
Section numbers preserve nested structure. Each evidence item includes its
historical citation and exact source slices; your research note and tags have
separate labels. Empty sections remain visible so the research structure is
clear. Archived topics and their descendants are excluded.

Text fields use reversible JSON strings, matching the existing research export
convention. This preserves quotation bytes represented as Unicode text without
letting source text create headings or other document markup. No summary,
argument or missing citation is generated.

Both comparison and outline exports obtain a fresh authorized snapshot. A
damaged original, withdrawn page review or cancelled operation stops the export.
Use the Save dialog to choose an export location outside the private library.

## 3. Apply reviewed RIS metadata to an imported edition

Select the target book in **Books and Collections**, then choose **Academic,
Import bibliographic metadata from RIS**. Open a local UTF-8 RIS file, select
the target imported edition and one unverified RIS record, then check only the
fields you intend to replace. All checkboxes start off; changing either record
or target clears those choices.

Supported fields are **title, ordered authors, language, edition and volume**.
Unchecked or missing fields keep their existing values exactly. Title and
author changes apply to all editions of the same work; edition-specific fields
apply to the selected edition. Publication is atomic, and a concurrent metadata
change requires a fresh review. Original files and previously saved citations
are preserved.

This command updates an already imported edition. It does not create a book or
a bibliography-only record. Year, journal, publisher, abstract, notes, keywords,
page ranges and other unsupported fields are not imported. DOI and ISBN-like
identifiers remain visible unverified hints; they are not stored or looked up.
URLs and attachment paths are never followed. Metadata import does not approve
source text for quotation.

RIS input is limited to 4 MiB and 5,000 records. Malformed, incomplete or
ambiguous multiline records reject the whole batch. In a library with more than
500 imported editions, select the target book before opening this command.

## 4. Review catalog quality

Choose **Academic, Check catalog quality**. The report covers the current library
or one selected active collection. It shows edition/work counts, distinct
catalog author names and languages, active collections, and registered assets.

Filter missing titles, missing authors, missing languages, editions without an
active collection, shared registered assets, or possible duplicate metadata.
Use **Previous editions** and **Next editions** to inspect the complete result.
**Edit selected edition metadata** opens the existing editor; run the check
again afterward to refresh its snapshot.

Possible duplicate metadata means matching normalized title, ordered authors
and language. Different editions, volumes and files remain separate; a match
does not establish that they are the same work. A shared registered asset hash
is a catalog fact, not a fresh verification of its retained file. Missing or
unknown authors remain unknown. The check never merges, deletes or rewrites
records automatically.

## Scope, limits and accessibility

All four workflows remain within the selected library. Long reads and exports
use **Cancel current operation**. Changing library/topic or closing the window
prevents a stale dialog completion from acting on the new scope.

Comparison and outline limits are explicit: 10,000 workspace topics, 1,000
selected topics, depth 32, 5,000 evidence items, 1,000 source revisions, 500
distinct tags, 100 tags per item, 16 MiB of stored evidence/notes/tag text and
64 MiB of exported text. A limit raises an error instead of silently dropping
evidence. Choose a smaller topic scope when needed. Catalog quality supports up
to 100,000 scoped editions and displays 200 affected editions per page; choose a
collection if a library-wide check exceeds its limit.

The controls use native keyboard navigation and named fields. Automated service,
handler and native probes have separate evidence. **Manual Windows/NVDA
acceptance of these academic workflows is NOT_RUN.** Automated passes do not
establish that a person using NVDA has completed these workflows successfully.

## Design context and reused implementation

The organization and review patterns were informed by these official guides:

- [Zotero: Collections and Tags](https://www.zotero.org/support/collections_and_tags)
  describes hierarchical organization and tags for detailed research grouping.
- [Zotero: Duplicate Detection](https://www.zotero.org/support/duplicate_detection)
  provides a comparison point for reviewing possible duplicates within one library.
- [JabRef: Check integrity](https://docs.jabref.org/finding-sorting-and-cleaning-entries/checkintegrity)
  provides a comparison point for a dedicated catalog-quality command.

These are design references, not connections to those applications or claims of
feature parity. RIS parsing reuses the already vendored MIT-licensed
[rispy implementation at commit b7aae3b](https://github.com/MrTango/rispy/tree/b7aae3b2069ced3fb75287711300f2edf0bcac21).
The original license and source provenance remain in
[RIS_REUSE.md](../src/carr_library/catalog/RIS_REUSE.md) and
[EXPORT_REUSE.md](../src/carr_library/research/EXPORT_REUSE.md).

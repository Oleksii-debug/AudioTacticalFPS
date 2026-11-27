# Ordered citations in the existing academic workbench

Open **Citation styles and bibliography**, choose the reference scope, author-name
convention, style and language, then choose **Edit citation order**. Formatting
uses the same installed, offline citeproc-js/QuickJS service and bundled styles
as the bibliography workbench. There is no additional Node process or processor.

## Editing and saving

Add a reference as a new citation, or add it to a selected group containing
several references. A reference can occur in several groups, with a different
locator each time. Each group keeps its own stable occurrence ID when moved.
Select an item to edit its locator/type, prefix, suffix or suppress-author
setting. Choose **Apply item and note edits** before formatting. Unapplied edits
disable formatting, copying and export.

Groups can be moved or removed. The same reference cannot appear twice within
one group. Blank note numbers follow the citation order; explicit note numbers
remain attached to their occurrences. **Number notes by citation order**
explicitly resets all note numbers to their current-order defaults.

**F6** formats the complete sequence. **Ctrl+Tab** changes sections. The editing
page scrolls with keyboard focus, including on a short display. Formatting and
Word generation run outside the native event handler. Closing signals the
original worker cancellation token; late results do not update the closed dialog.

Copy the reviewed citation sequence or save a new Word citation document.
Existing files are not overwritten. The Word snapshot retains reference
metadata, occurrence IDs, item order, locators and actual note positions through
the existing citation-document contract. Native Word endnotes require consecutive
note numbers starting at one. When reordering a loaded document, use the explicit
renumbering command if the retained note positions no longer match that order.

**Load citation order from a saved Word document** reads only its bounded inert
citation snapshot. Its normalized reference records must exactly match the current
selection. Visible text edited in Word is not imported. The original document is
kept unchanged; the existing regeneration workflow remains available separately.

## Source and recovery boundaries

Locators are manually entered presentation data. They do not approve a stored
quotation, prove a page number or change original source bytes. Publication page
extents never become citation locators automatically. Personal research notes are
excluded by the existing scoped CSL exporter.

The current workspace and exact reference metadata are rechecked before and
after formatting, before copying and through final export publication. Changed
metadata or revoked workspace access stops stale export. Selected destination
identity and no-overwrite publication use the existing research export service.

Drafts are temporary until saved in the Word snapshot. There is no second
citation database or new untyped library state. Existing formatting budgets
apply to a single preview (including up to 2,000 references and 4,000 groups),
not to the size of the stored library. The processor retains its memory, input,
output and CPU bounds.

## Reuse and evidence

The interaction follows the existing canonical citation-order workflow at
commit 4b6f8169986c4a58b43e4a23bc081fbefadbbdcb:
ui/citation_formatting.py, blob 189b71fd9f35c554d657bc026ab72e0c938e667c.
The adapter reuses the root's stricter existing citations/csl.py cluster API,
citations/documents.py, BibliographyWorkbench and scoped export publication.
Canonical Node/runtime/controller infrastructure is not duplicated.

The isolated workflow requires actual source, scoped database, local citeproc-js
and Word round-trip tests. Its Windows probe verifies installed source identity,
native controls and F6, short-display focus scrolling, real formatting and Word
publication, save/load provenance, and a close/late-result race. The temporary
destination is supplied by a synthetic chooser. It does not qualify the operating
system file-picker dialog, manual NVDA, the Word application or source quotations.

Source inclusion is not an execution verdict. Exact workflow results must be
recorded before this component is reported as passed.

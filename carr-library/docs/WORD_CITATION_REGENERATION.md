# Saved Word citation snapshots

The existing Citation styles and bibliography window can export an editable
Word document with citation occurrences, a bibliography and native Word
endnotes for a note style. The same window provides **Regenerate saved citation
document…** to create a new document from its embedded snapshot.

The original file is retained. This action reads the saved citation metadata,
paragraph text and complete occurrence order. It does not import visible edits
made in Word and does not automatically update Word fields. The confirmation
explains this before a file is opened. Choose a new output filename; regeneration
never replaces an existing document.

For a custom style, explicitly load the original CSL file using the existing
style-file action. The processor version, installed or custom style bytes,
citation IDs, note indices and document order must match the supported contract.
A modified or malformed snapshot is refused. A checksum detects accidental
corruption; it is not a signature or evidence that a quotation is correct.

The existing bibliography command also supports:

```text
research-library-bibliography regenerate --data-dir PRIVATE_LIBRARY --workspace LIBRARY_ID --input source.docx --output regenerated.docx
```

Add `--style-file original.csl` only for a document made with that custom
style. Both native and command-line actions use fresh access to the selected
library and the existing bounded export writer. They preserve the original
destination identity across queued work and stop when source bytes or access
change before publication.

Programmatic editing uses `citations.regeneration.regenerate_citation_docx`.
Supply the complete new citation sequence and matching `CitationParagraph`
objects when inserting, deleting or moving occurrences. Changed references
that retain locators require an explicit new citation request. That request
still does not certify the locator or convert bibliography metadata into
verified source evidence. Native Word endnotes require consecutive indices
starting at one; the service never silently renumbers a saved gap.

This implementation adapts the canonical PR111 regeneration algorithm to the
existing QuickJS CSL engine and `carr-citation-document/1` schema. It adds no
second formatting engine. Source tests exercise actual CSL/OOXML roundtrips,
note ordering, changed references, stale styles, source and destination races,
cancellation, revoked library access, native job wiring and the CLI. Execution
results must be read from the matching published commit's Windows evidence.

## Optional live Word controls

For explicit insert/edit and bibliography refresh in a new managed desktop Word
copy, see [Word desktop integration](WORD_DESKTOP_INTEGRATION.md). That optional
COM workflow retains this snapshot and its unverified-evidence contract. The
regeneration operation described above remains an independent new-file export;
it does not mutate a running Word document or import arbitrary visible edits.

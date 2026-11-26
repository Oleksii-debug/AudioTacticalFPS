# EPUB import and reading

The library imports the static XHTML text of EPUB 2.0 and EPUB 3 publications.
It follows the package's spine order rather than ZIP order, filenames, manifest
order or a generated table of contents. Each spine occurrence becomes one
logical section. Repeated references remain separate occurrences; supplementary
`linear="no"` sections are included and identified in the section chooser.

## Keyboard workflow

1. Open a library and choose **Library, Import books** (`Ctrl+I`). Select one or
   more `.epub` files alongside the existing TXT, PDF and DOCX formats.
2. Use the normal source search to find words, phrases or themes. **More** reads
   the matching complete section with an explicit unverified-extraction label.
3. To read from the beginning without first searching, choose **Library, Read
   EPUB book** (`Ctrl+Shift+R`), then choose the book.
4. Use **EPUB section** to jump directly, or **Previous section** and **Next
   section** to follow the reading order. Section information receives focus
   after navigation; Tab reaches the section text and separate image descriptions.
5. Press Escape or **Back** to return. The existing library, topic selection and
   unsaved research notes remain intact.

These controls use the existing background-job controller and native wx widgets.
Every section read checks the current library/topic authority and the retained
original's identity. Closing a dialog or changing the active library prevents
old callbacks from displaying another book. Automated control tests are separate
from manual Windows/NVDA acceptance.

The general **Reading, Read selected book** command (`Ctrl+R`) also opens imported
EPUB sections. Use that [reading desk](READING_DESK.md) for named bookmarks,
page/segment resume and navigation history. Both readers use the same retained
original and section order, including separate positions for repeated sections.
The EPUB-specific reader additionally presents chapter titles and authored image
descriptions. Its navigation does not update the reading desk's saved position.
Reading positions and bookmarks have their own export/import action; include
that export when moving a library backup to another computer.

## Text, locations and source preservation

Paragraphs, headings, list items, block quotes, line breaks and simple table
boundaries remain distinguishable in plain text. Inline markup does not split
words. Author-supplied image `alt` descriptions are retained separately and are
never inserted into quotation text or invented for an undescribed image.

The original EPUB is retained byte-for-byte and included in checked library
backup/restore. A revision records the original digest, extractor/parser version,
spine occurrence, manifest ID, resource path, title and supplementary status.
Catalog metadata changes keep the earlier source revision and the exact original
text available; they do not replace old source attribution.

The application displays logical section positions, not physical or printed
pages. It does not derive page numbers from chapter indexes, anchors, screen
layout or unverified page-list labels. EPUB text is searchable and readable but
remains **unverified**. The application's verified Copy quotation and Save
evidence commands reject it, as they already reject unverified DOCX extraction.
No source quotation approval is inferred from an import, a model score or a
successful XML parse.

## Supported subset and limits

The importer reads one rendition with local XHTML primary spine resources.
It preserves ordinary Unicode text and normalizes XML/HTML whitespace for a
readable text preview. It does not render CSS, run scripts, fetch external
resources or reproduce visual layout. Script/style/template bodies and native
HTML-hidden subtrees are omitted with reading notes; `hidden="until-found"`
content remains discoverable. CSS-dependent content remains a layout-unverified
preview instead of relying on an incomplete CSS visibility evaluator.

Unsupported primary SVG, embedded MathML, forms, frames and other content that
cannot be read faithfully by this adapter cause a clear rejection instead of a
partially published book. Image-only publications do not pass as complete text
imports. Encrypted primary text is unsupported. Recognized obfuscated unused
fonts can be ignored because this reader does not render them. No DRM is removed.

Limits include a 64 MiB original, the existing 128 MiB expanded ZIP budget,
1,000 spine occurrences, 8 MiB per XHTML member, 32 MiB total parsed XML,
bounded XML depth/node counts, 10 million extracted characters and 1 MiB of
section/alternative metadata. ZIP members are read in memory, never extracted
to paths supplied by the document. Duplicate/aliased members, unsafe paths,
external entities, external DTDs, internal entity declarations and traversal
outside the archive are rejected. An ordinary XHTML `<!DOCTYPE html>` is allowed.

Import runs in the existing bounded worker. Publication reuses the current
atomic catalog/revision transaction, original retention and non-destructive
backup machinery. A lost worker reply can recover an already published exact
revision without duplicate pages or catalog records. No schema migration or
network service is introduced.

## Reuse and source references

The adapter uses the existing `catalog.office_archive` checker, Python `zipfile`
and the pinned `defusedxml==0.7.1` parser. The parser was already used for catalog
metadata; it is now a required, hash-locked dependency in both offline Windows
bundles. Its upstream release/license and wheel verification are recorded in
[`packaging/EPUB_XML_RUNTIME.md`](../packaging/EPUB_XML_RUNTIME.md).

Primary specifications consulted on 2026-11-26:

- [W3C EPUB 3.3](https://www.w3.org/TR/epub-33/): package, manifest, spine,
  container-relative resources and XHTML content.
- [W3C EPUB Reading Systems 3.3](https://www.w3.org/TR/epub-rs-33/): reading
  order and supplementary content. This static adapter is not a claim of full
  EPUB reading-system conformance.
- [W3C EPUB Accessibility Techniques 1.2](https://www.w3.org/TR/epub-a11y-tech-12/):
  authored descriptions and the distinction between logical navigation and
  genuine print-page navigation.
- [defusedxml upstream](https://github.com/tiran/defusedxml): direct bounded
  XML parsing and entity/external-resource defenses.

## Reproduction

From `carr-library/`, with the project/test dependencies installed:

```console
python scripts/ci_check.py tests/extract/test_epub_ordered.py tests/e2e/test_epub_import.py tests/reader/test_ebook_reader.py tests/ui/test_ebook_reader.py
```

The Windows workflow also runs the EPUB import and reader tests against the
installed offline bundle, with runtime egress blocked. Public tests create
synthetic books in temporary directories; no user documents are published.

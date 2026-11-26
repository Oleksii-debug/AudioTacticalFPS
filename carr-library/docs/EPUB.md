# EPUB reading for academic libraries

## Reading and navigation

Import a locally owned EPUB with **Ctrl+I**. Select its catalog row in **Books
and Collections** and press **Ctrl+Shift+O**. Choose a chapter, use Previous or
Next, or select a destination in the table of contents. Section headings without
a destination remain visible and do not jump to another chapter. **Ctrl+F**
focuses Find; **F3** repeats a literal search within the current chapter.

Every chapter is an unpaginated logical segment. The reader follows the package
spine, including `linear="no"` supplementary chapters. XHTML headings, EPUB 3
navigation and EPUB 2 NCX destinations refer to offsets in the extracted text.
It never infers printed pages from chapter order or page-list labels. Empty
image covers remain in reading order with an explicit warning.

The extraction report distinguishes readable text from images, scripts, styles
and media that have not been rendered or transcribed. Image alternatives are
separate unverified author-supplied descriptions, not quotations or OCR.
Embedded title, creator, language, identifier and publisher fields are metadata
hints and do not silently replace your catalog information.

Native controls have names and visible labels. Keyboard navigation preserves
the main window's unsaved research notes. A changed book, library, topic or
revision invalidates the reader; failed loads clear previous text. Automated
native controls do not establish manual NVDA acceptance.

## Source integrity and storage

The isolated import worker reads the bounded original archive. Text nodes and
inline order are preserved; block/line boundaries become line breaks. XML
parsing normalizes XML line endings and entities. No CSS engine, script
execution, page reconstruction or paraphrasing is involved. Extracted text
therefore remains explicitly unverified.

Original bytes are retained and authenticated with SHA-256. Text digests and
bounded navigation descriptors are bound to the sealed extraction revision.
Each reader operation checks the current library/topic, active revision,
retained source and selected chapter. Catalog metadata edits retain the same
extraction while invalidating a reader opened before that edit. Search previews
work; verified quotation copying and evidence saving remain unavailable for
unverified EPUB text. Reading does not approve it.

Full-library backup includes the original EPUB, text and navigation. Restored
books reopen without reparsing. An external reader receives a separate viewing
copy so editor changes cannot alter the retained original. No database schema
or network metadata service is added.

## Supported path and limits

Supported books have one unambiguous EPUB 2/3 package, local reflowable XHTML
spine documents and consistent internal links. EPUB 3 grouping TOC and EPUB 2
NCX are supported. Optional font obfuscation does not itself block text import.
Text/navigation encryption is checked per member.

Malformed ZIP/XML, DTD-dependent XHTML, external or escaping navigation,
duplicate/missing reading-order identities, unresolved contents anchors,
`xml:base` or XHTML `base` overrides, fixed-layout books, unsupported embedded
MathML/SVG/ruby/canvas/object content and encrypted text are explicitly rejected.
Wholly image-only books need a separate OCR path. One failed chapter prevents
publication of the whole import.

The shared archive guard limits sources to 64 MiB, expanded archives to 128 MiB
and entries to 10,000. Additional chapter XML, element, character, navigation,
label and descriptor budgets bound parsing and display work.

## Runtime and validation

XML parsing uses **lxml 6.1.3**, already shipped in both Windows runtime locks
through python-docx and now declared directly. Entity resolution, DTD loading,
network access, recovery and huge-tree mode are disabled; document type
declarations are rejected. The older metadata preview no longer needs a
test-only defusedxml installation. No new wheel, executable or downloader is
introduced.

Required tests cover archive/XML/link rejection, source hashes, Unicode/inline
order, supplementary and empty chapters, TOC/NCX/group/page-hint navigation,
isolated import, scope/cancellation, atomic replay, catalog edits,
search/quotation boundaries, original viewing copies and backup/restore.
Native wx probes exercise controls, events and Unicode search positions.
Windows and manual NVDA evidence remain separately recorded.

## Primary references

- [W3C EPUB 3.3](https://www.w3.org/TR/epub-33/): package spine, XHTML,
  navigation, resources and internal link semantics.
- [W3C EPUB Reading Systems 3.3](https://www.w3.org/TR/epub-rs-33/): reading
  order and navigation presentation.
- [Zotero 7](https://www.zotero.org/blog/zotero-7/): integrated EPUB research
  reading and accessibility.
- [calibre E-book viewer](https://manual.calibre-ebook.com/viewer.html):
  contents and references in reflowable books.
- [lxml parsing documentation](https://lxml.de/parsing.html): parser options
  and document type information.

These references informed behavior; no Zotero or calibre application code was
copied. Existing dependency licenses remain in their distributions.

# EPUB spine extraction

The adapter reuses the existing `catalog.office_archive.validate_office` screen,
CPython `zipfile`, and the already selected `defusedxml==0.7.1` parser. No new ZIP,
XML or rendering engine is implemented, and no upstream application source is
copied. The EPUB integration makes defusedxml a pinned runtime dependency.

## Reading and source identity

The W3C EPUB reading-system specification requires spine order and distinct
occurrences when a manifest item is referenced repeatedly. Supplementary
`linear="no"` items are included and labelled. Container rootfile paths are
relative to the archive root; manifest paths are relative to the package file.
Percent-encoded UTF-8 paths are decoded once and resolved inside the archive.

- https://www.w3.org/TR/epub-rs-33/#sec-spine
- https://www.w3.org/TR/epub-33/#sec-package-spine
- https://www.w3.org/TR/epub-33/#sec-xhtml
- https://www.w3.org/TR/epub-33/#sec-ocf-metainf-encryption.xml
- https://github.com/tiran/defusedxml/tree/v0.7.1

Each supported XHTML spine occurrence becomes one logical segment. The result
contains the retained asset digest, text digest, normalized resource path,
manifest ID, spine position, supplementary flag and a bounded display title.
It does not assert physical or printed pages. Metadata remains an unverified
hint, and extraction never changes author/title attribution automatically.

## Honest preview boundaries

Text is a static XML-derived preview, not a browser rendering or a verified
quotation. Inline text and tails remain in order; paragraphs, headings, lists,
table cells and explicit line breaks have readable boundaries. Normal HTML
whitespace is collapsed and preformatted text is retained. Table layout and CSS
are not rendered. CSS-dependent text stays in the preview because an incomplete
cascade approximation could discard visible book content.

Script, style and template content and native HTML `hidden` subtrees are omitted
with warnings. `hidden="until-found"` content remains discoverable. Image alt text
is source-authored but is returned in a separate `alternatives` field; it is never
inserted into the quotation text. Empty decorative alternatives are distinguished
from missing alternatives. Unsupported MathML, SVG primary content, forms,
embedded objects, frames and ruby structures reject the import explicitly.
There is no silent fallback to an incomplete sentence.

The adapter does not load links, stylesheets, fonts, images, remote resources or
scripts. An ordinary XHTML5 `<!DOCTYPE html>` is accepted; external/public DTDs,
internal DTD subsets, entities and xml:base are rejected. ZIP/XML sizes, node
counts, depth, source text and section metadata are bounded. The 1000-section
limit matches the chapter reader. Repeated spine references consume the output
budget even though their parsed XHTML may be cached.

Encryption declarations are examined without decrypting any resource. Required
content cannot be encrypted. Known IDPF/Adobe obfuscation for unused,
manifest-identified fonts can be retained in the original archive without
rendering it; unknown algorithms or targets reject the import.

## Verification

Synthetic extractor regressions live in `tests/extract/test_epub_ordered.py`.
They cover reversed ZIP/manifest order, repeated and supplementary references,
Unicode and safe relative paths, inline text, tables and preformatted text,
ordinary DOCTYPE, honest image alternatives, hidden-until-found, CSS cascade
counterexamples, malformed/encrypted/unsupported sources and resource limits.

Run from `carr-library/`:

```text
python scripts/ci_check.py tests/extract/test_epub_ordered.py
```

Public application/worker, backup/restore and chapter-reader integration have
separate regression suites. An extractor pass alone does not establish Windows
installation, native screen-reader operation or general EPUB-format coverage.

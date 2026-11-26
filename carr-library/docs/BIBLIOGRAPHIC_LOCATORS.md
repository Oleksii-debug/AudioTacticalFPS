# Source locators in bibliographic exports

Saved-evidence BibTeX and RIS exports retain the location of each source slice
in a generated `Source locator` note: BibTeX `note` or RIS `N1`. A multipage
selection produces one reference with every slice listed in its original
order. Printed labels are enumerated exactly, without inferring a page range.

The export distinguishes the physical PDF page from its verified printed
label. An unverified or absent printed label appears as `printed page unknown`;
its candidate text is omitted. Unpaginated material retains its logical segment
number and the word `unpaginated`.

Pinpoint locations do not establish the page extent of a whole publication.
These saved-evidence exports therefore do not populate BibTeX `pages` or RIS
`SP`/`EP` from selected quotation pages. They contain no source quotations or
private research notes. The generated source-locator note is separate from the
user's research note. Historical source, scope, integrity, and approval checks
still run before formatting any output.

This is bibliographic interchange, with the existing generic reference types.
It does not add a citation-style processor or claim APA/Chicago formatting.
The operation is local and requires no metadata lookup or network connection.

## Primary references

- [CSL 1.0.2: Number Variables](https://docs.citationstyles.org/en/stable/specification.html#number-variables)
  distinguishes a cite-specific `locator` from `page`, the page range occupied
  by the item within its containing publication.
- [Zotero: Item Types and Fields](https://www.zotero.org/support/kb/item_types_and_fields)
  defines bibliographic Pages as the page range of an item in a larger
  publication, separate from the total number of pages of a book.
- [Zotero: Page and Other Locators](https://www.zotero.org/support/word_processor_plugin_usage#page_and_other_locators)
  describes the per-citation locator used to identify a particular page,
  page range, or other part of an item.

## Focused verification

`tests/reader/test_bibliographic_locators.py` checks both formats, the distinction
between pinpoint and publication pages, the complete ordered multipage sequence,
unknown printed labels, logical segments, and exclusion of quotations and
private notes. RIS output is read back using the existing vendored RIS parser.

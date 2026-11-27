# Additional document extractors: reuse and provenance

This note records the parser reuse for the V3 document-format increment. It is
an implementation record, not complete V3, native Windows, or NVDA acceptance.
The supported subsets and user-facing limitations are maintained in
`DOCUMENT_FORMATS_V3.md` and the shared document capability registry.

## Existing Python runtime

The Windows package continues to target **CPython 3.11.18**, as recorded in
`packaging/windows-python.json`; this increment does not replace the runtime.
The additional text adapters import Python's existing `html.parser`,
`email.feedparser`, `email.message`, `codecs`, `base64` and `quopri` modules.
Markdown uses strict text decoding and deterministic character slices, with no
new markup renderer. Existing container screening continues to use `zipfile`.
No Python standard-library source was copied into the application package.

Primary interfaces reviewed:

- [HTMLParser, Python 3.11](https://docs.python.org/3.11/library/html.parser.html):
  incremental callbacks, entity handling and the absence of automatic HTML tree
  validation. The application adds explicit nesting, token and resource bounds.
- [Email parsers, Python 3.11](https://docs.python.org/3.11/library/email.parser.html):
  `BytesFeedParser`, an explicit policy and the bounded message factory.
- [EmailMessage, Python 3.11](https://docs.python.org/3.11/library/email.message.html):
  MIME payloads, content types, dispositions and ordered multipart traversal.
- [Email defects, Python 3.11](https://docs.python.org/3.11/library/email.errors.html):
  malformed multipart boundaries and transfer/header defects.
- [Python history and license](https://docs.python.org/3.11/license.html):
  Python's own license and incorporated-component notices remain part of the
  existing runtime distribution. This note does not replace those notices.

The local text-adapter verification used **Python 3.12.14**, not the Windows
runtime. The strict source suite executed **72 tests**, with no failure or skip,
under the existing kernel network-denial runner. Native Windows verification
must exercise the integrated package separately.

## Existing XML dependency

The XML/ZIP adapters reuse the already required **defusedxml 0.7.1** dependency.
It remains pinned in `pyproject.toml` and both Windows locks. The adapters use
`DefusedXMLParser` with DTDs, entities and external references forbidden; they
do not use global monkeypatching. Container, expanded-byte, XML node/depth,
logical-record and output-character limits are application responsibilities.
There is no new parser dependency in this increment.

The existing reviewed package provenance is recorded in
`packaging/EPUB_XML_RUNTIME.md`:

- [Exact upstream source](https://github.com/tiran/defusedxml/tree/ebff1b493751e2f0775314bdd4188d64f07ea184),
  revision `ebff1b493751e2f0775314bdd4188d64f07ea184`, for release 0.7.1.
- [Exact upstream license](https://github.com/tiran/defusedxml/blob/ebff1b493751e2f0775314bdd4188d64f07ea184/LICENSE),
  Python Software Foundation License version 2. The installed 0.7.1 license was
  read again during this increment; SHA-256
  `b80ce9da8c42a1f91079627fbbe2bf27210ae108a0ffe5f077d5b08e076c24c8`
  matches the existing provenance record.
- The unchanged wheel's recorded publisher SHA-256 is
  `a352e7e428770286cc899e2542b6cdaedb2b4953ff269a210103ec58f6198a61`.
  This increment reuses that prior verification; it does not claim a new wheel
  download or a new full-runtime hash audit.

## Text fidelity boundaries

HTML extraction is an unverified static text preview, not browser rendering.
External stylesheets, CSS selectors, generated CSS content and layout are not
evaluated. Consequently a class hidden by a stylesheet can still contribute
text. The result always includes `html_styles_not_applied` and
`layout_not_verified`; scripts, known hidden attributes and supported explicit
inline hiding are omitted with additional warnings. Browser-dependent hidden
text cannot be treated as authenticated visible source. Unsupported ambiguous
implicit element transitions are rejected. Invalid numeric references are
rejected instead of silently dropping text or introducing replacement glyphs.

Email extraction chooses supported message bodies and discloses excluded
attachments/alternatives. It does not authenticate senders or signatures and
does not include headers in quoted body text. MIME decoding preserves the
selected body's Unicode and line endings without replacement decoding.

All new formats retain the original-byte checksum, use logical locations, and
remain `review_required`. Neither parsed text nor metadata supplies verified
printed pages or upgrades existing source evidence. Text segments are bounded
for Reading Desk, and slicing preserves the original extracted characters.

## Review references for spreadsheet boundaries

The [Microsoft CellFormula documentation](https://learn.microsoft.com/en-us/dotnet/api/documentformat.openxml.spreadsheet.cellformula?view=openxml-3.0.1)
describes formula ranges and array formulas. A cached dependent cell must not be
misclassified as independent literal input merely because it lacks its own
formula element.

The [OpenDocument 1.3 schema specification](https://docs.oasis-open.org/office/OpenDocument/v1.3/OpenDocument-v1.3-part3-schema.html)
defines matrix spans and group visibility. These require explicit range/group
handling or rejection, not silent inclusion of hidden or formula-derived cells.
The parser's supported subset is narrower than either complete office format.

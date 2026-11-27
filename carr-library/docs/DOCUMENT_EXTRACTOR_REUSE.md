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

The original upstream text-adapter report recorded **Python 3.12.14** and
**72 tests** under its network-denial runner. Those historical results are not
verification of this integrated checkout. Native Windows verification must
exercise the integrated package separately.

## Selective integration review

The source of this format increment is repository PR 104 at exact commit
`7180db0e8622ce45a321199f1c341ebd7e5ccc4e`. Only the document readers, shared
format registry, existing-pipeline hooks, synthetic tests and format guides
are reused. This does not import a second audio studio, conversion service,
backup implementation, search stack, keyboard framework or progress system.
The unrelated PR 103 conversion service is not enabled or packaged.

The integration review retained the portable strict MIME base64-padding fix,
the original-byte replay guards, cumulative XML counters, hidden/formula
omission rules and the existing prohibition on approving extracted text as
quotation evidence. It additionally found and closed an ODS allocation gap:
the old reader could retain independently expanded paragraphs for an entire
row before charging their cumulative text budget. Pending row text and all
repeats now reserve the remaining document budget first. Shared paragraph
collectors check before whitespace expansion and joins; they avoid recursively
copying complete nested paragraphs. Remaining XML-byte admission occurs before
reading another archive member.

The regression proof uses only a 10,000-space request and a small 64-cell
synthetic row. It checks bounded peak allocation without a giant input, an
out-of-memory condition or a benchmark. Separate tests preserve empty-paragraph
source coordinates, exact Unicode budget boundaries and cumulative XML limits.

### Integration verification, 2026-11-27

The reviewed checkout passed **652 tests and 2 subtests** on **Linux with
CPython 3.12.14**. This covered all new format parsers and actual isolated
application imports, plus the existing ingest, original-retention, replay,
backup/restore, DOCX/EPUB, Reading Desk, UI protocol and audio/DOI CLI checks.
The first broad invocation exposed five existing lock-subprocess checks whose
child interpreters could not import an uninstalled checkout. Re-running the
same gate with the checkout's explicit `PYTHONPATH` passed every test; no
dependency installation or product change was needed for that harness issue.

The allocation regression was also executed against the exact upstream module
and the reviewed module, with each XML tree constructed before tracing. Both
versions rejected the inputs, but only the reviewed version admitted pending
text before allocating it:

| Small synthetic input | Exact PR 104 peak traced allocation | Reviewed peak traced allocation |
| --- | ---: | ---: |
| One 10,000-space request after the one-character allowance was used | 13,085 bytes | 3,842 bytes |
| 64 cells, each with one character and 10,000 spaces; a 20,002-character document allowance | 659,965 bytes | 51,250 bytes |

These figures demonstrate the specific allocation-order regression. They are
not throughput measurements, a total-process memory claim, or a benchmark.
The checked-in tests use conservative bounds instead of depending on these
exact allocator values. They also preserve the original paragraph coordinates
when empty paragraphs are omitted from spreadsheet reading text.

The complete local gate, from `carr-library` in a source checkout, was:

```sh
PYTHONPATH=src:. python -m pytest -q \
  tests/extract/test_document_formats_registry.py \
  tests/extract/test_document_formats_text.py \
  tests/extract/test_document_formats_xml.py \
  tests/e2e/test_extended_document_workflows.py tests/ingest \
  tests/catalog/test_intake.py tests/catalog/test_office_intake.py \
  tests/extract/test_docx_ordered.py tests/extract/test_docx_hidden_styles.py \
  tests/extract/test_epub_ordered.py tests/reader/test_reading_desk.py \
  tests/reader/test_ebook_reader.py tests/storage/test_library_backup.py \
  tests/storage/test_backup_schema_identity.py \
  tests/storage/test_backup_source_binding_review.py \
  tests/ui/test_desktop_handlers.py tests/ui/test_import_center.py \
  tests/e2e/test_audio_cli.py tests/acquisition/test_acquisition_cli.py
```

Native Windows and manual NVDA acceptance were **not run** for this integrated
checkout. The installed native document probe and final combined Windows gate
must run on the exact release commit; upstream passes do not replace them.

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

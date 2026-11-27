# Document import, conversion and supported formats

Library import retains a document as a source in the catalog. Conversion
creates a separate reading copy. The two routes have distinct source and
format guarantees; the tables below identify the supported behavior of each.

## Library import and retained originals

The existing import worker now accepts ten additional document families. The
catalog, retained originals, import queue, request/attempt publication receipts,
scoped search, Reading Desk and backup/restore remain the same services.

Choose **Library → Import Center** to plan files or a folder, review the queue
and exclusions, then start it. The direct file chooser uses the same format
registry. **Help → Document formats**, or `python -m carr_library --document-formats`,
lists the supported formats and limitations without opening a library or native
runtime. Use **Reading Desk (Ctrl+R)** to read an imported document.

| Family | Extensions | Reading preview |
| --- | --- | --- |
| HTML | `.html`, `.htm`, `.xhtml` | Static text; no scripts or remote resources. Stylesheets and generated content are not evaluated. |
| Markdown | `.md`, `.markdown` | Literal text, including markup. Embedded HTML and links are not executed. |
| Email | `.eml` | Selected readable MIME body; attachments and remote resources are omitted. Headers are not authenticated. |
| FictionBook | `.fb2`, `.fb3` | Supported ordered body text/headings; no visual layout or image transcription. |
| OpenDocument text | `.odt` | Supported ordered body content; active, ambiguous or unsupported structures are refused. |
| OpenDocument presentation | `.odp` | Stored presentation text in document order; no playback. |
| OpenDocument spreadsheet | `.ods` | Stored literal cell text with bounded logical coordinates; no formula calculation. |
| PowerPoint | `.pptx` | Stored slide and shape order; no presentation playback, master slides or speaker-note transcription. |
| Excel | `.xlsx` | Literal stored cell text in workbook order; no macros or formula calculation. |

FictionBook 2 and 3 are separate parser families; the table groups them for
readability. The existing TXT, PDF, DOCX and EPUB paths retain their behavior.
Spreadsheet formula/hidden-cell omissions and other parser limitations are
reported in the Import Center result details, including after a recovered
post-commit worker stop.

### Original bytes and evidence

Every additional format retains the exact original bytes under its digest.
Extension aliases are stored using the canonical extension (`.html` or `.md`).
Extracted text is an **unverified logical segment**, with no invented printed
page label. Search and reading do not approve quotations or saved evidence.
Scripts, linked resources, embedded objects and macros are never run. These
additional original formats are not launched through the default external
viewer because that viewer could execute active content or fetch resources.
The retained source and authorized byte-exact private copy are preserved.

The parsers use the Python standard library and the already-required
`defusedxml` dependency. No converter, model or runtime dependency is downloaded.
The source ceiling remains 64 MiB; archive expansion, XML depth/node count,
MIME structure, extracted text and logical locations have additional limits.
There may be at most 10,000 logical records, 10 million extracted characters,
2 million characters per reader segment, and 4 MiB of publication configuration.
A long paragraph can be split without changing its characters. A document
whose location metadata exceeds the configuration bound is refused before any
catalog, revision or search publication, even when its compressed file is small.

### Stop, resume and backup

The existing request plus attempt receipt commits atomically with a new source.
A worker stopped before that commit does not publish partial searchable text.
A worker stopped afterward can return the verified durable outcome, with the
extracted text still unverified. Another request's equal revision cannot
complete the stopped request.

An exact replay can add a collection membership after validating current
metadata. If a metadata edit superseded the original revision, replay reports
that revision as superseded and does not reactivate it. If interrupted after a
replay membership commit, the first publisher's immutable receipt cannot prove
which later replay finished: the request stays stopped until an explicit
resume verifies it without duplicate membership or source rows.

Completed and pending import backups include the bounded additional source
formats. A restored queue can continue after the external originals have been
removed. Changed planned sources, changed retained bytes and unapproved scope
changes are refused through the existing source boundary.

### Unsupported inputs and qualification

Encrypted/password-protected documents are not decrypted and DRM is not
bypassed. Legacy DOC/PPT/XLS, RTF, MOBI/AZW/AZW3, CHM, DjVu, LIT, PDB/PRC, TCR,
WPD and standalone image/TIFF imports are not added by this closure. An explicit
conversion of a copy using a separately installed tool produces a new source;
it does not establish original pagination or extraction fidelity.

The three extractor modules are reused from canonical commit
`966e5ccf892243ead6942c635af5fbab47e72551`. Their integration preserves this
candidate's receipt verifier, source authority, queue and backup schema. Tests
use original synthetic documents, real isolated imports and actual SQLite
commit/termination boundaries. The native Import Center probe retains its TXT
quotation/control checks and adds HTML/XLSX import, omission details and reader
boundaries. Its final installed Windows execution and human NVDA qualification
must be reported separately; portable test success does not qualify them.

## Conversion into a reading copy

**Convert documents** produces a new UTF-8 reading copy and a separate
`.provenance.json` receipt. It leaves the original file unchanged. Converted
text is always a derivative and is never automatically registered as a verified
original, approved quotation, or confirmed printed page.

The receipt records the source filename, byte count and SHA256, output SHA256,
reader, logical sections, actual PDF page indexes where applicable, warnings
and execution duration. A successful conversion means these files were actually
created and verified. It does not establish perfect layout, OCR or quotation
accuracy.

### Format support in this implementation

| Format | Enabled reader | Important limits |
| --- | --- | --- |
| TXT | Strict built-in decoder | UTF-8/BOM and UTF-16/BOM by default; explicit supported legacy encodings; invalid bytes stop rather than becoming replacement characters |
| MD / Markdown | Built-in text or optional sandboxed Pandoc | Built-in retains markup; Pandoc makes plain reading text |
| HTML / HTM / XHTML | Isolated standard-library HTML parser | No scripts/styles/frames/active objects; no network requests; image alternatives labeled separately; CSS/visual visibility is not reconstructed |
| EML | Isolated standard-library email parser | One plain/HTML body alternative; attachments are never opened or executed |
| DOCX | Existing reviewed python-docx adapter | Original body order; unsupported hidden styles, tracked changes, complex tables and other unsupported structures require review; no fabricated pagination |
| EPUB | Existing reviewed spine adapter | ZIP and XML limits; spine order; no DRM/password bypass; logical sections, not printed pages |
| PDF | Isolated pypdf native-text reader | Real physical indexes retained; native text remains unverified; scanned pages require the existing library OCR workflow |
| FB2 | Bounded FictionBook XML body reader | Body paragraphs/verses; no images or layout reproduction |
| FB3 | Bounded package/declared-body XML reader | One `application/fb3-body+xml` part; namespace validation; no pagination reconstruction |
| ODT | Bounded OpenDocument XML reader | Logical paragraphs/headings and explicit whitespace; encrypted documents and tracked changes rejected |
| ODS | Bounded OpenDocument sheet reader | Stored text/values; repeated empty padding is not expanded into millions of cells; no formula evaluation or layout reproduction |
| ODP | Bounded OpenDocument presentation reader | Text in stored page/paragraph order; no animations, embedded objects or visual-order claim |
| XLSX | Bounded OOXML sheet reader | Workbook relationship order, shared/inline strings and stored values; formulas never run; hidden sheets/rows excluded; number/date formatting and charts are not recreated |
| PPTX | Bounded OOXML presentation reader | Actual presentation relationship order; hidden slides excluded; text tree order requires visual/accessible source review |
| RTF | Optional installed Pandoc with `--sandbox` | Plain-text extraction only; no Lua/custom filters, PDF engine, templates or resource embedding |
| PNG / JPEG / BMP / TIFF / WebP | Optional installed Tesseract | Explicit installed language selection; pixel/frame budgets; unverified text-only OCR derivative |
| AZW / AZW3 / CHM / DjVu / DOC / LIT / MOBI / PDB / PRC / PPT / TCR / WPD / XLS | Not enabled in this reader set | A separate converter with tested offline and active-content isolation is still required. Availability of a converter executable alone does not establish safe support. |

The supported-format matrix is available through
`ConversionService.capabilities()`. Optional reader availability depends on the
actual installation. These entries describe specific enabled behaviors, not
universal support for every variant of a file extension. Error messages name
unsupported, encrypted, malformed or over-budget input instead of claiming a
conversion completed.

### Local service API

```python
from carr_library.conversion import ConversionService

converter = ConversionService(chosen_output_folder,
                              input_roots=(chosen_input_folder,))
result = converter.extract(source_file, 'reading.txt', cancelled=cancelled)
# result.path: actual new UTF-8 reading copy
# result.report: provenance and fidelity warnings
# result.report_path: actual new provenance JSON file
```

Optional keyword arguments are `adapter='auto'|'builtin'|'pandoc'`, explicit
`encoding` for text files, and `ocr_languages=('ukr', 'eng')` for images.
Language/model installation is a separate explicit action. `extract_many`
supports a finite sequential batch with checksum-based filenames; completed
outputs remain available if a later item fails or the batch is cancelled.
It does not claim a persistent cross-restart conversion queue.

### Input and process boundaries

Only regular local files inside the selected input roots are accepted. The
output must be a new `.txt` file inside the selected output root. Existing text
or provenance files are never overwritten. Links, reparse points, network
paths and traversal are rejected by the common local-path boundary.

Before extraction, the service streams the original into a private snapshot
and records its hash and identity. It checks the original again before output
publication. The text and receipt are staged, flushed and published without
replacement; the receipt is the completion marker. A failed receipt publication
removes the just-created text rather than reporting partial success.

Built-in parsers run in a separate Python process with a fixed entry point,
bounded source/output/log sizes and timeout. POSIX workers also apply address
space, file-size and CPU limits. The Windows implementation uses bounded
document structures/output sizes and process cancellation; it does not claim
an operating-system whole-process memory sandbox. Python audit rules deny
network connections, child commands and writes outside staging in these
built-in workers. This is defense in depth for trusted installed parsers, not
an OS sandbox against a compromised native library.

ZIP members are never extracted to arbitrary filesystem paths. Readers reject
unsafe/aliased paths, links, encrypted members, unsupported compression,
oversized expansion and embedded executable/script payloads. XML readers deny
DTDs, entities and external references, and bound depth, nodes and text. Office
formulas and links remain inert data. Image metadata is inspected in the isolated
worker, with per-frame and aggregate pixel limits checked across every frame.
Tesseract receives this validated local image snapshot, explicit language IDs
and fixed options; no text file list or user command arguments are accepted.

Optional Pandoc uses explicit readers and the plain-text writer with its
documented `--sandbox` boundary. No custom filters or PDF engines are enabled.
The Haskell heap and execution time are bounded. The real test uses both an
HTTP iframe and a local-file iframe and verifies neither resource is read.

### Reuse and external candidates

DOCX/EPUB reuse the existing project adapters, including their reviewed
dependency identities and source-integrity limitations. The vendored Cairn
HTML/XLSX readers were inspected. They are not called here because the HTML
reader replaces decoding errors, and the spreadsheet reader depends on
openpyxl, drops empty cell positions and labels sheet count as page count.
The new derivative readers preserve explicit limitations and do not inherit
those page/decoding claims. No third-party parser implementation was copied.

Official references inspected on 2026-11-26:

- [Pandoc input formats and sandbox security](https://pandoc.org/MANUAL.html)
- [Tesseract command-line and multilingual selection](https://tesseract-ocr.github.io/tessdoc/Command-Line-Usage.html)
- [FictionBook 3 publisher schemas](https://github.com/gribuser/FB3)
- [calibre ebook-convert](https://manual.calibre-ebook.com/generated/en/ebook-convert.html)
- [LibreOffice conversion filter table](https://help.libreoffice.org/latest/en-US/text/shared/guide/convertfilters.html)

calibre and LibreOffice are candidate optional legacy-format adapters. They are
not invoked automatically: headless operation alone does not establish that
macros, external resources and document-controlled behavior cannot execute or
access the network. No converter binary, model or proprietary format asset is
redistributed by this source change. Pandoc's own license applies to a separately
installed executable; the application does not copy its implementation.

### Verification

Tests exercise actual isolated extraction of synthetic Unicode TXT, HTML, EML,
DOCX, PDF, EPUB, FB2, FB3, ODT, ODS, ODP, XLSX and PPTX. The checked-in PPTX is a
small complete synthetic presentation produced with python-pptx; its declared
slide order intentionally differs from archive filename order. No private
document is used. Tests also cover malformed archives/entities, unauthorized
paths/links, budgets, original-file changes, cancellation, no overwrite and
receipt-publication failure.

When installed, actual Pandoc RTF and HTML sandbox tests and actual English
Tesseract OCR run. On hosts without these optional engines, the same tests
verify explicit unavailable behavior and do not claim real conversion evidence
for that host. Native Windows/NVDA, uncommon format variants and corpus-level
fidelity require their own acceptance evidence.

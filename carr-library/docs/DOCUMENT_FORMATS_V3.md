# Additional document formats: reading, search and source integrity

This component implements the additional document-import portion of
[V3-01](UNIVERSAL_SCOPE_V3_AUDIO_AGENT.md). It extends the existing application,
Import Center, isolated import worker, original-byte store and research reader.
It does not create a separate converter or automatically approve quotations.

## Working with a document

Use **Library → Import books** or **Ctrl+I** to select files for direct import.
For folder import and a reviewable persistent queue, use **Library → Import
Center: files, folders and queues**, choose files or a folder, inspect the plan
and run it. Folder plans retain their original
file identities; a file that changes after planning must be planned again.
Interrupted queues can be reopened and continued. Successfully published
sources are not silently imported a second time during queue recovery.

After import, select the book and use **Ctrl+R** to read its extracted text, or
search normally. Locations identify **logical segments** in extraction order.
They are not physical pages, printed pages, spreadsheet coordinates presented
as pages, or an assertion that the original layout has been reproduced.

The original bytes are retained unchanged with their SHA-256 identity. Catalog
title, ordered authors, language and edition supplied during import remain
authoritative; an embedded title or mail header does not silently replace
them. A normal full-library backup contains the registered original files,
catalog metadata and extracted revisions. Restoring to a new data folder keeps
these same identities and texts.

Imported text from the additional formats is marked **unverified**. Reading,
searching and backing it up do not authorize **Copy verified quote** or saved
evidence. An OCR or text-conversion tool agreeing with another tool would not
provide independent source approval either. The existing evidence rules apply.

## Compatibility and fidelity

The following describes the implemented text-reading subsets. Unsupported
structures are rejected or explicitly reported; no format is a promise to
reproduce every visual, interactive or proprietary feature of its authoring
application.

| Input | Reading representation | Fidelity boundary |
| --- | --- | --- |
| HTML / HTM / XHTML | Static textual blocks and decoded character references | No JavaScript, stylesheets, browser rendering, remote images or external resource retrieval. Script/style, active content and recognized explicit hidden states are excluded. Stylesheet visibility and generated content are not evaluated; text hidden only by a stylesheet may appear. |
| Markdown / MARKDOWN | Original decoded text with Markdown syntax retained | No renderer, include directives, link fetching or automatic rewriting of code blocks and expressions. |
| EML | Decoded textual message bodies in MIME order; plain text preferred over its HTML alternative | Headers remain metadata hints. Attachments and nested attached messages are not executed or silently merged into the main body. Duplicate plain/HTML alternatives are avoided. |
| FB2 | XML body text in document order | Embedded binary assets and layout are not part of the text. It is not a rendered or DRM-removing ebook reader. |
| FB3 | Supported XML book-body text inside the declared package | The declared primary body must be the supported internal resource. Images and visual layout are not rendered; unsupported body structures are rejected. |
| ODT | Ordered supported text paragraphs | No LibreOffice execution, macros, embedded-object execution or layout reconstruction. |
| ODS | Ordered supported worksheet text and stored non-formula cell values | Simple formula cells and explicitly hidden rows/sheets are omitted with warnings. Matrix formulas, hidden row/column groups and unsupported inherited visibility are rejected before publication. Values are not recreated using spreadsheet formatting. |
| ODP | Supported slide text in document order | Slides are logical reading sections. Drawing order is not proof of a human-designed visual reading order. |
| PPTX | Text from the slides named by the presentation relationships, in presentation order | ZIP filename order is not slide order. No PowerPoint, macros, animations, active media or external relationship execution. |
| XLSX | Supported stored non-formula worksheet content in workbook order | ZIP filename order is not workbook order. Simple formula cells and explicitly hidden rows/columns/sheets are omitted with warnings. Ranged/shared/array/data-table formulas are rejected before dependent cached values can be mistaken for literal text. No formulas, macros or external workbook links are evaluated. Custom/conditional number formatting is rejected rather than guessed. |

The original TXT, PDF, DOCX and EPUB workflows continue unchanged. TXT is
subject to the existing exact UTF-8 attestation policy. PDF retains its own
physical-page and OCR review workflow. DOCX and EPUB retain their established
unsupported-content checks and unverified logical locations.

Aliases use one canonical retention suffix: for example, an `.htm` or `.xhtml` source is
retained as a byte-identical `.html` file, and `.markdown` as `.md`. This changes
the filename extension in the private original store, not the source bytes.

### Additional explicit reading conversions

The native **Convert → Convert documents for reading** workflow keeps additional
formats in the existing conversion service. It writes a new UTF-8 reading copy
and its source/text-hash provenance receipt; the retained original and catalog
quotation authority remain unchanged.

| Source family | Explicit reader | Finite boundary |
| --- | --- | --- |
| DOC / PPT / XLS, WPD, PalmDoc PDB, WRI | Selected installed LibreOffice | Fixed declared import/export filters and an inactive private profile; guarded ODF reading afterward. Supported binary/header families only. Visual layout, printed pagination and spreadsheet formula fidelity are not promised. |
| AZW / AZW3 / MOBI / PRC / LIT / TCR / CHM / DjVu | Selected installed Calibre | Unprotected supported book containers; DjVu must already contain text. Image-only DjVu is refused rather than silently routed through OCR. |
| MHT / MHTML | Built-in isolated MIME reader | The declared textual root only; no attachment execution or remote-resource retrieval. |
| TXTZ / ZIP | Existing guarded archive reader | One TXT for TXTZ, or supported readable text members for ZIP; bounded names/entries/expanded bytes, no filesystem extraction, nested archives or executable content. |
| RTF | Existing selected Pandoc route | Explicit local conversion and derivative review; no automatic download or PATH-based fallback is implied. |

Native tools are selected for the current operation and checked again before
execution. Selection alone does not qualify a tool's dependencies, every file
variant, visual fidelity or distribution rights. The dedicated finite native
gates must pass for their reported operating system and exact selected build.
The original direct-import table above remains the contract for raw catalog
imports; legacy suffixes are not promoted by renaming them to TXT.

See [selected legacy document boundaries](LEGACY_DOCUMENT_READING.md) and
[Calibre reading and actual eight-format acceptance](CALIBRE_READING.md).
No converter, browser, mail application, private model or DRM-removal tool is
bundled or automatically acquired by these reading adapters. Password and DRM
bypass are not provided.

## Resource and security boundaries

All additional formats enter the existing subprocess-supervised import path.
The worker can be cancelled or timed out. That process boundary is not a claim
of a complete operating-system sandbox. Imported text remains data; it cannot
be used as Python, shell code, an executable filter or a command to a local
assistant.

The existing original-file limit remains **64 MiB per source** in this
component. Archive screening rejects unsafe member paths, duplicate or aliased
names, links, encryption, unsupported compression, corrupt CRCs and excessive
expanded content. XML parsing uses the existing protected parser and checks
content/member/node/text budgets. No archive member is extracted into an
arbitrary filesystem path. The V3 large-library streaming expansion is a
separate requirement; this compatibility work does not claim to have removed
those resource limits.

Default native viewers can execute active content or request remote resources.
For all additional formats, the application retains
the original and makes its safe extracted reader available, while refusing
automatic default-viewer launch. Keeping a byte-exact original does not mean
executing that original is safe. No viewing or preview operation approves its
text for quotation.

## Acceptance evidence

`tests/e2e/test_extended_document_workflows.py` exercises real application
imports, worker isolation, Unicode source order, exact original retention,
reader navigation, FTS search, citation blocking, cross-library isolation,
extension aliases, pause/reopen queue recovery, metadata conflict handling,
backup/restore, changed planned files, damaged retained originals, unsafe
archives, cancellation/resume and blocked active-viewer callbacks.

The individual parser suites cover malicious and unsupported structures at
their respective format boundaries. Successful parser tests alone do not
establish application integration, Windows behavior, native screen-reader
accessibility or visual fidelity. Run the actual whole-path suite and the
required Windows checks for the exact release revision. Manual NVDA acceptance
must be recorded separately and is not inferred from these tests.

`tests/ui/native_document_formats_smoke.py` checks the installed application's
real wx format selector, Help menu, Import Center controls and worker results,
then reads an actually imported HTML source through the native Reading Desk.
Input paths are supplied by the synthetic test; operating the operating-system
file picker and manual NVDA are explicitly not claimed. A missing display or
wx runtime returns `NOT_RUN` (exit 77), never a passing acceptance result.


The current Office text boundary accepts the observed empty static PPT geometry
leaf and empty inactive header/footer slots, with exact attributes and finite
numeric bounds. A sequential path lexer avoids exponential regular-expression
backtracking. A separate ODS-only rule recognizes the four observed inactive
Calc sheet/page templates in direct master-page slots of `styles.xml`. It
requires the exact cached `???`, `Page ` and `1` values and recorded structure,
and emits `inactive_sheet_page_fields_not_read`. These layout fields never enter
the source text. Body lookalikes, other formats, additional authored text,
unknown attributes/children, drawing formulae, external links and unrecognized
active structures retain their refusal behavior.
See [the selected Office workflow](LEGACY_DOCUMENT_READING.md) for the actual
runtime identity, native fixtures and source/installed result references.

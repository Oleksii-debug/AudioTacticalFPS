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

### Formats outside this component

V3 also requests AZW/AZW3, CHM, DjVu, legacy DOC/PPT/XLS, LIT, MOBI, PDB/PRC,
RTF, TCR, WPD, additional raster-image workflows and more complex conversion
features. A suffix in that requirements list is not evidence of implemented
support. These formats must not be accepted by renaming them to TXT or by
claiming their output came from a supported parser.

This component does not bundle or automatically invoke calibre, LibreOffice,
Pandoc, a browser or a mail application. Optional external conversion needs a
separately verified executable/version contract, constrained local execution,
original and derivative provenance, explicit fidelity reporting, and tests of
the actual output. Password or DRM bypass is not provided.

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

## Consolidated compatibility verification

The additional-format modules were selectively reused from PR104 revision
`f1228730cd629671a739af786c5d9688c7ed0b1e` onto consolidated base
`f3ec201153d6e821154ad281e646482710c209fc`. This adaptation keeps the existing
complete-backup/sidecar implementation, reading state and managed OCR selection.
Only the format registry, parsers and corresponding intake, retention, queue,
source-opening, Import Center and application dispatch hooks are changed.

The private consolidated overlay executed **407 tests, all passed with no
skips**, on CPython 3.12.14. The run included the three additional-format suites
and existing intake, retained-source recovery, batch planning, DOCX/EPUB imports,
source opening, Import Center/OCR selection and complete-backup/sidecar suites.
The tests used installed filelock 3.29.0, pytest 9.0.2 and defusedxml 0.7.1.
This is portable source evidence; it is not a native Windows, operating-system
network-isolation, manual file-picker or NVDA acceptance result.

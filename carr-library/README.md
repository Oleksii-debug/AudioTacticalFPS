# Резонанс / Resonance


Resonance is an accessible Windows platform by Sceneria Innovative Technologies
for reading and scientific research, translation, speech and voice cloning,
transcription, music creation and audio production. These are the intended
platform capabilities; the implemented and qualified subset is tracked separately.

Start with the [current technical specification and architecture](docs/UNIVERSAL_SCOPE_V3_FINALIZATION.md),
[reuse decisions](docs/REUSE_DECISIONS_V2.md), [acceptance](docs/acceptance.md) and
[worker instructions](automation/COMMON.md). The internal package and existing
launch filenames are retained for compatibility. Carr is an optional collection.
The canonical product branch is carr-library; preserve main and unrelated projects.

The library workflows below describe the canonical implementation at the time
of this review. Audio/agent and other additions in open integrated candidates
require their own exact-head installed-build acceptance before being called released.
The owner-supplied transcription implementation is [under reviewed integration](docs/TRANSCRIPTION_INTEGRATION_REVIEW.md).

## Run the desktop

The standalone Windows x64 candidate includes Python, dependencies and the
verified local multilingual MiniLM model. Extract its complete folder, run
`Install.cmd`, then `ResearchLibrary.exe`. The CMD launcher remains available.
The native entry is an unsigned private candidate; no signed-release claim is
made. No Python installation or internet
connection is needed for that bundle. `CheckInstallation.cmd` exercises a
temporary synthetic library without touching personal research.

From a Python 3.11+ development environment:

```sh
python -m pip install '.[desktop]'
python -m carr_library
```

Use `python -m carr_library --help` for explicit data-folder, backup, restore and
model options. See [the running guide](docs/RUNNING.md) for installer and keyboard
instructions.

## Implemented workflows

- Maintain an academic reference catalog for books, articles, theses, reports
  and web resources, including references without an attached source file.
  Keep bibliographic descriptions separate from verified source quotations.
- Plan reading with status, priority, progress, rating, tags and personal notes;
  search and filter the reference catalog within the selected library.
- Preview and import RIS, BibTeX or CSL JSON bibliographies, inspect possible duplicate
  references, and export RIS, BibTeX, CSL JSON, CSV or plain text. Personal notes
  are excluded from bibliography exports unless explicitly requested.
- Format references through the installed local CSL processor, with seven
  bundled styles or an explicitly loaded custom style, and export TXT, HTML or
  DOCX. This is a static export, not a live Word integration. See
  [citation styles and BibTeX](docs/V3_BIBLIOGRAPHY.md).
- Create, rename, describe, archive and reopen independent libraries; organize
  books into multiple collections without duplicating original bytes.
- Import UTF-8 TXT, PDF, DOCX and text-based EPUB. Retain originals, publish catalog/revision
  changes atomically, isolate native parsers and resume interrupted imports.
- Import the documented static text subsets of HTML, Markdown, EML, FB2, FB3,
  ODT, ODS, ODP, PPTX and XLSX through the same isolated pipeline. Preserve exact
  original bytes and unverified logical segments; see
  [supported document formats](docs/DOCUMENT_FORMATS_V3.md).
- Read EPUB books by their declared section order, with keyboard chapter
  selection, previous/next navigation and separate authored image descriptions.
- Navigate embedded EPUB contents and headings, find exact text within a chapter,
  and inspect extraction notes; earlier imports retain their existing identities
  and remain readable through a chapter-start contents list.
- Search exact words, phrases or all terms; filter by collection, author, title,
  language and edition. Local thematic BM25 and multilingual MiniLM rank exact
  source slices. Catalog and source lookups share one authorized snapshot.
- Open source context and copy verified quotations and citations. Original text,
  physical PDF pages and unpaginated segments remain distinct. Printed page
  labels require their own independent approval.
- Organize nested research topics, immutable saved evidence, editable notes,
  tags and saved searches. Metadata revisions preserve earlier saved citations.
- Search explicitly selected libraries together with source metadata filters,
  grouped results and freshly verified context, copy and save actions. Manage
  saved topic searches with edit, duplicate, archive and restore; use JSON search
  commands without the desktop. See [academic search](docs/ACADEMIC_SEARCH.md).
- Read complete imported source pages or segments without a search phrase;
  resume the last opened page, keep named bookmarks, navigate back/forward,
  and hear exact location information through native text controls. See
  [the reading desk guide](docs/READING_DESK.md).
- Export Markdown, text, RIS, BibTeX and CSV with notes separate from quotations.
  Back up completed libraries with original files and checksums; restore into a
  new folder without overwriting the existing library.
- Review RIS or CSL-JSON reference metadata before applying selected fields to
  an existing book. Export a whole library or collection bibliography in
  CSL-JSON, RIS or BibTeX, and export a complete research topic branch with its
  verified evidence, notes and tags. See [academic workflows](docs/ACADEMIC_WORKFLOWS.md).
- Review catalog details, export a source inventory, and build a literature-review
  matrix with exact quotations and separate notes through **Library tools**.
  Its bibliography exports use the same catalog service as the Bibliography
  menu. See [catalog tools](docs/CATALOG_TOOLS.md).
- Create persistent local audio projects, assign actually installed voices,
  preserve original source text while editing pronunciation, and resume saved
  production jobs. Export the mix, chapters, subtitles and role stems. Configure
  optional local voice and music engines explicitly; MusicGen inference is
  unavailable on Windows in this candidate. Use typed controls to apply local
  audio effects and verify the resulting file without replacing its source.
- Review a scoped local assistant plan before executing supported library and
  production actions. API profiles are optional and credentials remain in the
  operating-system vault. Full-state backups preserve reading positions,
  project drafts and production state; restored external engine registrations
  require revalidation. See [the Ukrainian start guide](docs/START_UK.txt).

TXT quotation text is independently checked against retained original bytes.
PDF extraction and OCR require explicit independent review before being treated
as verified quotations. DOCX and EPUB are imported as logical segments, with unknown
printed pagination and an explicit unverified status. The [EPUB reading guide](docs/EPUB_READING.md)
describes the supported static XHTML subset. A model's similarity
score never approves a quotation.

## Candidate status and verification

This is an integrated development candidate. The assembled services and installed
Python package have real end-to-end tests, including offline local model
inference, citation preservation, workspace isolation and backup recovery.
The native Windows workflow also exercises installation, desktop controls, local
model inference and scanned-source review. Manual NVDA acceptance remains a
separate check; automated native UI tests do not establish its spoken behavior.

The Windows bundle's hashes and dependency inventory are verified during
assembly. Only local synthetic or lawfully supplied fixtures may be used in
public tests. Do not commit private books, quotations, file paths or client data.

## Development

Install `.[test,semantic]` for the complete test environment. OCR tests additionally
need local Tesseract and the pinned language files documented in
`src/carr_library/extract/OCR_MODELS.md`. Local-model integration tests require the
verified model directory; the test runner fails a required gate if tests skip.

```sh
python scripts/ci_check.py tests bootstrap docs/contracts/probes
```

The adapters reuse SQLite/FTS5, pypdf, PDFium, Tesseract, python-docx, FastEmbed,
ONNX Runtime and the already imported licensed components. Exact upstream
notices and model/dependency pins are preserved with the source and distribution.
See [scope and architecture](docs/UNIVERSAL_SCOPE_V3_FINALIZATION.md), [acceptance](docs/acceptance.md),
[packaging](packaging/README.md), and
[the control issue](https://github.com/Oleksii-debug/AudioTacticalFPS/issues/1).

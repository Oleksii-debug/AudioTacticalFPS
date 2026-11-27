# Резонанс / Resonance

A local desktop for research libraries, reading, derived documents and audio,
by Sceneria Innovative Technologies. Carr is an optional collection. The Python
package, existing data identities and internal CARR commands remain stable.
The current platform requirements are in
[V3 finalization](docs/UNIVERSAL_SCOPE_V3_FINALIZATION.md).
[Integration evidence](docs/RESONANCE_INTEGRATION_EVIDENCE.md) distinguishes
executed source/native checks from model quality, NVDA and delivery acceptance.

## Run the desktop

The Windows x64 build tools assemble a self-contained installation folder with
Python, pinned dependencies and the selected verified runtime/model components.
Its native `ResearchLibrary.exe` entry verifies the folder, performs first
installation when needed, and opens the desktop. The retained `Install.cmd`,
`ResearchLibrary.cmd` and `CheckInstallation.cmd` entries remain available for
installation, launch and a temporary synthetic self-test. A build is not a
qualified delivery until its exact source, installed Windows behavior and
component distribution profile have passed their separate acceptance gates.

From a development environment (the qualified Windows dependency profile targets CPython 3.11 x64):

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
- Preview and import RIS or CSL JSON bibliographies, inspect possible duplicate
  references, and export RIS, BibTeX, CSL JSON, CSV or plain text. Personal notes
  are excluded from bibliography exports unless explicitly requested.
- Create, rename, describe, archive and reopen independent libraries; organize
  books into multiple collections without duplicating original bytes.
- Import UTF-8 TXT, PDF, DOCX and text-based EPUB. Retain originals, publish catalog/revision
  changes atomically, isolate native parsers and resume interrupted imports.
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
- Create separate reading copies and narration plans without changing retained
  originals; keep literal reading distinct from structured production scripts.
- Render supported local speech/music projects through the existing audio
  service, apply typed role effects, save projects and resume checked completed
  work. The Studio includes bounded playback controls, device selection,
  seeking, playlists and A–B repeat. See [Studio](docs/AUDIO_STUDIO_V3.md),
  [effects](docs/LOCAL_STUDIO_EFFECTS.md) and [playback](docs/AUDIO_PLAYBACK.md).
- Convert explicitly selected documents into separate UTF-8 text and provenance
  files through isolated readers. Regenerate Word citations from the approved
  bibliography and style while preserving source identities. See
  [conversion](docs/DOCUMENT_CONVERSION.md) and
  [citation regeneration](docs/WORD_CITATION_REGENERATION.md).
- Prepare and review typed assistant plans through the same domain services.
  Optional local LLM proposals do not grant additional tool or file authority.
  Translation keeps the source and derived output separate and records provider
  authorization, costs and uncertain interrupted requests. See
  [assistant](docs/LOCAL_ASSISTANT.md) and [translation](docs/TRANSLATION.md).
- Register an explicitly inspected external transcription runtime and model,
  retain selected audio, admit checked results, save separate transcript edits
  and export derivatives. Shared resource admission coordinates supported heavy
  operations. Complete backups include accepted transcription work and edit
  history while excluding external engines and transient worker caches; restored
  execution requires review. See [transcription integration](docs/transcription_verified_protocol.md)
  and [complete backup](docs/COMPLETE_BACKUP.md).

TXT quotation text is independently checked against retained original bytes.
PDF extraction and OCR require explicit independent review before being treated
as verified quotations. DOCX and EPUB are imported as logical segments, with unknown
printed pagination and an explicit unverified status. The [EPUB reading guide](docs/EPUB_READING.md)
describes the supported static XHTML subset. A model's similarity
score never approves a quotation.

## Candidate status and verification

This is an integrated development candidate. Required source and installed
Windows workflows exercise actual subprocesses, local model inference, citation
preservation, workspace isolation, backup recovery, native controls and scanned
source review. Each result belongs to the exact tested source/tree and runtime;
a passing component or earlier commit does not qualify a later whole build.
Manual NVDA and listening acceptance remain separate checks.

The complete V3 platform also requires the authorized owner voice engine,
qualified real transcription/local-LLM/translation models, professional capture
and multitrack workflows, the reviewed private signing/update/entitlement
integration, and capability-specific language/quality acceptance. Adapter code,
synthetic fixtures and the 135-language catalog do not establish those results.
See [acceptance](docs/acceptance.md) for the complete required scope.

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
See [current scope](docs/UNIVERSAL_SCOPE_V3_FINALIZATION.md),
[retained library contracts](docs/UNIVERSAL_SCOPE_V2.md), [acceptance](docs/acceptance.md),
[packaging](packaging/README.md), and
[the control issue](https://github.com/Oleksii-debug/AudioTacticalFPS/issues/1).

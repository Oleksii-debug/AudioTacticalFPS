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
`Install.cmd`, then `ResearchLibrary.cmd`. No Python installation or internet
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
- Preview and import RIS or CSL JSON bibliographies, inspect possible duplicate
  references, and export RIS, BibTeX, CSL JSON, CSV or plain text. Personal notes
  are excluded from bibliography exports unless explicitly requested.
- Create, rename, describe, archive and reopen independent libraries; organize
  books into multiple collections without duplicating original bytes.
- Import UTF-8 TXT, PDF, supported static images, DOCX and text-based EPUB. Retain
  originals, publish catalog/revision changes atomically, isolate native parsers
  and resume interrupted imports. [Import Center](docs/IMPORT_CENTER.md) exposes
  bounded folder pages, saved continuations and explicit streaming resource
  profiles; [automatic OCR](docs/AUTOMATIC_OCR_UK.md) keeps language selection and
  independent text review separate.
- Read EPUB books by their declared section order, with keyboard chapter
  selection, previous/next navigation and separate authored image descriptions.
- Navigate embedded EPUB contents and headings, find exact text within a chapter,
  and inspect extraction notes; earlier imports retain their existing identities
  and remain readable through a chapter-start contents list.
- Search exact words, phrases or all terms; filter by collection, author, title,
  language and edition. Local thematic BM25 and multilingual MiniLM rank exact
  source slices. Catalog and source lookups share one authorized snapshot.
  [Search paging](docs/SEARCH_PAGING_AND_RESOURCES.md) covers the complete selected
  scope through bounded result/source windows and one current cursor owner.
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
  Back up libraries, their declared pending work and retained inputs with exact
  checksums. Full and explicit-chain incremental copies use the same verified
  snapshot and restore service; restore publishes into a new folder.
  See [backup resource controls and recovery](docs/BACKUP_RESOURCES_AND_RECOVERY.md).
- Use [explicit local translation](docs/LOCAL_TRANSLATION_UK.md) through a selected
  LibreTranslate-compatible loopback server, or the existing official Google
  provider routes. Saved derivatives preserve original source identity, current
  approvals and the provider ledger; completed text remains available offline.
- Save and review a [playback queue](docs/PLAYBACK_QUEUE.md) in Audio Studio. It
  reuses the current player and retained audio inputs; reopening or restoring a
  queue does not start playback.
- Review RIS or CSL-JSON reference metadata before applying selected fields to
  an existing book. Export a whole library or collection bibliography in
  CSL-JSON, RIS or BibTeX, and export a complete research topic branch with its
  verified evidence, notes and tags. See [academic workflows](docs/ACADEMIC_WORKFLOWS.md).
- Review catalog details, export a source inventory, and build a literature-review
  matrix with exact quotations and separate notes through **Library tools**.
  Its bibliography exports use the same catalog service as the Bibliography
  menu. See [catalog tools](docs/CATALOG_TOOLS.md).

- Review [English, Ukrainian and Slovak speech preparation](docs/SPEECH_PREPARATION.md)
  before applying it to a reading copy or Studio text. Original text and the
  exact derived mapping remain available; the accepted proposal is one Undo/Redo
  operation and uses the existing save and recovery routes.
- Repeat an explicit source-text A–B range and set or cancel a sleep interval
  in [live reading](docs/LIVE_READING.md), using the current reader and player.
- Use [native Word citation controls](docs/WORD_DESKTOP_INTEGRATION.md) with the
  existing CSL format and source-bound references. The optional local Word
  connection works on a new copy and keeps document Save and Undo explicit.
- Preview conflicts between two verified backups and
  [merge their published research content](docs/RESEARCH_CONTENT_MERGE.md)
  into a fresh destination, with whole-workspace choices and explicit
  acknowledgement of the operational state this selective profile omits.
- Inspect [long silent intervals](docs/AUDIO_SILENCE_INSPECTION.md) in a current
  completed Studio production. The read-only report is shown in the existing
  status field and does not alter the audio.
- Export [standard chapter CUE sheets](docs/AUDIO_EDITOR_INTERCHANGE.md) beside
  the measured WAV master, with explicit frame rounding and retained timeline
  and source bindings.
- Read actual installed [component notices in Help/About](docs/ABOUT_COMPONENT_NOTICES.md)
  and preview a bounded [redacted diagnostic report](docs/REDACTED_DIAGNOSTICS.md)
  before explicitly saving it to a new local file.

TXT quotation text is independently checked against retained original bytes.
PDF extraction and OCR require explicit independent review before being treated
as verified quotations. DOCX and EPUB are imported as logical segments, with unknown
printed pagination and an explicit unverified status. The [EPUB reading guide](docs/EPUB_READING.md)
describes the supported static XHTML subset. A model's similarity
score never approves a quotation.

## Candidate status and verification

This is an integrated development candidate. Required source and installed-package
checks, actual Windows controls, offline execution and large-library tiers qualify
the exact composed commit. See [PR143](https://github.com/Oleksii-debug/AudioTacticalFPS/pull/143)
and [release readiness](docs/V3_RELEASE_READINESS.md) for the current result and
retained evidence. Completed component runs apply to their own recorded commits;
they do not predeclare the final composition's result.

Manual NVDA acceptance, physical audio output and private model packages have
separate acceptance evidence. Word desktop automation is optional and reports
an explicit zero-check NOT_RUN result when Office is unavailable; CARR's native
citation controls remain required. The current UI uses English; the
[Ukrainian and Slovak localization plan](docs/UI_LOCALIZATION_PLAN.md) describes
the remaining catalogue and acceptance work.

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

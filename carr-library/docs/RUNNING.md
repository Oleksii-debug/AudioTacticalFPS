# Universal Research Library

An offline desktop research library with separate workspaces, books and
collections, literal and thematic search, source context, saved evidence, notes,
and exports. Quotations retain their source revision and page or text-segment
reference. Unknown printed page numbers remain explicitly unknown.

This is a development build. Windows and NVDA acceptance are recorded separately
from automated checks; a successful installation is not an accessibility verdict.
The prepared Windows bundle is for your private installation. Its Microsoft
runtime terms are included; publishing or redistributing that binary package
requires the applicable Microsoft redistribution grant.

## Windows offline package

The Windows x64 package targets CPython 3.11. The complete package includes its
own Python runtime. Extract the complete package into a new folder, then open
`ResearchLibrary.exe`. Its first launch runs the bundled offline installer and
opens the application after verification. Installation creates a local
environment inside that folder and uses only included, hash-checked wheels.
It does not contact a package index and does not require administrator access.
The smaller wheel-only variant requires CPython 3.11 through the Windows `py`
launcher. Its manifest identifies whether Python is included.

After installation, use the same `ResearchLibrary.exe` entry. This is an unsigned
private development candidate; publisher signing remains a release prerequisite.
`Install.cmd` remains an explicit installation command, and older packages without
the native entry use `ResearchLibrary.cmd`. Installation and
`CheckInstallation.cmd` verify that native desktop and document components can
load before reporting success, then run the synthetic application self-test.
When OCR is included, the checks also recognize a generated test image.
These checks do not change your research data.

The launchers require the installation-complete marker created after all install
checks succeed. If installation fails or is interrupted, extract the package to
a fresh program folder and open the native entry there. Keep research data in its
existing separate location; the installer does not overwrite an environment.

A private complete package may include the verified Microsoft C++ runtime
supplement required by wxPython and ONNX Runtime. Its original licenses and
source provenance accompany the files. This supplement is for the account
holder's private local installation; it does not grant public redistribution
rights. See `packaging/NATIVE_RUNTIME.md` in the source distribution.

The complete package includes local Tesseract OCR with pinned English,
Ukrainian and Slovak models and an orientation model. The launcher selects the
included engine and models automatically. Scanned pages wait for explicit
independent review before the complete book is published for searching and
quotation. OCR does not establish printed page numbers. No language download
is needed. See `packaging/OCR_RUNTIME.md` in the source distribution for the
review and runtime verification details.

The default data location is `%LOCALAPPDATA%\Universal Research Library`.
The portable launcher identifies its program folder. The application rejects
library data and backup/restore destinations inside that folder, including paths
that resolve there through an alias or junction. Existing saved library locations
use the same check. Choose a separate location for your research and backups.

To uninstall, close the application and check your selected library, backup and
export locations first. Earlier builds may have allowed research or backups
inside the extracted program folder. Copy any such personal files to a separate
location and verify the copy before deleting that program folder. Never delete
a folder containing your only research copy. The application does not silently
move existing data. The installer refuses to replace an existing environment.

## Installation from a source checkout

Use Python 3.11 or later. From the `carr-library` directory:

```console
python -m venv .venv
.venv\Scripts\python.exe -m pip install ".[desktop]"
.venv\Scripts\python.exe -m carr_library
```

On Linux, the interpreter is `.venv/bin/python`; install a compatible wxPython
build for the system's GTK libraries before using the desktop extra. The core
self-test and command help do not require wxPython.

```console
python -m carr_library --help
python -m carr_library --self-test
python -m carr_library --data-dir "D:\Research data"
```

`--self-test` uses a temporary synthetic library. It does not import or upload
your documents. Search and library data remain on the local computer.

## Open original sources and export research

The **Bibliography** menu adds reviewed local RIS/CSL-JSON metadata intake and
whole-library or collection bibliography export. **Ctrl+Shift+E** exports a
research topic together with its visible subtopics. See
[academic workflows](ACADEMIC_WORKFLOWS.md) for the field-review steps, supported
metadata and size limits.

After selecting a search result, use **Open original source file** to open a
copy in the computer's default PDF, text, Word or EPUB reader. The search status gives
the physical PDF page or logical text segment. Use the external reader to reach
that location; opening the file does not automatically jump to a particular
page. Changes made in that reader do not alter the retained library original.
Opening a source does not approve its extracted text for quotation.

For imported EPUB books, choose **Library, Read EPUB book** or press
**Ctrl+Shift+R**. Choose a book, then use the **EPUB section** selector or the
**Previous section** and **Next section** buttons. Section information and text
use named, read-only controls. Tab moves between them; Escape returns to the
library. Image descriptions supplied by the book's author have their own field.
The reader also includes supplementary sections in the book's declared order.
EPUB section numbers are not printed page numbers. Text remains an unverified
extraction; the application's verified-quotation and saved-evidence commands
do not approve it automatically. See [EPUB reading](EPUB_READING.md).

For embedded contents, chapter headings and literal search inside the chapter,
select the EPUB in **Books and Collections** and press **Ctrl+Shift+O**
(**Library, Read electronic book contents**). `F3` finds the next occurrence and
`Shift+F3` the previous one. An extraction report explains omissions and shows
authored image descriptions separately. Earlier imports open with their stored
chapter starts, without rewriting their text or existing research references.

Choose **Research, Export active topic**. Markdown, plain text and CSV preserve
source quotations and research notes in separate fields. RIS and BibTeX contain
only bibliography; they omit quotations and private notes. CSV text columns
ending in `_json` use reversible quoted text so line breaks and Unicode remain
exact and spreadsheet programs cannot execute a quotation as a formula.

Word (`.docx`) and HTML (`.html`) produce a readable research document titled
with the current topic name. Heading navigation separates evidence, source citations,
research notes and the source bibliography. A quotation spanning several source
pages keeps its separate slices and locators. Unusual control characters and
original line endings that a document reader cannot preserve are shown with
explicit reversible text encoding; they are never silently rewritten as a
different quotation. The document includes source identities for later checking.
These documents contain the topic's research notes as well as quotations.

In the Save dialog, choose the file type and use its matching extension. The
Word document uses native paragraph styles and can be edited in a word processor;
it is a static export and changes do not update the library. HTML is one local
file with headings for screen-reader navigation. Neither export loads remote
content or requires an internet connection. Source descriptions are not a
claim of conformity to APA, Chicago or another journal's citation style.

RIS and BibTeX preserve citation pinpoint locations as a labelled **Source
locator** note. They do not infer a publication's page extent from the pages
selected for a quotation. Research notes remain excluded from those formats.

Choose a folder outside the private library data folder. Existing ordinary export files can be replaced after the
Save dialog's confirmation. If another program changes the destination during
export, select it again before replacing it. A failed save keeps the previous
file intact.

## Academic references and reading plans

Open a library, then choose **Academic, Open academic library** or press
**Ctrl+Shift+B**. References can describe books, articles, theses, reports and
web resources even when no source document is available. Bibliographic titles,
authors, identifiers and publication page ranges do not establish quotation or
printed-page verification. They remain separate from retained originals and
historical source citations.

The catalog supports reading status, priority, progress, rating, tags, abstracts
and personal reading notes. Search and filter within the active library; use
Previous and Next when the list spans multiple pages. Opening the catalog keeps
the main research notebook and any unsaved evidence note in place. Dialogs use
named native text fields, choices and buttons; Tab, Shift+Tab, arrow keys,
Ctrl+Tab and Escape provide keyboard navigation.

Use **Add selected book to academic catalog** from the Academic menu to link an
already imported book. **Open linked document** opens a checked viewing copy;
changes in an external viewer do not modify the retained original. Standalone
references do not fabricate document text or allow source quotation.

Import BibTeX, RIS or CSL JSON files through a preview. Select the records to keep;
possible duplicates and unsupported fields are reported before import. Selected
records are saved in one transaction. DOI and ISBN comparisons and conservative
title/author/year matches identify possible duplicates for review. Records are
never automatically merged or deleted. Archive and restore retain their IDs.

Export the selected reference or all active references as RIS, BibTeX, CSL JSON,
CSV or text. Bibliographic exports omit personal notes by default. Include notes
only when you deliberately choose that option. For formatted citations, choose
**Citation styles** in the Academic Library. Preview one of the seven bundled
CSL styles or an explicitly selected local independent CSL file, then copy the
bibliography or export it as text, HTML or Word DOCX. Formatting runs locally.
Changed references require a fresh preview. See [citation styles and BibTeX](V3_BIBLIOGRAPHY.md).

For an online metadata request, open **Academic, DOI lookup and open-access
sources**, select providers and explicitly allow the lookup. Review returned
metadata before saving it. Downloading a reported open-access PDF requires a
separate confirmation and enters the ordinary reviewed import process. Entering
a DOI or URL in a reference alone makes no network request. See
[DOI acquisition](ACADEMIC_ACQUISITION.md).

Version 0.2 opens the exact supported version-three library layout through a
transactional upgrade. Existing source documents and citations are preserved.
Install an update into a new program folder, with a backup of your library saved
first. Open the existing data folder in the new program. Do not use an earlier
program version on the upgraded data folder. Previous valid backups can still
be restored into a new folder and upgraded on opening.

Full library backups also include personal page bookmarks and reading resume
positions for every library. Restore preserves their exact source revision,
names and timestamps in the new data folder. The backup checksums cover this
reading state as well as the database and retained originals. Older backups
without reading state still restore; use a separate reading-state export to
transfer those older bookmarks. Interrupted import queues are not part of the
backup. See [reading desk](READING_DESK.md) for historical-position handling.

## Review the source catalog

Open a library, then use the **Research** menu:

- **Library overview** summarizes sources, languages, active collections,
  searchable text units, recorded PDF reviews and links to visible research
  topics.
- **Source metadata and indexing checklist** lists sources with missing title,
  author or language data, no active text, an incomplete index, PDF pages without
  a matching recorded review, or no active collection. Missing optional metadata
  is a prompt to check the source, not permission to invent it.
- **Export source comparison matrix** compares your imported sources in CSV,
  Markdown or plain text, including collections, topic links, evidence counts
  and tags. Choose the entire library or one active collection.

These reports contain catalog metadata and stored link counts. They do not
include source text or research notes, approve an extraction, establish printed
pagination, or recheck the original files. Physical PDF pages and unpaginated
logical segments have separate counts. Archived topics and their descendants
are excluded from the research links. A multi-page saved quotation counts as
one evidence record, and historical source revisions keep their edition link.

Use **Tab**, **Shift+Tab** and the arrow keys in the scope and format choices.
Reports open in named, read-only text fields; **Escape** closes the report.
**Cancel current operation** stops analysis. Opening a report does not refresh
the evidence panel or discard an unsaved note.

See [catalog insights](ACADEMIC_CATALOG_INSIGHTS.md) for report limits and
interchange details.

## Research from the keyboard

### Save work and prepare a reading copy

**File, Save current changes**, or **Ctrl+S**, saves a pending research note.
**Ctrl+Shift+S** saves all main-window changes. Dialogs keep their own contextual
Save commands. The application reports an unsuccessful save and retains the
draft instead of reporting completion.

**File, New reading copy** opens an editable text project. Use **Import text as
a reading copy** for a UTF-8 text file or **Open saved reading copy** to continue
saved work. In the full-source reader, **Create editable reading copy** starts
a separate project from the displayed page or segment. Its original text,
source identity and revision are preserved. Normalization and pronunciation
replacements require preview and explicit application; edited reading text is
never approved as a quotation. See [reading copies](READING_COPIES.md).

**Options, Keyboard shortcuts** changes main-window assignments and checks
conflicts. **List current keyboard shortcuts** shows the active assignments;
the guide below describes defaults. Preferences can be reset, exported and
imported. See [keyboard settings](KEYBOARD_SETTINGS.md).

Complete backups now include page bookmarks, resume positions, saved reading
projects and recovery drafts, OCR language presets and keyboard settings when
present. The manifest checks each file and its source bindings before restore.
See [complete backup](COMPLETE_BACKUP.md) for the exact included state and the
remaining exclusions.

### Search several libraries and manage saved criteria

Press **Ctrl+Alt+F**, or choose **Research, Research Search Center...**.
Only the current library is selected initially. Use Space to select additional
libraries, then enter a phrase and optional title, author, language or edition
filters. Results identify their originating library and verification status.
Context, copying and saving recheck the original source; saving offers topics
from the result's own library. This dialog preserves the active notebook and
unsaved note. **Cancel operation** stops pending work without publishing partial
results.

With a research topic open, choose **Research, Manage saved topic searches…**
to create or edit criteria for saved quotations or research notes, run the
current definition, duplicate it, archive it or restore it. Each run searches
the topic's current evidence. Definitions survive reopening and normal library
backup/restore. See [academic search](ACADEMIC_SEARCH.md) for limits, source
safeguards and the headless `--list-libraries` / `--search` commands.

### Library and collection tools

Press **Ctrl+Shift+L**, or choose **Library, Library tools**. Choose the entire
library or an active collection, then use the named buttons:

- **Refresh report** shows source totals, missing catalog fields, extraction and
  index readiness, collection coverage, and editions sharing identical retained
  bytes. These are review candidates; the program does not merge or delete them.
  A recorded extraction or review is not a new independent verification of text.
- **Export bibliography** writes current source-catalog metadata as RIS, BibTeX,
  or CSL JSON. This includes sources without saved quotations. Author names retain
  their catalog order. Unknown publication dates, types and identifiers are not
  invented. The bibliography does not contain quotations or research notes.
- **Export source inventory** writes a CSV inventory for inspecting the selected
  collection outside the application.
- **Export review matrix** writes a CSV table of saved evidence from visible
  research topics, with topic paths, tags, exact quotations, separate research
  notes, source locators and immutable revision identifiers. Each page or logical
  segment remains a separate row. Collection membership is current; citation
  metadata stays as it was when the quotation was saved. Archived topic subtrees
  are excluded. Every candidate saved item is authenticated before collection
  filtering, so a damaged source or saved record stops export with an error.

Report details state when only the first items are displayed; totals remain
complete for the selected scope. Exports either finish completely or report a
size, time or cancellation limit. They never silently return a partial table.
CSV text columns ending in `_json` preserve exact Unicode using JSON encoding;
array columns such as `topic_path_json` and `tags_json` retain separate values.
This also prevents spreadsheet formula execution from source text or notes.
Use **Cancel operation** to stop a long operation before publication. Export destinations
use the same checked Save workflow as topic exports.

### Search and source actions

**Ctrl+Alt+Q** opens the advanced query builder. Its All, Any, Exclude and
Nearby fields accept one literal word or phrase per line. Build the expression,
choose Use query, then press Enter in Search. The explicit Advanced search mode
supports uppercase AND/OR/NOT, parentheses, exact quoted phrases and NEAR/0
through NEAR/100 within one page or text segment. Existing search modes keep
their original meanings. See `docs/ADVANCED_SEARCH.md` in the source repository
for syntax, examples, scope and limits.

Choose a library or research topic, then **Open selected scope**. In Search,
**Search mode** chooses how matches are found. **Result mode** independently
chooses **Short quotations**, usually a source sentence around the match, or
**Thematic excerpts**, with more surrounding text on the same page or text
segment. Neither mode generates or paraphrases the source. Copy, More and Save
use the same selected source passage. Changing a query or filter clears the
previous results; choose Search again. Results are limited, so use the source
filters to focus on a particular book, author or collection.

**Ctrl+Shift+I** resumes an interrupted import in the current library. Use
**Cancel current operation** to stop it. A page that still requires independent
review remains explicitly unverified.

Select a book in Books and Collections, then press **Ctrl+Shift+M** to edit its title,
authors, language, edition or volume. Title and author changes also describe
other editions of the same work. Saved quotations retain their historical
citations, and original source files remain unchanged.

In Evidence and Notes, **Ctrl+Shift+T** edits the selected quotation's tags,
one per line. **Ctrl+Alt+T** finds saved evidence with a tag in the current topic.
These actions preserve an unsaved research note. View tags is also available in
the Research menu.

The **Source** menu opens an optional independent PDF page review and can revoke
an existing review. Reading the extraction or opening the original never grants
approval. The reviewer must compare the full extracted page with the original;
Cancel leaves it unchanged. Printed page numbers require separate verification.

## Search across saved research topics

Choose **Research, Search saved research** or press **Ctrl+Shift+F**. Search
quotations, your own notes, or both across the currently open library. An exact
tag can narrow the search or find tagged material without a text query.
Archived topics are excluded unless you explicitly include them.

Results identify the topic, source, matching field and archive state. Original
quotations, citations and your notes have separate readable controls. Choose a
source part to read another page of a saved excerpt. Copying quotations and
citations verifies the selected saved source again; opening a topic uses the
existing protection for unsaved notes. Archived results remain read only.

The dialog has its own **Cancel current operation** and **Close** controls.
Tab and Shift+Tab move between controls, arrow keys choose a result or source
part, and Enter starts the search from the query field. Counts disclose a
result limit; refine the phrase or tag when more matches exist. See
`docs/RESEARCH_NOTEBOOK.md` in the source distribution for matching rules and
integrity boundaries.

## Optional local semantic search

The `semantic` extra supplies the pinned FastEmbed runtime. A separately
verified local model bundle is required; installing the extra alone does not
download or approve model weights. Literal and lexical thematic search remain
available when the optional model is unavailable. The repository's
`packaging/MODEL_BUNDLE.md` records the model revision and checksum workflow.
The complete semantic Windows package includes the checked MiniLM model and
loads it from its own `models` folder. It makes no model download on startup.

## Building an offline Windows package

The build host can run Linux or Windows; the resulting wheels target Windows
x64 and CPython 3.11. A native Windows test is still required before release.

```console
python -m build
python -m pip download --only-binary=:all: --platform win_amd64 --python-version 3.11 --implementation cp --abi cp311 --require-hashes -r packaging/windows-cp311.lock -d wheelhouse
python packaging/build_windows_bundle.py --wheel dist/universal_research_library-0.2.0.dev1-py3-none-any.whl --wheelhouse wheelhouse --lock packaging/windows-cp311.lock --output dist/windows-offline
```

For the complete package add `--python-archive path-to-pinned-python.tar.gz`.
For a release build, also pass `--source-commit` with the full commit SHA from
the local Git repository. The builder checks the application wheel's package
files against that commit and checks its complete wheel hash inventory before
recording the source identity. A stale wheel with the same version is rejected.
Git is required on the build host for this check, not on the user's computer.
The project's `.gitattributes` preserves those committed bytes on Windows too;
build from a fresh checkout if an older checkout converted line endings.
The exact Windows CPython 3.11.18 archive URL and publisher SHA256 are recorded
in `packaging/windows-python.json`, sourced from uv 0.13.9's official download
metadata. The bundler checks that SHA256 before unpacking with Python's standard
tarfile data filter and retains the runtime's license files. Runtime downloads
are build-time only.
For the complete private installation, attach the verified native runtime with
`packaging/add_native_runtime.py` after building the bundle. Follow
`packaging/NATIVE_RUNTIME.md` for the exact pinned supplement and command.
The application verifies and activates that separate DLL directory before
loading wxPython or ONNX Runtime, preserving the original Python runtime files.

To include semantic search, resolve/download `windows-semantic-cp311.lock`
instead and pass `--model-dir` and `--model-manifest` to the bundler. It applies
the application's pinned model verifier both before and after copying the
model; the model's nine original files remain unchanged. Its Apache 2.0 license
is included beside the model directory.
To include portable OCR, pass `--ocr-dir prepared/ocr` and
`--ocr-source-archive prepared/ocr-corresponding-source.tar`. The runtime has
its own `ocr-manifest.json`; the bundler verifies every runtime file before and
after copying, then checks the separately pinned corresponding-source archive.
The application's OCR adapter verifies runtime and model files again when used.
The runtime contains its required Windows DLLs, four pinned OCR models, and
original license and build records. See `packaging/OCR_RUNTIME.md` for the
exact build command.
`--sbom` includes a native uv CycloneDX 1.5 runtime dependency inventory after
checking that every package and version matches the dependency lock. The file
hash manifest separately covers Python, the application, and optional model
bytes; a dependency inventory alone does not certify a release.

The bundler validates wheel tags, checks every dependency wheel against the
previously resolved hash lock, and copies only those wheels, the application
wheel, the offline installer, and these instructions. It never collects local
libraries, source documents, caches, or credentials. It refuses an existing
output directory. Each bundle includes an inventory of exact file hashes.

## Third-party components

The application uses SQLite/FTS5 from Python, pypdf, PDFium through pypdfium2,
python-docx, platformdirs, and wxPython. Third-party wheel licenses remain in
their distributions. Vendored cairnsearch, filetype, rispy, Rifflux and
wx-accessible-grid license and provenance files are included in the application
wheel. See each corresponding `UPSTREAM` or `REUSE` file in the source tree.
The complete package also contains the Python runtime dependency notices from
its publisher's exact build metadata, plus source-pinned notices for dependency
wheels that omit their own license file. These are retained in `notices`.
The separately licensed OCR tools include GPL/LGPL libraries. Their original
notices and build records are in `ocr/licenses` and `ocr/provenance`;
matching source archives, recipes and patches are retained in the included
`ocr-corresponding-source.tar`. Keep these files with the package when
redistributing it.
Microsoft VC runtime terms, private-install notice and exact file provenance
are retained in `native-runtime/licenses` and `native-runtime/provenance`.
Microsoft DLL bytes are not
included in the source repository. The private-install permission for this
prepared package does not establish permission for public binary distribution.

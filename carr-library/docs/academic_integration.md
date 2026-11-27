# Offline CSL citations, bibliography and Word documents

The `carr_library.academic_integration` package formats actual CSL JSON references
through the canonical `citations.csl` module with its pinned, unmodified
**citeproc-js** processor and bounded **QuickJS** interpreter. It creates in-text citations,
numbered citations, note citations and bibliographies. It does not approximate
these formats with handwritten templates. The compatibility package retains the
verified-pinpoint, editable DOCX and regeneration API; the canonical bibliography
workbench uses the same processor, styles and locales. No Node runtime or second
vendor tree is required. Missing QuickJS is an explicit error; there is no download
or online formatting fallback.

The existing Python `python-docx` dependency creates editable DOCX output. There
is no Python `citeproc-py` production dependency. Evaluation of that engine found
incorrect page-range and name formatting for the pinned modern styles, so it is
not used as a fallback.

## Installed formats and provenance

| API style ID | Exact bundled upstream title |
| --- | --- |
| `apa` | APA Style 7th edition |
| `chicago-author-date` | Chicago Manual of Style 18th edition (author-date) |
| `chicago-notes` | Chicago Manual of Style 18th edition (notes and bibliography) |
| `mla` | MLA Handbook 9th edition (in-text citations) |
| `harvard` | Cite Them Right 12th edition (author-date/Harvard) |
| `vancouver` | NLM/Vancouver: Citing Medicine 2nd edition (citation-sequence) |
| `ieee` | IEEE Reference Guide version 11.29.2023 |

Locales `en-US`, `en-GB`, `uk-UA` and `sk-SK` are bundled. The compatibility
ID `chicago-notes` refers to the canonical `chicago-note` style. Existing documents
retain their original style digest; regeneration refuses to relabel old pins as
the newly installed style without an explicit style choice. Unavailable locale lookups made
inside a style use the installed matching language or English; they never fetch
remote XML. The public API rejects an uninstalled selected locale. Locale rules
come from the selected style and CSL locale, so they need not translate every
literal string embedded in a style.

Exact source revisions are declared once in
`citations/_vendor_citeproc/UPSTREAM.json` and the digest allowlist in `citations/csl.py`:

- citeproc package `2.4.63`, reported engine `1.4.61`, git revision
  `73bc1b44bc7d54d0bfec4e070fd27f5efe024ff9`; processor SHA256
  `55abba1a8b8b48c14323fd053f9646013b4b7a0c977938064ecf5195a502b9f1`.
- Styles: `citation-style-language/styles` revision
  `e27b9b7e204b1a40998f774ff05c8a6ed6a5f7d8`.
- Locales: `citation-style-language/locales` revision
  `2a7d750cb8b1d01ad838b42a2309d58a4d695c1b`.
- QuickJS-ng Python binding `0.18.0.2`; exact platform wheels belong to the
  dependency locks and the installed distribution's source/notice inventory.

Processor, styles and locales are digest-checked before execution. These hashes
identify installed resources; they are not release signatures. The compatibility
result exposes this current provenance and preserves each document's saved pins.

## Formatting API

```python
from carr_library.academic_integration import (
    CitationCluster, CitationRequest, available_styles, render_citations,
)

references = [{
    "id": "reference-17",       # Preserve the research library's stable ID.
    "type": "article-journal",
    "title": "An example article",
    "author": [{"family": "Doe", "given": "Jane"}],
    "issued": {"date-parts": [[2020]]},
    "container-title": "Example Journal",
    "volume": "12",
    "issue": "3",
    "page": "100-125",         # Publication extent, not the quoted page.
}]
result = render_citations(
    references,
    style="apa",
    locale="en-US",
    citations=[CitationCluster(
        (CitationRequest("reference-17", locator="112", label="page",
                         locator_verified=True),),
        id="claim-4-citation",
    )],
    cancelled=lambda: False,
)
print(result.citations[0].text)
print(result.bibliography[0].text)
print(result.attribution)
```

`available_styles()` returns `StyleInfo` records without starting a runtime.
`CitationRequest` values can be supplied directly instead of clusters; the API
assigns deterministic `citation-1`, `citation-2`, ... IDs. Use explicit cluster
IDs when they must survive document editing. A cluster may contain several
references. Its `note_index` is optional; pass the actual positive note number
when formatting a note sequence.

The result is immutable and includes plain text and sanitized markup, stable
reference/cluster IDs, warnings, style identity and an independent `.interchange`
snapshot. Changing a returned snapshot cannot modify the result. The engine
updates earlier citation strings when later citations require disambiguation;
numeric bibliographies follow first-citation order. Formatting all references
together is necessary for these rules.

### Source fidelity and pinpoint checks

Input dictionaries and source databases are never modified. Only supported
bibliographic CSL fields enter the processing copy. Private `note`, `abstract`
and unrelated workspace fields are excluded. Missing data stays missing; the
processor may show the style's conventional missing-date label. CARR does not
invent authors, dates, publication details, quotations or page numbers.

Publication `page` metadata describes an article/chapter extent. It is **never
copied to a citation locator**. A supplied locator is rejected unless the caller
explicitly sets `locator_verified=True`. This flag records the caller's source
check and is not independent verification by the formatter. A research UI must
derive that flag from its verified source-location workflow, or require the
user to check the page. It must not set it merely because metadata has a `page`
field.

After moving or repeating citations, format the full sequence again. Never
reorder old numeric or note strings and assume their numbering/disambiguation
remains correct.

## User-installed CSL styles and bounds

`render_citations(..., style_path=selected_local_csl_path)` explicitly loads an
independent local style containing both `<citation>` and `<bibliography>`.
The installed styles remain unchanged. The result identifies the local file by
basename and SHA256; private parent-directory paths are not exported. Its rights
metadata and correctness remain the style supplier's concern;
bundled-style test coverage is not a promise that every custom style is correct.

Remote style parents and dependent styles are unsupported. The canonical loader
requires an explicitly selected regular local UTF-8 CSL file, at most 512 KiB,
10,000 XML nodes and 64 structural/macro dependency levels. DTDs, entities,
unknown macros and recursive macros are rejected before execution. The stricter
shared style parser is also used by the compatibility API.

Compatibility operation bounds remain 1,000 references, 5,000 clusters, 100
citation items per cluster and 16 MB of input/output. The canonical QuickJS
memory budget is 512 MiB. A supervised fixed Python command (`-I -B`) enforces
the existing 30-second overall deadline (configurable from 0.1 to 60 seconds)
and cancellation, using the exact same `citations.csl.render` implementation.
The interpreter receives no host/network/filesystem callbacks. Ambient Python
and Node preload settings are not inherited as launch authority. The obsolete
`node_path` override is rejected explicitly. These are bounded processing and
process-isolation controls, not an OS security sandbox.

Errors expose short operational explanations rather than raw processor traces
or source passages. Returned HTML retains only a small formatting allowlist;
scripts, external resources, event attributes and links are not published.

## Reviewed bibliography import and safe desktop export

The existing **Academic library** window imports `.ris`, `.bib` and CSL `.json`
files through the same review dialog. A BibTeX file is parsed locally using the
pinned upstream packages described in [UPSTREAM_BIBTEX.md](UPSTREAM_BIBTEX.md).
Review warnings, check the desired entries, then choose **Import selected**.
The original citation keys and unsupported fields remain in private reference
notes; attached files and URLs are never opened by bibliography import.
The existing **Format bibliography and citations** command then formats the
selected library's references through this same CSL processor.

Desktop bibliography, outline, topic, catalog, comparison, reading-state,
import-report, provider-profile and studio-project exports capture their
selected destination before serialization, rendering or
queue submission. An existing file receives an explicit replacement question
with **No** as the default. If another operation changes that file while the
question is open or the document is rendering, saving stops and keeps the newer
bytes. Select the destination again after reviewing the changed file. A newly
created competing destination is also preserved. The canonical citation DOCX
export and regeneration continue to require a new filename.

## Editable Word export and regeneration metadata

```python
from carr_library.academic_integration import (
    CitationParagraph, export_citation_docx, read_citation_docx,
)

path = export_citation_docx(
    result,
    chosen_output_folder / "research-citations.docx",
    title="Research citations",
    paragraphs=[CitationParagraph(
        "A source-supported statement.",
        citation_ids=("claim-4-citation",),
    )],
)
snapshot = read_citation_docx(path)
```

The DOCX contains native paragraphs/runs, italics, heading styles, bibliography
hanging indents, stable bookmarks and citation provenance in a custom XML part.
For a note style it contains actual native Word **endnotes**, linked by
`w:endnoteReference`, and the bibliography. A body sequence must contain each
formatted cluster once in its original order. Native endnotes require
consecutive note numbers starting at one; otherwise reformat before exporting.
With `paragraphs=None`, a companion document lists all citations and its
bibliography.

The export is a new, editable document and requires a new `.docx` destination.
It does not overwrite originals or existing documents. Cancellation before
atomic publication leaves no finished artifact. Linked/reparse destinations
and Windows device/stream filenames are rejected on every platform.

Embedded metadata includes the original processing copy, locator checks,
reference/cluster IDs, exact style/processor hashes and rendered text. It is
hash-checked on read. This detects accidental corruption, not malicious rewriting
of both metadata and hash. Editing visible text in Word does not silently change
the embedded source snapshot. The reader does not execute document macros or
follow external relationships.

This is an offline DOCX integration. It does **not** claim an installed native
Word add-in, live citation fields, a Zotero bridge, Word automation, or automatic
bibliography updates inside Word. The regeneration helper below reads the
snapshot, applies explicit edits and reuses the formatting/export APIs to create
a new file.
Word installation, Office login and paid services are not required to export.

### Regenerating an existing CARR citation document

The public `regenerate_citation_docx` helper reads the bounded, hash-checked
embedded snapshot, reconstructs typed citation requests, runs the same pinned
local processor and writes a new DOCX through the existing atomic exporter:

```python
from carr_library.academic_integration import regenerate_citation_docx

regenerate_citation_docx(
    chosen_source_folder / "research-citations.docx",
    chosen_output_folder / "research-citations-regenerated.docx",
    cancelled=lambda: False,
)
```

The default retains the saved document title, exact paragraph text, stable
reference and cluster IDs, citation order, note indices and prior locator-check
records. All citation strings and the bibliography are actually reformatted;
cached rendered strings are not trusted as new output. Optional `references`,
typed `citations` and `paragraphs` supply explicit edits using the same contracts
as `render_citations` and `export_citation_docx`. Inserted citations need new IDs;
move existing IDs together with their paragraph references and supply the full
ordered sequence. Native endnotes still require consecutive numbers starting at
one; the helper never silently renumbers a supplied sequence.

Replacing a reference's bibliographic metadata invalidates automatic reuse of
that reference's checked locators. Supply explicit newly reviewed
`CitationRequest` values, or remove the locator. A source revision can change
without changing CSL metadata: callers must declare such changes through
`changed_reference_ids=("reference-17",)` and freshly check its locators. The
standalone helper does not inspect a library database or prove that source pages
remain current. A retained check is a historical caller assertion, not a new
independent source verification.

Regeneration requires the original processor identity and style bytes. If either
identity differs, it stops rather than assigning the old provenance to new rules.
A custom style needs an explicitly supplied `style_path` with the original hash;
stored source URLs and private paths are never followed. For a deliberate style
or processor migration, review the data and use the explicit formatting API.
The existing archive/XML bounds, canonical metadata/locator guards, unchanged
source checks, cancellation and no-overwrite publication remain in effect.

This helper regenerates the **embedded paragraph snapshot**. It does not read or
merge edits made to visible text in Word, preserve arbitrary Word layout edits,
install an add-in or update fields in a running Word application. Pass reviewed
paragraph edits explicitly. The original DOCX is retained. Snapshot checksums
detect corruption, not an attacker who rewrites both data and its checksum;
structural validation does not authenticate the source assertions.

## DOI metadata preview

The optional `DoiMetadataClient` is separate from offline formatting. It defaults
to network disabled. Explicit opt-in allows only the configured Crossref and/or
Unpaywall metadata request. Unpaywall additionally requires a user-supplied email.
It returns a preview with provenance, rights/license and location information;
it neither downloads a paper nor imports a reference automatically. See
[doi_metadata.md](doi_metadata.md) for its exact API, transport controls and tests.

## Licensing and distribution

The unmodified citeproc-js source is dual-licensed upstream under **CPAL-1.0 OR
AGPL-3.0-or-later**. This distribution selects CPAL-1.0. Preserve its copyright,
license, upstream source identity and user-visible attribution. The full CPAL
text and the upstream alternative AGPL text are in `citations/_vendor_citeproc/`; do not relabel
the vendored processor as MIT or as application-owned code.

Bundled styles and locales are **CC BY-SA 3.0**. Their upstream author,
contributor, source and rights XML remains intact. The full license and a notice
are included. No intentional formatting-rule modifications were made; terminal
newlines may have been normalized on retrieval. UI and exported documents show
`result.attribution`, crediting citeproc-js/Frank Bennett and the
[Citation Style Language project](https://citationstyles.org/).

Package data comes only from the canonical `citations/_vendor_citeproc/*` and
`citations/styles/*` resources, including their complete notices and provenance.
`academic_integration` contains Python compatibility/DOI/DOCX code only; it must
not restore a second engine, worker script, style directory or notice tree.
Runtime dependencies include pinned QuickJS, `python-docx` and lxml.

## Acceptance evidence

Run `python -B -m pytest -q tests/academic_integration` with the pinned QuickJS
runtime installed. Missing runtime is a failure, not a skip. Tests run the one
canonical processor for every bundled style and verify actual
names, dates, publication page extents, verified pinpoints, disambiguation,
numeric ordering, repeated notes, Ukrainian/Slovak locales, native DOCX endnote
parts, editable italic runs, stable metadata roundtrip and original preservation.
Additional checks cover XML/macro rejection, asset integrity, cancellation,
timeout, runtime preload isolation, path safety and corruption handling.
Online adapter tests exercise the explicit transport boundary without querying
a user's account or claiming a real external lookup. Native Windows Word/NVDA
interaction remains a separate platform acceptance check.

The current reconciliation changes the runtime and style pins. Historical Node
results remain historical; final source, installed Windows, native keyboard and
manual NVDA outcomes must name the current exact candidate independently.

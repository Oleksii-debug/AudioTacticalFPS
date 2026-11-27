# Offline CSL citations, bibliography and Word documents

The `carr_library.academic_integration` package formats actual CSL JSON references
with a pinned, unmodified **citeproc-js** processor. It creates in-text citations,
numbered citations, note citations and bibliographies. It does not approximate
these formats with handwritten templates. The local processor needs an installed
**Node.js 18 or newer** executable; use an administrator-supported current Node
runtime. CARR neither downloads Node nor runs npm during citation formatting.

The existing Python `python-docx` dependency creates editable DOCX output. There
is no Python `citeproc-py` production dependency. Evaluation of that engine found
incorrect page-range and name formatting for the pinned modern styles, so it is
not used as a fallback.

## Installed formats and provenance

| API style ID | Exact bundled upstream title |
| --- | --- |
| `apa` | APA Style 7th edition |
| `chicago-author-date` | Chicago Manual of Style 18th edition (author-date) |
| `chicago-notes` | Chicago Manual of Style 17th edition (full note) |
| `mla` | MLA Handbook 9th edition (in-text citations) |
| `harvard` | Cite Them Right 12th edition (author-date/Harvard) |
| `vancouver` | NLM/Vancouver: Citing Medicine 2nd edition (citation-sequence) |
| `ieee` | IEEE Reference Guide version 11.29.2023 |

Locales `en-US`, `uk-UA` and `sk-SK` are bundled. Unavailable locale lookups made
inside a style use the installed matching language or English; they never fetch
remote XML. The public API rejects an uninstalled selected locale. Locale rules
come from the selected style and CSL locale, so they need not translate every
literal string embedded in a style.

Exact source revisions:

- Processor: [Juris-M/citeproc-js commit 56e7ef3](https://github.com/Juris-M/citeproc-js/tree/56e7ef310154748368665cc8a2be124e3c141832),
  reported processor version `1.4.61`, git blob `31b4f533c4bb4816d91dd61f79b4aa54ff520ac2`,
  SHA256 `8d80b2d4ad5d249178ddfa4c535bb72bbd1883521a88e9362686558e2f82029d`.
- Styles: [Citation Style Language commit cf90f6c](https://github.com/citation-style-language/styles/tree/cf90f6ce72b67c14c7a5b40a612f32869cd92d36).
- Locales: [Citation Style Language commit f637c83](https://github.com/citation-style-language/locales/tree/f637c83f6267b761513bcc1cb0e95571172e768c).

`academic_integration/UPSTREAM.json` records each file's installed hash, exact
source URL and license. Installed processor, styles and locales are checked
before use. These hashes detect unexpected installed-file changes; the manifest
itself belongs to the trusted application distribution. They are not signatures.

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
    # node_path=r"C:\Program Files\nodejs\node.exe",  # Optional explicit local runtime.
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

Remote style parents and dependent styles are unsupported. Styles/locales must
be UTF-8, at most 1 MB and at most 20,000 XML elements. DTDs, entities, unknown
macros, recursive macros and excessive macro depth are rejected. lxml does not
resolve network resources. JavaScript code is never constructed from style XML
or reference text.

Default operation bounds are 1,000 references, 5,000 clusters, 100 citation items
per cluster, 16 MB of input/output, a 256 MB Node heap and a 30-second overall
deadline (configurable up to 60 seconds). Parent cancellation terminates the
worker. Fixed argv is passed without a shell. `NODE_OPTIONS` and environment
preload settings are excluded. Node's VM receives only JSON data and the pinned
processor, with dynamic string-code generation disabled; `process`, `require`,
filesystem and networking objects are not exposed to it. This is a constrained
data-processing design with process resource limits, not a claim that Node VM
is a general hostile-JavaScript security sandbox.

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
text and the upstream alternative AGPL text are in `vendor/`; do not relabel
the vendored processor as MIT or as application-owned code.

Bundled styles and locales are **CC BY-SA 3.0**. Their upstream author,
contributor, source and rights XML remains intact. The full license and a notice
are included. No intentional formatting-rule modifications were made; terminal
newlines may have been normalized on retrieval. UI and exported documents show
`result.attribution`, crediting citeproc-js/Frank Bennett and the
[Citation Style Language project](https://citationstyles.org/).

Package data must include `UPSTREAM.json`, `csl_worker.cjs`, `styles/*.csl`,
`locales/*.xml`, and **all** `vendor/*` files. Python code also needs the existing
`python-docx`/lxml runtime dependencies. A separate Node installer/runtime is an
explicit deployment prerequisite; app startup and the offline library remain
available when citation formatting cannot find it.

## Acceptance evidence

Run `python -m pytest -q tests/academic_integration` with actual Node installed.
Tests run the vendored processor for every bundled style and verify actual
names, dates, publication page extents, verified pinpoints, disambiguation,
numeric ordering, repeated notes, Ukrainian/Slovak locales, native DOCX endnote
parts, editable italic runs, stable metadata roundtrip and original preservation.
Additional checks cover XML/macro rejection, asset integrity, cancellation,
timeout, runtime preload isolation, path safety and corruption handling.
Online adapter tests exercise the explicit transport boundary without querying
a user's account or claiming a real external lookup. Native Windows Word/NVDA
interaction remains a separate platform acceptance check.

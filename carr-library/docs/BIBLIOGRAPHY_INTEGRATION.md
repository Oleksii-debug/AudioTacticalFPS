# Bibliography integration review

This change adopts the bibliography feature from PR111 at
`bd169b9962532ef567c4ed8e927c9b60a386a3f4` onto the integrated application at
`0929eab3f34cf36c9da2b9bcbc8a58ba590fab37`.

## Adopted feature and interfaces

The selected delta contains the real offline citeproc-js formatter, pinned
styles/locales and notices, bounded custom CSL intake, reviewed BibTeX parsing,
the scoped bibliography workbench, its native dialog, headless CLI, and focused
regressions. The existing Academic Library import chooser accepts `.bib` and
opens its existing transaction-backed preview. Its new **Citation styles**
button opens the formatting dialog with the selected reference and existing
executor. The separate `research-library-bibliography` entry point calls
`carr_library.bibliography_cli:main`; the main audio/acquisition CLI is retained.

`research/export_file.py` adds optional immutable destination capture and
atomic no-clobber publication to the existing text/binary export functions.
Existing callers retain their signatures and explicit replacement policy.
The formatting workbench checks workspace authorization and exact metadata
again before copy, after rendering, and immediately before publication.
Publication page ranges remain metadata; formatting does not approve source
text, authenticate quotation positions or modify original documents.

The parser, workbench and native dialog are selected independently of PR111's
older academic/search/OCR foundation. In particular, the separately integrated
RIS-to-catalog review in `ui/bibliography_import.py` is not the BibTeX workflow.

## Review findings and additional fixes

The native export dialog in the selected PR already captures the destination
before overwrite confirmation. The CLI's `--overwrite` path lacked that same
binding across its initial CSL preview. Two actual-render reproductions showed
that a file created or modified during formatting was overwritten and reported
as saved. The CLI now captures the destination before formatting and passes the
same approval through publication. An initially absent file always uses atomic
no-clobber publication. Regressions verify preservation of another writer's
bytes and successful explicit replacement when the selected file is unchanged.

The formatter uses a fresh `quickjs-ng` context with memory, stack and CPU
limits. No Python callback, host filesystem, network API or module loader is
installed. Actual runtime tests exercise CPU and memory failures and verify
that successive real renders have neither host I/O capabilities nor shared
globals. Custom XML is limited during construction and rejects DTDs, entities,
foreign namespaces, processing instructions, unsafe processor keys, and
recursive/missing macros. Creator mapping retains ambiguous or incomplete
personal names literally, rather than assigning punctuation to another name
component. Exact custom style bytes and their digest stay with the preview.

The native probe now retains and closes its own optional bundled-runtime
directory handle, matching the integrated application's runtime contract.

## Dependencies and package inventory

The new exact pins are `bibtexparser==2.1.0`, `pylatexenc==2.11`, and
`quickjs-ng==0.18.0.2`. Both Windows locks and SBOMs include the three inspected
wheel hashes. BibTeX's sole runtime dependency is the included compatible
`pylatexenc` pin. Tests used existing hash-checked Linux/pure-Python wheels in
an isolated target directory. The shared Python environment was not modified.

The package declares the bibliography entry point, vendored processor and
style/locale package data, and complete source notices. All existing integrated
package declarations remain present. [CSL provenance](UPSTREAM_CSL.md) and
[BibTeX provenance](UPSTREAM_BIBTEX.md) retain exact upstream identities,
limitations and license records.

## Verification

The stable selective source passed **539 tests** in 125.45 seconds. The run
covered CSL/custom styles, BibTeX and creator boundaries, real text/HTML/DOCX
outputs, asynchronous native-handler logic, destination races, existing
text/binary exports, and the integrated audio studio, narration editor and
file-lifecycle contracts. All seven installed styles ran in the actual pinned
JavaScript interpreter. Authored integration files pass `git diff --check`.
Vendored upstream assets retain their exact reviewed bytes, including existing
upstream whitespace; their SHA-256 manifest is checked by the tests.

The upstream exact `bd169b9` bibliography workflow
[45912360229](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45912360229)
completed 26 strict suites with **953 passes, zero failures and zero skips**.
Its installed Windows native probe completed **69 checks**, covering simulated
keyboard input, clipboard, custom styles and service exports. It explicitly
records manual keyboard use, native file pickers, NVDA and installer acceptance
as `NOT_RUN`. That upstream evidence is separate from this selective commit;
the combined application requires its own exact-source Windows run.

The native probe is `scripts/windows_citation_styles_smoke.py` and runs with
the installed wheel using `python -I -B`. The feature's direct source suites
are `tests/citations/test_{csl,custom_csl,bibliography_workbench,custom_workbench}.py`,
`tests/research/test_{bibtex_import,csl_author_boundaries}.py`,
`tests/ui/test_citation_styles_jobs.py`, and
`tests/reader/test_export_no_clobber.py`. The existing file-export and
audio/editor suites provide compatibility coverage for the shared writer.

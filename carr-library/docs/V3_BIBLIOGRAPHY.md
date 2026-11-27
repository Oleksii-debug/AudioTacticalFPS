# Citation styles and BibTeX import

This V3-08 increment extends the existing Academic Library. References, notes,
original documents and saved source quotations keep their existing storage and
authorization boundaries. This is one part of the expanded V3 product; it is
not acceptance of the audio studio, online acquisition, live Word add-in,
complete BibLaTeX semantics or manual NVDA workflows.

## Keyboard workflow

1. Open the required research library. Press **Ctrl+Shift+B** in the main window
   to open its Academic Library.
2. Choose **Import bibliography** to open a `.bib`, `.ris` or `.json` file.
   Read the preview, its unsupported-field warnings and possible duplicates.
   Select the records and choose **Import selected**. A failed selection is
   rolled back as one transaction.
3. Choose **Citation styles** (Alt+S inside the Academic Library). The dialog
   offers the selected reference or all active references in the current
   library; current list filters do not implicitly change that choice.
4. Choose a style, author-name convention and citation language. Use **Preview
   formatting** (Alt+P). Rendering runs in a worker; the window remains available
   to the keyboard. The read-only multiline preview receives focus when ready.
   To use a local independent `.csl` file, choose **Load custom style file**
   (Alt+S), select the file, then preview. Loading another custom file replaces
   that choice; the installed styles stay available.
5. Use **Copy bibliography** (Alt+B), **Copy citation examples** (Alt+X), or
   **Export bibliography** (Alt+E). Exports offer plain text, formatted HTML and
   editable Word `.docx`. Close with Escape or Alt+C. Copy and export recheck the
   library and metadata; changed references require a fresh preview.

Tab and Shift+Tab move through native controls. The source does not include a
claim that NVDA announcements or every native file-picker path were manually
accepted. The targeted Windows smoke exercises actual installed wx controls,
focus, keyboard navigation, async completion, clipboard and error paths;
manual screen-reader acceptance remains distinct.

## Styles and names

The application embeds the reviewed citeproc-js processor and pinned official
CSL resources. The runtime is the maintained `quickjs-ng` distribution; its
Python import is named `quickjs`. It has no JavaScript host filesystem/network
bindings and no online fallback. The formatter checks resource SHA-256 values,
rejects unsafe or malformed metadata and enforces input, output, CPU and memory
budgets. Heavy styles may take several seconds to compile.

Installed styles:

- APA Style, seventh edition.
- Chicago Manual of Style, eighteenth edition, author-date.
- Chicago Manual of Style, eighteenth edition, notes and bibliography.
- MLA Handbook, ninth edition.
- Cite Them Right, twelfth edition, Harvard author-date.
- NLM/Vancouver, Citing Medicine, second edition, citation sequence.
- IEEE Reference Guide, version 11.29.2023.

Locale terms are supplied locally for United States and United Kingdom English,
Ukrainian and Slovak. A locale changes citation terms; it does not translate
titles or invent publication metadata. The embedded runtime does not provide
ICU-quality locale-specific collation. The upstream grouped citation ordering
limitation and exact version identities are recorded in [the CSL provenance
record](UPSTREAM_CSL.md).

### Explicit custom styles

Custom styles use the same actual processor and output guards. The file must be
an independent CSL style with both citation and bibliography layouts. The loader
accepts at most 512 KiB and enforces XML depth, node, attribute and text budgets.
DTD declarations, entities, processing instructions, foreign namespaces,
unsafe dynamic keys and recursive or missing macros are rejected. A dependent
style that needs an external parent is refused with an explanation; no style,
locale or parent is downloaded. Select the corresponding independent style file
instead. Remote, Windows device, alternate-stream and redirected Windows paths
are not accepted by this offline file loader.

The preview keeps the exact selected bytes and their SHA-256. Changing or
removing the file afterward does not change that preview or its exports. Load
the revised file explicitly to apply a new version. Custom styles remain scoped
to the current dialog/CLI request; they do not replace installed resources or add
an unversioned library sidecar. A style's metadata or publication link is inert.
The application validates supported structural/resource boundaries; it does not
claim complete CSL schema conformance or scholarly approval of a user file.

Existing references store display names. **Keep author names exactly as entered**
is the default and does not guess surnames. Select the explicit personal-name
convention only when the relevant names use `Family, Given` or
`Family, Given, Suffix`. A protected organisation such as
`{Research and Development, Inc.}` stays one literal author in both modes.
Names without commas remain literal. These presentation choices do not change
the stored reference. The lower-level CSL renderer accepts structured creator
objects when the caller already has that metadata.

Bibliography page extents remain publication metadata. Source-exact quotations
and verified printed labels continue to use the existing source-citation tools.
Full Chicago notes may include a publication's page range; the preview identifies
that range as publication extent, not a verified quotation location.

## BibTeX profile

The actual `bibtexparser` and `pylatexenc` implementations handle structural
parsing, brace-aware authors and LaTeX text accents. The adapter adds bounded
string macro/concatenation handling, conservative same-file cross-references,
duplicate/malformed rejection and transaction-compatible previews.

Original keys, entries, used string definitions and parent records are retained
as inert **BibTeX import provenance** in research notes. Exporting without notes
omits that provenance. The application keeps its own stable reference IDs.
Attachments and TeX commands never launch a process, read an included file or
make a network request. Unknown fields or commands are preserved in provenance
with visible warnings. Unsupported child-reference types use the existing
reference categories with a warning; full Biber/BibLaTeX semantics are not claimed.
See [the BibTeX provenance record](UPSTREAM_BIBTEX.md) for exact versions,
licenses, limits and tests.

## Headless commands

The installed `research-library-bibliography` entry point runs the same services.
The equivalent module command works without wxPython:

```console
python -m carr_library.bibliography_cli styles
python -m carr_library.bibliography_cli import --data-dir "D:\Research data" --workspace WORKSPACE_ID --input references.bib
python -m carr_library.bibliography_cli import --data-dir "D:\Research data" --workspace WORKSPACE_ID --input references.bib --commit --skip-duplicates
python -m carr_library.bibliography_cli format --data-dir "D:\Research data" --workspace WORKSPACE_ID --style apa --names family-given --output "D:\Exports\bibliography.docx" --format docx
python -m carr_library.bibliography_cli format --data-dir "D:\Research data" --workspace WORKSPACE_ID --style-file "D:\Styles\journal.csl" --output "D:\Exports\journal-bibliography.html" --format html
```

Import defaults to a read-only preview; `--commit` explicitly applies it.
`--select` can repeat zero-based preview indices. Formatting requires an existing
library and an explicit workspace. It excludes personal notes and foreign or
archived references from all-active output. Existing destinations require the
explicit `--overwrite` option. Without it, publication uses an atomic no-clobber
operation, including when a file appears during rendering. A filesystem lacking
that operation fails clearly instead of weakening the replacement policy.
`--style` and `--style-file` are mutually exclusive. The saved-output report
includes the exact style hash and whether an explicitly loaded custom style was
used.

## Packaging and verification

The Python dependency pins are `bibtexparser==2.1.0`, `pylatexenc==2.11` and
`quickjs-ng==0.18.0.2`. Both Windows runtime locks and their SBOM inventories
include the exact inspected Windows/pure-Python wheel hashes. All processor,
style, locale and attribution resources are declared package data. The source
wheel verifier checks their bytes against the tested Git tree.

The dedicated `CARR bibliography and import acceptance` Windows workflow runs
strict source suites and the installed-wheel native probe. It publishes no
installer, voice, model or user data. The suite's results must be read for the
exact candidate SHA; predecessor CI is not a substitute.

The accompanying maintenance commit repairs already-completed TXT/PDF reimports:
requested collection membership is now applied through the existing atomic
registration contract. After a metadata revision, replay accepts only matching
current metadata and never reactivates the historical source revision. Conflicting
metadata, cancellation and consistency failures cannot silently alter source
identity. Baseline failures and passing recovery cases are preserved in the
committed regressions.

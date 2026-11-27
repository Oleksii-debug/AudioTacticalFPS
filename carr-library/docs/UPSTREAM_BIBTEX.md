# Offline BibTeX metadata import

The V3 academic adapter reuses the installed, unmodified `bibtexparser` and
`pylatexenc` packages. It does not vendor another parser, execute TeX, invoke
BibTeX/Biber, read attachments, follow URLs, or import source documents.

## Reviewed upstream identities

| Component | Version and source | License | Reviewed distribution SHA256 |
| --- | --- | --- | --- |
| bibtexparser | 2.1.0; [v2.1.0 commit 8103b71dad72215d1c64a76cd65ed511c63786bb](https://github.com/sciunto-org/python-bibtexparser/tree/8103b71dad72215d1c64a76cd65ed511c63786bb) | MIT | `bibtexparser-2.1.0-py3-none-any.whl`: `3b4b1de50ce45ee9091ccca72e1efc8732ab3ef615b70bcecc537c17cd84753e` |
| pylatexenc | 2.11; [v2.11 commit 0e936a10f125bc3357b1a9d68de0beec5f835a3f](https://github.com/phfaist/pylatexenc/tree/0e936a10f125bc3357b1a9d68de0beec5f835a3f) | MIT | `pylatexenc-2.11-py2.py3-none-any.whl`: `e78e7391d6c104f1ed150e21cfaa58016cdb50aa54406a2eecb793649ffdfdd0` |

`bibtexparser` requires Python 3.10 or later and declares `pylatexenc~=2.10`.
Both wheels are platform-independent; the application pins the tested pair.
The runtime does not install missing components automatically.

The exact [bibtexparser license](https://github.com/sciunto-org/python-bibtexparser/blob/8103b71dad72215d1c64a76cd65ed511c63786bb/LICENSE)
has Git blob `4d30978e4cb2f215ccde81ee6ef80cf33a4fd2c8` and SHA256
`1f092a7986b9ac79a4fb08ef0b3a02f1cf2f0a5d9aeab93dc281e3bf7268271c`.
Its copyright notice is Michael Weiss, 2021.

The exact [pylatexenc license](https://github.com/phfaist/pylatexenc/blob/0e936a10f125bc3357b1a9d68de0beec5f835a3f/LICENSE.txt)
has Git blob `56110d0afcb38edc9c9cb6148c9064b44968f45f` and SHA256
`90b6f8fc23cb0328a9678562627fb2ebc5f9c871712fad698d781869eb02862c`.
Its copyright notice is Philippe Faist, 2015–2019.
Retain both complete MIT notices from the distributions in any packaged runtime.

Official interface references inspected on 2026-11-26:

- [bibtexparser 2.1.0 documentation](https://bibtexparser.readthedocs.io/en/main/)
- [Parsing and checking failed blocks](https://bibtexparser.readthedocs.io/en/main/quickstart.html)
- [Library/model/middleware API](https://bibtexparser.readthedocs.io/en/main/bibtexparser.html)
- [Published wheel metadata and digest](https://pypi.org/project/bibtexparser/2.1.0/)

## Reused behavior and local adaptation

`research.bibtex_import.parse_bibtex(text)` uses
`bibtexparser.parse_string(text, parse_stack=[])` for structural parsing.
The upstream `failed_blocks` collection is checked before any rows are returned;
malformed/duplicate blocks never become a partially successful import.
Case-insensitive duplicate fields and string definitions are also rejected.
Citation keys differing only by letter case are rejected as ambiguous within
this import profile.

The upstream default string middleware only substitutes a complete single
symbol. The adapter therefore adds a bounded expression resolver supporting
quoted/braced literals, unsigned decimal values, case-insensitive `@string`
references, `#` concatenation, forward definitions, and the twelve conventional
month names. It detects undefined symbols, cycles, deep chains, and expansion
growth before concatenating an oversized result. One optional trailing comma
in `@string` is accepted. No executable macro evaluation is involved.

The upstream brace-aware coauthor splitter and strict name parser are reused.
The adapter retains unstructured name order. Explicit BibTeX comma names are
shown as `Family, Given, Suffix` without assigning new name components.
The low-level parser marks protected corporate names with braces, for example
`{Research and Development, Inc.}`. Protected punctuation inside a person name
also receives this literal marker to prevent a comma from being mistaken for a
suffix separator. A preview warning describes these transformations.
Comma-separated names with an empty family or given-name component are kept
as brace-marked literal text with an explicit review warning. For example,
`, Jr., John` remains `{, Jr., John}`; its suffix is never promoted to a given
name and John is never promoted to a family name. This check occurs after
text decoding, so an empty protected component such as `{}` cannot evade it.

The existing Academic Library selects the parser's `literal_names=True` mode,
which omits parser-generated markers while preserving actual decoded braces.
It never strips guessed markers from a name. Its CSL export and the
canonical citation formatter continue to use literal names; they do not guess
personal-name roles from commas. The full original entry remains in inert
import provenance. This extends the existing bibliography preview/import and
CSL/DOCX workflow, with no second citation processor, style registry or dialog.

Open **Academic library**, choose **Import bibliography**, select a `.bib` file,
review the entries and warnings, and explicitly import the selected records.
The existing **Academic > Format bibliography and citations** workflow then formats those
records with the same installed citeproc-js engine and DOCX exporter. No source
book is imported and no attachment is opened by this metadata-only action.
An empty optional suffix is omitted only when both required personal-name
components are present. Structurally invalid trailing-comma names fail the
existing strict upstream parser. The original entry remains in provenance
for review in every accepted case.

`pylatexenc.latex2text` decodes supported accents and text formatting in memory.
Known accent/text commands are allowlisted. Unknown or active commands remain
literal, with a warning, rather than being discarded. Math remains verbatim.
File inclusion is additionally disabled by the decoder subclass.
`@preamble` content is ignored, never executed, and reported in the preview.
No user macro definitions are installed into the decoder.

## Same-file cross-references

A `crossref` must resolve to an entry within the same supplied text. Missing
parents, cycles, and excessive chains fail the whole batch. The adapter inherits
only missing container/publication fields: year, date, publisher, volume,
number, issue, journal, journaltitle, and booktitle. A chapter or conference
contribution may use its explicit parent title as the container title.

Child titles, authors, DOI/ISBN, and page extents are never inferred from a
parent. The preview lists inherited fields. Original referenced entries and
their used string definitions are retained with the child, so selecting only
the child does not leave its provenance dependent on importing the parent.
This is a documented conservative metadata profile, not complete BibLaTeX
inheritance or Biber semantics. `xdata`, sets, editor/translator roles, and other
unmapped fields remain in provenance notes with an explicit warning.

## Persistence and evidence boundary

The return value matches existing academic adapters:
`[(fields_dictionary, warnings_tuple), ...]`.
The existing `AcademicLibrary` validation, duplicate preview, selected import,
scope authorization, and transaction/savepoint remain responsible for storage.
The parser itself performs no database writes.

Every imported reference's notes contain a labelled **BibTeX import provenance**
section with the original citation key, inert original entry, necessary parent
entries, and used string definitions. Imported note/annotation text is retained
before that section. Unknown fields remain in the original entry rather than
being silently lost. Original external citation keys never replace the stable
application reference UUID. Normal exports that omit research notes also omit
this provenance; the preview tells the user this explicitly.

Source files, source revisions, edition linkage, saved evidence and page-label
approval are untouched. Bibliographic `pages` remain publication metadata, not
an authenticated quotation pinpoint. No bibliography import approves a source
quotation or fabricates an edition/page locator.

## Resource bounds

- 8 MiB UTF-8 input and total expanded result/provenance budget.
- 2,000 references, 6,000 total blocks, and 1,000 string definitions per batch.
- 256 fields per entry and 128 characters per field/type name.
- 64 brace levels, 32 string-reference levels, and 16 cross-reference levels.
- 100,000 characters per expanded value and provenance note.
- 100 author names and 100 keywords per reference.

Bounds apply before high-risk expansion and while producing results, including
cross-reference provenance fanout. Overflow raises an error; content is never
silently truncated. Original source text is unchanged. The host's existing
metadata constraints may reject a field earlier than these parser budgets.

## Executed verification

Linux CPython 3.12 with the actual pinned distributions:

```text
python scripts/ci_check.py tests/research/test_bibtex_import.py
95 passed, 0 failed, 0 skipped
```

The cases exercise actual parser/decoder calls, accents and Unicode, name
grouping, strings/concatenation, conservative crossrefs, original-key retention,
raw provenance, no file/network/process access, malformed/duplicate rejection,
private-log suppression, memory/expansion limits, and disk-backed SQLite
metadata persistence and workspace isolation across reopening. They include
source invariance and explicit note-export behavior. Dependency-depth limits
apply equally to parent-first, child-first and interleaved string/cross-reference
orders; cached resolutions retain their full longest dependency depth.

The canonical formatter integration also exercises actual selected BibTeX
import, closing and reopening the SQLite library, citeproc-js rendering and
DOCX export/readback. Corporate names, incomplete names, Unicode and genuine
literal braces survive without assigning guessed personal-name roles.
The corresponding required path is
`tests/academic_integration/test_end_to_end.py` (11 passing cases).

These are functional parser/storage checks. Native Windows/NVDA acceptance,
arbitrary BibLaTeX compatibility, and all CSL style combinations are separate
checks; this document does not claim them.

# Catalog bibliography export

`bibliography_export.export_bibliography` exports an authorized workspace or
active collection without requiring saved evidence. It reuses the existing
SQLite `Catalog` and `browse_editions` services, keeps the caller's read
transaction open across cursor pagination, and makes no schema or source-file
changes. The caller still authorizes the workspace and chooses the destination
through the existing `research/export_file.py` boundary.

## Reused implementations and format sources

- RIS uses the already vendored MIT-licensed rispy writer from upstream
  [MrTango/rispy, commit b7aae3b2069ced3fb75287711300f2edf0bcac21](https://github.com/MrTango/rispy/tree/b7aae3b2069ced3fb75287711300f2edf0bcac21).
  Source and license remain in `research/_vendor_rispy/`; no additional copy or
  dependency is introduced. Its supported `set_header` override suppresses
  numbered headings so output also passes the existing strict local RIS reader.
  Each author remains a separate `AU` field. Unknown reference kinds use `GEN`.
- BibTeX reuses `research/evidence_export.py::_bibtex_text`, including its
  control-character rejection and documented whitespace handling. Each author
  is separately braced as a literal name, then joined using BibTeX's `and`
  separator. An `and` inside a name is retained within that name. Entries use
  `@misc`; publication types and given/family names are not inferred.
- CSL-JSON follows the official
  [CSL v1.0.2 input schema](https://raw.githubusercontent.com/citation-style-language/schema/v1.0.2/schemas/input/csl-data.json),
  inspected on 2026-11-26. The schema includes the generic `document` type,
  requires `id` and `type`, and supports `author` names with `literal` strings.
  Python's standard `json` module writes the JSON; no CSL rendering engine or
  style package is added. This exports exchange data, not a rendered citation
  in APA, Chicago or another style.

## Data and boundary decisions

Only known title, ordered author names, language, edition and volume are
exported. Unknown fields are omitted. Each scoped edition gets a stable opaque
ID, independent of its title, assets and collection membership. Distinct
editions remain separate even when their titles or original bytes are equal.
Source contents, notes, paths, asset hashes, DOI, publisher, year and unverified
page claims are not added to the records.

Traversal is limited to 10,000 references and output to 16 MiB of UTF-8. An
overflow or cancellation raises before returning any export. The caller's
transaction is left open on success and failure. Archived or foreign
collections and missing/archived workspaces are rejected. The format adapter
does not read source, evidence or asset-manifest tables.

RIS rejects metadata containing control characters or line separators, which
could otherwise inject tags. CSL-JSON preserves those strings through JSON
escaping. BibTeX retains the established adapter's escaping and represents tabs
and line breaks as spaces, disclosed in the exported comment. Catalog metadata
remains user-supplied; successful export is not independent citation review.

## Verification

`tests/catalog/test_bibliography_export.py` uses real canonical schema V3
databases and read-only SQLite connections. It covers workspace/collection
isolation, archived and empty scopes, distinct editions sharing source bytes,
Unicode and literal author order, actual RIS parser roundtrip, BibTeX syntax
injection, a concurrent writer during multi-page export, cancellation, exact
UTF-8 size boundaries, and the actual 10,000/10,001-reference boundary. It also
denies access to source/evidence/asset-manifest tables during export and checks
that the schema fingerprint remains unchanged. No external Zotero import or
native Windows/NVDA interaction is claimed by these backend tests.

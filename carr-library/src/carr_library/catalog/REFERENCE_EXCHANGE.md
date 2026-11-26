# Reviewed local bibliography exchange

The metadata-preview adapter reuses the actual vendored MIT **rispy** parser
through `ris_preview.preview_ris`; provenance and original license are retained
in `RIS_REUSE.md` and `_rispy_LICENSE`. It does not introduce another RIS value
parser. After validated parsing, a tag-name inventory identifies discarded RIS
fields without exposing their values.

The CSL-JSON adapter implements a narrow profile using Python's standard JSON
decoder and the official CSL **1.0.2** input-data schema as a specification:

https://raw.githubusercontent.com/citation-style-language/schema/v1.0.2/schemas/input/csl-data.json

It does not copy the schema, install a new dependency, fetch a schema at runtime,
or claim full CSL validation. The official schema permits the generic `document`
type, literal author names, and the title/language/edition/volume fields used by
the current catalog. Zotero documents CSL-JSON and RIS import/export here:

https://www.zotero.org/support/dev/data_formats

The preview accepts a nonempty UTF-8 (optional BOM) list of up to 5,000 CSL
records, with `id` and a recognized `type`. Input is capped at 4 MiB, 16 nesting
levels, 100,000 values and 16,384 characters per general string. Duplicate JSON
keys, non-finite numbers, unpaired surrogates and unsuitable controls fail the
whole batch. Supported title/author fields are capped at 512 characters;
language, edition and volume at 4,000; author lists at 100. The existing strict
RIS framing limits also apply to RIS input.

Only title, ordered author display strings, language, edition and volume can be
offered for copying. Literal names stay literal. Structured names retain all
provided name components in reviewable display text, with an explicit warning
that their order needs review and their structure is not retained. Unknown
fields, publication dates, publisher, DOI, URLs, attachment references, reference
type, external IDs and name-rendering flags are inventoried as unsupported;
their values are never returned. Unknown publication metadata is not inferred.

The batch hash and record ordinal locate unverified local metadata only. They
are never trusted work, edition, asset, revision, page, passage or citation IDs.
The preview does not read files, access links, write to the database, deduplicate
records, add sources, or modify saved citation snapshots. A UI must select one
record, show unsupported fields and review warnings, and pass explicitly chosen
supported fields through the existing authorized, optimistic metadata update.
Ambiguous repeated RIS candidates remain separate. This is bibliographic data
exchange, not APA/Chicago formatting or an assertion of lossless interoperability.

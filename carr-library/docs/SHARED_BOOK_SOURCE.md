# Shared current source capture for books and translation

This module adapts the accepted PR152 `assistant/translation_source.py` and
`speech/source.py` behavior into `reader/book_source.py`. It calls the current
ReadingDesk and reuses the current exact `legacy_reading.validate_anchor`
shape. No alternate reader, provider, speech service or execution state is added.

## Stable interface

- `capture_source(library, workspace, edition, *, topic=None, first=None,
  last=None, chapter=None, revision=None, max_characters=2_000_000, timeout=60,
  cancelled, guard)` returns exactly `text, title, warnings, source_anchor,
  source_metadata, source_topic`. Page separators are explicit `\\n\\n`;
  each original page slice and ReadingLocation remains exact. The title and
  metadata come from the existing source.
- `capture_sources(library, selections, *, max_documents=32,
  max_total_characters=8_000_000, timeout=60, cancelled, guard)` preserves the
  explicit selection order. Each selection has workspace/edition, optional
  first/last/revision/topic/title and `kind=document|range|chapter`. An explicit range requires both inclusive ordinals. A chapter uses
  `first` as its exact EPUB reading-order ordinal and requires the real EPUB
  inventory. An optional title is an expected source title, not a rename.
  Translation callers should select their smaller two-million-character total.
- `source_pages(source)` yields `location, ordinal, raw_text, source_start,
  source_end, verified`. Source offsets address the joined snapshot.
- `validate_source(library, text, anchor, metadata, topic=None, ...)` refreshes
  the active scope, revision, metadata, retained original and every page.
- `validate_fragments(library, origins, *, topic=None, metadata=None,
  max_characters=8_000_000, timeout=60, cancelled, guard)` takes ordered exact
  `{location, ordinal, start, end}` origins. Here start/end are offsets into
  that original page; optional `text` is compared exactly. It returns a tuple
  of exact fragments. Optional metadata maps
  `(workspace, edition, revision)` to the captured metadata dict.
- `source_context` and `validate_capture` are pure shape/hash validators.
  They do not read an external original or authorize current execution.

## Bounds and authority

A source is bounded to two million characters and 10,000 existing reading
positions; a selection has at most 32 documents and eight million characters.
The caller may request smaller limits. Capture and validation share one finite
0.001–3,600 second deadline, including guard callbacks and fresh page reads.
Cancellation or a limit breach returns no partial accepted capture.

After the last capture, all earlier sources are validated again. Fragment
validation uses a bounded page cache and rechecks every selected page before
returning; it does not use an arbitrary page-one check that could miss altered
selected text or unnecessarily read a large unselected page.

An independent text-review flag may advance later. That historical flag remains
data; `quotation_authority` is always false. An archived/hidden scope, new
active revision, modified original, altered page bytes or changed metadata is
refused rather than silently retargeted.

The source suite is `tests/reader/test_book_source.py`, using actual current
SQLite/retained text and the existing EPUB compatibility fixture. At this source
checkpoint execution is **NOT_RUN** because the executor is unavailable.

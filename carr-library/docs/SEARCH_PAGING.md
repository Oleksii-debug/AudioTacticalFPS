# Bounded search navigation over existing sources

## Desktop behavior

Main Search and Research Search Center use the existing LibraryApp and FederatedSearch services. Search starts the first source window; Previous/Next result moves within it, and Previous/Next source window visits the remaining material. Search Center also exposes Previous/Next selected libraries. A cooperative time allowance is editable in seconds (default 300, positive and at most 86400).

An invocation reads at most 20 selected libraries, 100 source pages or logical segments per library, and 5,000,000 source characters per window. The catalog is visited in 500-edition keyset pages, with active-page continuation inside each catalog page. These limits bound one operation; they do not reject an entire library merely because it has 501 editions or a source has 101 segments. The full explicit library selection occupies at most 1 MiB. Each owner retains at most 2000 live action handles. Native navigation retains at most 128 previous source windows; forward continuation is not capped, and a fresh Search restarts from the beginning.

Result ranking is local to the displayed source window. Selected libraries keep their selection order. No global relevance or simultaneous multi-library snapshot is claimed. A successful search replaces its owner's completed session; a failed or cancelled operation cannot publish partial source actions. Source context/copy/save continues to verify the exact active source and existing evidence rules.

## Source identity and recovery

A source cursor is passive JSON navigation data, not authorization. It binds the root, workspace, query, retrieval/presentation options, metadata/collection filters and authorized topic. Each call opens a fresh authorized database read transaction and checks the cursor's active revision marker. A changed scope or inactive marker refuses continuation. Source changes between calls may change ordering; restarting Search obtains a new traversal. A result lookahead additionally compares the source-window identity before publication.

The source window contains only bounded source IDs and metadata. Existing literal normalization and exact spans, lexical ranking, pinned semantic runtime/cache and explicit semantic fallback remain in place. A single extracted page above 5 million characters must be split into supported reading segments before this adapter can search it. No new queue, index, model or database is introduced.

## Python API

`LibraryApp.search_page(workspace, query, cursor=None, offset=0, limit=100, timeout=300, **existing_search_options)` returns:

| Field | Meaning |
| --- | --- |
| results | Exact existing SearchResult objects for this result page |
| cursor | Source cursor for subsequent result pages in this same window |
| has_more_results | One-result lookahead found another matching result in this window |
| next_source_cursor | Cursor for the following source window, or `None` when complete |
| offset | Requested result offset within the current source window |
| source_pages / source_editions / source_characters | Bounded selection inspected for this window |
| window_identity | Digest of the exact selected active-page metadata for lookahead consistency |

Exhaust result pages using the returned `cursor` and increasing `offset` by the requested `limit`. Then pass `next_source_cursor`, reset offset to 0 and continue. Empty result pages can still have a next source cursor. All six retrieval modes support this interface. The older bounded `search` API remains available for existing callers.

FederatedSearch accepts `paged=True`, `source_cursors={workspace_id: cursor}` and `library_offset`. Absent source-cursor entries start that library; an explicit `None` entry means its source traversal is complete. `next_source_cursors`, `has_more`, `has_more_libraries`, `selected_workspaces` and `source_windows` identify the available continuations. Only the current 20 selected libraries are read.

## Command line

```text
python -m carr_library --data-dir /path/to/library --workspace WORKSPACE_ID --search Needle --search-mode terms --limit-per-library 100 --search-timeout 300
```

Continue results with `--offset-per-library 100` while preserving the same source-cursor JSON and selected options. Continue source material with the exact JSON object from `next_source_cursors` in `--source-cursors`, resetting the result offset to 0. Continue selected libraries with `--library-offset 20` and an initial empty source-cursor object. Shell-specific quoting is required for JSON.

## Evidence

The new source gate creates and searches 501 retained editions, a 205-segment original in all six retrieval modes, more than 20 selected libraries, literal presentation deduplication, invalid/inactive/cross-topic cursors, larger deadlines and cancellation without handle publication. Existing independent federated/source-integrity and desktop callback tests are retained. The native Search Center probe now traverses 100+5 real source segments with the actual controls. The gates must finish on the candidate before acceptance; authoring during the executor outage is NOT_RUN.

These fixtures establish application behavior when executed. They are not an actual 1k/10k/50k corpus throughput, memory or latency benchmark, and do not qualify semantic-model quality, hardware or human NVDA use.

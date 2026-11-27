# Search continuation — V3-07 bounded integration

This change adds source and result continuation to the existing library services.
It does not certify the remaining large-corpus, ingestion, source-size or
semantic-index requirements. Source review and executable qualification are
separate; no benchmark, native Windows or NVDA result is claimed by this document.

## One service path

Use LibraryApp.search_page for a library and FederatedSearch.search_page for an
explicit ordered library selection. A completed page contains current result
handles, next_cursor, page/count information and an explicit ordering statement.
The next request repeats the query, filters, presentation, selected libraries and
page size. A cursor is bounded metadata, never permission to copy source text.

Existing single-call search APIs remain compatible, including their per-call
guards. The desktop library search and native Research Search Center use the new
page path. CLI users choose --search-page with the existing --search/workspace
options, then repeat that invocation with --search-cursor. The assistant's
library.search result includes the same continuation and accepts cursor as an
explicit argument. No continuation automatically approves or runs another tool.

## Consistency and resource limits

Each page checks a streaming fingerprint of selected catalog metadata, active
revision/page identities, source digests and citation metadata inside an authorized
SQLite read transaction. A changed query, scope, selected-library order, data
directory, model configuration or source fingerprint refuses continuation.
Federated scope fingerprints include every explicitly selected library, including
libraries not yet reached by the current result page.

The existing search and source services still validate exact raw spans, retained
originals, quotation approvals and action scope. Successful navigation replaces
only the page's selection handles; failed or cancelled work publishes nothing.
A previous page is rerun through these services; its old quotation handles are
not replayed from the UI.

Catalog enumeration uses at most 500 editions at a time. A source batch has at
most 100 pages and at most 5,000,000 source characters. These are per-batch
boundaries, so more editions, pages and result pages can be traversed. A single
source page that exceeds its declared character budget still fails explicitly.
Each worker invocation retains the existing cooperative time and cancellation
boundaries. A fresh invocation continues later work without a whole-search
60-second deadline.

Fingerprinting streams metadata under a configurable SearchPageBudget row budget
(default 1,000,000 edition/page records), cancellation and the per-call deadline.
Selected-library IDs have a declared metadata byte budget, replacing a 20-library
whole-search ceiling. Up to 20 empty libraries are visited in one work slice;
further selected libraries remain reachable through its cursor.

The UI keeps current-page text/handles only. Previous navigation retains cursor
metadata within a 1 MiB history budget. Expiring old navigation history never
stops forward search; the status explicitly says when an older cursor expired,
and starting the search again can revisit earlier sources.

## Ordering and qualification

Literal results follow edition, source ordinal, page ID and raw offset. Stable
continuation does not use an ever-growing offset or collect all matches.

Thematic and semantic adapters retain their existing BM25/RRF/window behavior
inside each bounded source batch, retaining the existing top-100 ranked-result
selection per batch. Literal continuation enumerates all matching spans; ranked
continuation does not claim every possible semantic window. The batch order is explicit; concatenated
batch scores are never described as a global relevance ranking. Existing model
unavailability and window-budget fallback notices remain visible.

Required qualification includes real SQLite/original-source corpora above
500 editions, more than 20 selected libraries, more than 100 matches, stable
multi-page coverage, stale-cursor rejection, current quotation actions,
cancellation, native Next/Previous controls and CLI/tool parity. Any generated
small corpus is synthetic. A 1k/10k/50k real-corpus benchmark, latency percentiles,
hardware profile and NVDA usability must be measured before being reported.

The [measured catalogue profile and query-shape fix](LIBRARY_CAPACITY.md)
preserve the exact source identities, complete 1k/10k observations, original
50k timeout, explicitly interrupted baseline and optimized progress boundary.
The report separates synthetic registration from actual small TXT imports and
does not turn a partial 50k traversal into full or native/NVDA qualification.

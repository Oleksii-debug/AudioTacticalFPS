# Candidate work within one paged search

Status: source review and regression fixtures are ready; execution and performance are NOT_RUN.

## What this change does

A public LibraryApp.search_page operation can perform two ordinary searches: the requested result page and the hidden one-result lookahead. Both searches still open their own workspace-authorized, read-only SQLite transaction. Both still select the current source window, read every scoped raw page, check its SHA256, resolve source selections, authenticate retained originals, and run the existing text/PDF attestors. The existing comparison of the two source-window identities remains mandatory. Any failure prevents the new result handles from being published.

The only shared object is temporary literal candidate computation: the in-memory trigram index and a small set of normalization maps. The first search builds that work from its checked rows. The second search independently reads and validates its current rows before it may use the work. Reuse requires exact equality of all eleven SQL row fields, including workspace, edition, revision, asset, ordinal, locator metadata, raw text and digest. A difference closes the old index and builds new candidate work. An unchanged digest alone is insufficient.

The object is created for one public paged operation and closed in its finally block. Cleanup finishes before new public selections are published. A later result-page request creates another object even when its cursor and source bytes are unchanged. Ordinary non-paged literal search keeps its existing independent index lifetime. Thematic and semantic search retain their existing path.

## Bounds and cancellation

The shared candidate index is confined to the existing source window: at most 100 source pages and 5,000,000 raw characters. Only normalization maps for pages no longer than 2,048 source characters are retained; their combined source length cannot exceed 32 Ki characters, and the source-window bound limits them to at most 100 entries. Larger maps are computed through the same implementation and discarded as before.

Each literal invocation installs its current cancellation/deadline callback on the candidate index and removes that callback before returning. A former snapshot's callback cannot govern the next read. Current cancellation checks remain around fresh scoped reads, candidate binding and normalization. The outer public search still makes its final cancellation check after candidate cleanup and before publishing handles.

This is not a source-authentication cache, a persistent index, a cursor permission or a cross-call cache. No original-byte result, source authorization, catalog membership or attestation outcome is reused.

## Why this work was investigated

The exact original C3 retained-fixture measurement used result pages of 37 and source windows of at most 100 pages. With one matching result on each of 100 pages, the current implementation performs three public result-page requests and two complete lookahead searches for that window.

The verified 10,000-book, 200,000-segment source profile completed traversal in 432.381463435 seconds across 6,000 public requests and 2,000 source windows. Its complete five-phase profile, including backup and restore, passed. These values belong to original C3 head b524b3ac3453864903ec415ce7a5f048da09d700 and its recorded Linux environment.

The 50,000-book, 1,000,000-segment profile also passed all five phases. Its traversal took 3508.494986251 seconds across 30,000 public requests; the entire measure phase took 3563.203822906 seconds. Backup creation, verification and restore took 70.42977900799997, 35.031313886000135 and 78.83511036300024 seconds respectively, with the original library root removed and 50,014 editions verified after restore. These are actual original C3 measurements, not measurements of the optimization.

The separate 50,000-book, 2,000,000-segment profile built and audited all requested segments, then returned PARTIAL with BudgetExceeded during the measurement phase after 3600.365813707 seconds. That report retained only the phase name; the exact internal substage and exception message were not recorded. The elapsed time supports the phase deadline as an explanation, but it is not a recorded substage attribution.

Those observations justify examining duplicated computation. They do not prove the speed of this change. The 37-result page size, 3,600-second phase allowance, source checks and complete traversal requirements remain unchanged. A future page-size-100 measurement would be a separate declared workload and cannot replace or relabel the previous result.

Full verified four-profile evidence: Git blob a632d4ef5f71b4c8d5364ef8c33e12128cadab6a. Compact measured summary: 317971359e6be78a9e885625e2b905c9b9c6334d.

## Regression obligations

The new whole suite is tests/search/test_paged_candidate_work.py: 30 statically enumerated cases in 13 test functions. It uses actual LibraryApp imports, retained UTF-8 originals and SQLite connections; it does not substitute a second search engine.

The fixtures require two distinct fresh read connections, the actual hidden 38th-result attestation, one shared candidate index for the first page37 operation, and no shared index or maps in the next public operation. They verify actual connection closure, exact Unicode raw-span equivalence, complete-row rebuilds, corruption of a nonmatching row, changed/missing originals, current collection membership, schema/workspace changes, cancellation during the second SQL read, current callback ownership, bounded normalization memory and cleanup before handle publication.

All existing paging, literal integrity, read-only, retained-source, scoped query-work and other search suites remain required. No existing negative assertion or source contract is removed.

## Qualification still required

Compile and run the new whole suite and the existing search regressions on the selected source and isolated installed package. Before a new corpus run, include carr_library.search.literal in the benchmark's before/after module-origin proof together with the current application and paging modules. Keep the new run's source head, harness identity, environment, workload, phase timings and partial results separate from the original C3 evidence.

An actual performance run is still required. This patch makes no sustained-ingestion, two-million-result completion, Windows, native accessibility or release-capacity claim.

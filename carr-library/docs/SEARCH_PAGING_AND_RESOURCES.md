# Search pages, complete relevance ranking and resource allowances

## Use the search controls

The library Search tab and Research Search Center keep the current selected
library, topic, collection and source filters. The Search Center additionally
lets you explicitly check multiple libraries. Opening it checks only the
current library. It does not change the desktop's active scope or notebook.

Exact phrase, whole-word, all-words and advanced Boolean searches visit a
bounded window of source pages or text segments in source order. **Next result
page** continues matches in that window. **Next source window** visits the next
authorized sources. Empty matching windows can still have further sources.
The thematic and semantic choices instead build a complete relevance order for
the selected scope and use **Next result page** to traverse that order. The
status identifies which of these two behaviors is in use.

Search Center groups results by their source library. Its library navigation
visits the next checked libraries; it never silently adds an unchecked library.
Relevance scores are computed within each selected library and its current
filters. A library outside that scope contributes neither documents nor BM25
statistics. There is no cross-library relevance score or automatic corpus merge.

The native forms use standard labeled controls in scrollable viewports. Tab
moves between controls, Enter in the query starts the existing background job,
and Space activates a focused button or checks a focused library. Cancellation
uses that same job. Automated native tests and any manually performed screen
reader review are reported separately.

## Page sizes are not total corpus limits

| Value | What it bounds | How to continue |
| --- | --- | --- |
| 20 libraries | One selected-library page in Search Center | Next selected libraries |
| 500 editions | One internal catalog read | The service advances its catalog key |
| 100 source pages or segments | One source-order working window | Next source window |
| 100 results | One application result page | Next result page |
| 2,000 action handles | The current ephemeral result window | Later result pages issue fresh handles |
| 500 semantic candidates | One model scoring batch | The same ranker scores subsequent batches |
| 16 texts | One native embedding call | The existing ranker subdivides the scoring batch |
| 128 source-history entries | Retained backward navigation | Forward traversal continues; a new Search restarts |

These values retain finite working operations. They are not promises that a
specified number of books fits every machine. A search still stops when its
explicit time, memory or disk allowance is exhausted, or when its authorized
source scope changes. It does not label a truncated ranked prefix as the
complete result set.

The catalog's existing edition list also has forward keyset navigation. Its
visible page does not limit the number of editions that can be stored or found.

## Choose an allowance for the operation

Both search forms expose a cooperative time budget, working-memory allowance
and temporary-disk allowance. The defaults are 300 seconds, 64 MiB and
2,048 MiB. The time may be greater than zero and at most 86,400 seconds, memory
may be 16 MiB through 16 GiB, and disk may be 16 MiB through 1 TiB. Invalid,
nonfinite or out-of-range values are rejected before searching.

The memory value controls bounded source windows and SQLite working buffers.
It is not a hard process RSS limit: Python, the existing native model and the
host application also use memory. The disk value bounds the search-owned
database and order allocation policy. It is not an operating-system quota over
all SQLite temporary files or unrelated processes. Choose an allowance suited
to the machine and retain free disk space for other application work.

The deadline and cancellation checks cover source scanning, native SQLite
progress, window partitioning, model batches and result materialization.
Existing model and source operations keep their own admission and cancellation
rules. Increasing the overall search budget does not disable those rules.

## Complete thematic and semantic ordering

Complete ranked search builds a disposable selected-scope FTS index and a
private disk-backed order. It streams authorized source rows instead of
loading the whole candidate set into a Python list. FTS statistics include the
complete current selected scope. Unchanged derived index rows can be reused;
new, changed or revoked source rows update or leave the index.

Every new search still checks the current source inventory. Reusing the index
does not make its bytes authoritative or remove current-source validation.
Changed, malformed or unrecognized derived state is rebuilt. Result paging
uses the exact completed SQLite handle retained by the search owner and
rechecks each returned row against the current catalog and source authority.

Semantic search uses the existing explicit local model, its verified assets
and the existing cache. Long source text is partitioned into contiguous raw
Unicode spans with bounded tokenizer input and final 128-token windows. The
default scan chunk is 16,384 characters; explicit internal callers can choose
a bounded chunk up to 200,000 characters. There is no total 500-window cutoff.
Combining sequences and joined emoji are not knowingly cut at an internal
window boundary. Returned quotations still resolve to exact original spans.

Model scores are merged across bounded candidate batches before the complete
order is exposed. Lexical and semantic orders use the existing reciprocal-rank
fusion and source-diversity rules. An unavailable or inapplicable model follows
the existing explicit keyword fallback policy and reports it in status.
These mechanics do not establish relevance quality for an owner's corpus.

## Cursor lifetime and source actions

Cursors are opaque objects issued by the exact live library/search service.
They bind the query, options, scope and resource settings. Copying serialized
fields or creating a look-alike object does not create a usable cursor.

Each page reauthorizes its library, topic and source filters. A database commit,
replacement or changed source/model identity invalidates the old cursor. Start
a new Search to get a current order. The service does not silently continue a
mixture of old and new database snapshots.

Quotations, citations, context and saves still re-resolve the selected source
and exact revision. A page's result handle is not independent authorization to
read another source. Unverified extraction remains visibly unverified and
cannot acquire verified-copy authority merely by appearing in ranked results.

There is one search owner per LibraryApp. It serializes queries and owns its
observer, private result databases and cleanup. Close and data-root changes
withdraw further search admission. A root replacement is refused while actual
old search cleanup is pending. Application shutdown retains failed cleanup
without a widget callback or another search executor. Completion is reported
only after the retained database handles and private directories are released.

## Disposable state and restore

The root `search-cache/` directory is derived state. It is excluded from backup,
alongside the existing semantic cache, and may be rebuilt from authorized
originals and current catalog rows. Restoring a backup never restores a live
cursor, private ranked result handle or model execution permission.

The existing bookmark, evidence, source and library records keep their own
storage and backup contracts. Search pagination introduces no replacement
source store.

## Evidence and current limits

The focused source and isolated-installed tests cover exact raw quotations,
607-source multi-batch semantic ordering with deterministic test vectors,
complete selected-scope BM25 parity, foreign-library exclusion, catalog changes,
opaque cursor ownership, cancellation and retained cleanup. POSIX substitution
tests use owned real children and actual FIFO/symlink races.

A separate test uses the existing pinned model on 607 actual retained source
segments, observes real 500+107 scoring and at-most-16 embedding calls, traverses
all result pages, and checks cache reuse without further embedding. Its actual
execution result is tracked independently from deterministic-vector tests.
Neither test is a held-out relevance-quality evaluation.

The [initial common-gate run](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46078435015)
completed with fixture failures: Linux source and installed each had 883 passes
and 18 failures; Windows each had 881 passes, 18 failures, one teardown error
and one warning. The corrections retain the production search bytes and fix
old timeout assumptions, conflicting work metadata, the native event double,
and actual fixture disposal. Their exact rerun remains pending. The separate
native keyboard proof and pinned-model case are also **NOT_RUN or pending** until
their own Actions evidence is recorded. No 1,000-, 10,000- or 50,000-book throughput,
peak-RSS, latency percentile or model-quality qualification is implied by these
contract tests. The separate corpus benchmark must report the actual imported
original bytes, selected scope, source count, cold/warm phases, disk and memory
observations, and any cancellation or budget stop.

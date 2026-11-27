# Persistent local semantic cache

The desktop and command-line library automatically reuse exact MiniLM query
and source-window embeddings after the first successful inference. Reopening
the same library preserves this acceleration. No model, source, or query is
sent to a service. The cache does not change quotations, retrieval thresholds,
or source permissions.

## Data and authority

The cache lives in the private data directory under `semantic-cache`, outside
the sealed program installation. It contains normalized 384-dimensional numeric
vectors, hashed identities, checksums, and access timestamps. It stores neither
source text nor query text. These vectors are nevertheless private derived
data; they are not anonymous and should not be shared as a public index.

A record identity includes the exact approved model manifest, FastEmbed runtime
and preprocessing contract, exact text digest, and the current workspace,
edition, revision, and window identifiers. Changed source bytes or revision
identities require fresh inference. Query and source records have separate
identities. Before using a vector, the search still authorizes the current
database snapshot, verifies the original source, and checks complete tokenizer
coverage. Cache membership cannot add a result to the caller's candidate list.

The implementation uses only the existing Python/SQLite runtime. It introduces
no dependency, database migration, downloadable asset, or external request.
See the upstream [SQLite transaction documentation](https://www.sqlite.org/lang_transaction.html)
and [page-count budget](https://www.sqlite.org/pragma.html#pragma_max_page_count).

## Capacity, interruption, and recovery

The default budget is **50,000 records and 256 MiB per model cache file**.
`CacheBudget(max_entries=..., max_bytes=...)` supplies explicit validated programmatic
budgets. Reads and writes accept at most 512 vectors per operation; inference
continues in batches of 16 texts. The SQLite memory cache is bounded to 2 MiB,
memory mapping is disabled, and there is no growing WAL. A small rollback
journal can exist during a write, so the file budget is not a promise that the
whole directory occupies exactly that many bytes at every instant.

Recently used records are retained when the entry or file budget is reached.
An oversized incoming batch keeps its most recent portion. Each publication is
transactional. An interrupted write leaves the preceding committed records
available after reopening. A busy, corrupt, linked, or unrecognized cache is
ignored and the normal local inference path is used. Corrupt vector records
are individually recomputed. No research database, original, note, or evidence
record is changed during this recovery.

The cache is disposable and is not required in a research backup. Restoring a
library can recreate vectors on its next semantic search. To discard it, close
the application and remove only that library's `semantic-cache` directory.
No command automatically deletes or overwrites an unrecognized cache file.

## Verification and limits

The focused tests cover exact reuse after reopen, changed query/text/revision,
token-coverage checks on cache hits, cancellation, stale-scope exclusion,
corruption, malformed vectors, links and schema changes, capacity eviction,
competing writers, and actual child termination before commit. The installed
MiniLM workflow additionally performs genuine inference on the cold search,
then requires a reopened search to succeed with the embedding engine forbidden
from running. It revokes collection membership and checks fresh access again.

The recorded [scale evidence](evidence/semantic-cache-scale.json) uses real
SQLite files and deterministic synthetic vectors. All 1,000, 10,000, and 50,000
records survived reopening and were returned exactly. At 50,000 records the
file occupied 239,153,152 bytes; measured child peak RSS was 37,478,400 bytes.
The measurements describe storage behavior on the recorded Linux host, with
the operating system page cache in use. They do not measure neural inference,
search quality, cold-disk speed, or a 50,000-source end-to-end search.

The current semantic source-window and search-result budgets remain separate
constraints. This feature removes repeated embedding work; it does not by
itself satisfy every V3 requirement for large-library pagination or indexing.

The portable `scripts/benchmark_semantic_cache.py --output-dir NEW_DIRECTORY`
repeats the three storage sizes in isolated child processes, records source and
harness identities, and fails if counts, bytes, integrity, or exact values do
not match. `--installed` selects the installed implementation. An existing
output directory is never overwritten.

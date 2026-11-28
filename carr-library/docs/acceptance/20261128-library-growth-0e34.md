# Measured library growth pilot: 100 complete, 1,000 incomplete

[Run 46114336553](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46114336553)
executed source `0e34a92908dbd5e8271fee71837e5b57859a653c`, preserving all
1,357 files from ordinary source `95d305d527f8fa4f36202d40fc069d71f2551069`
and adding only the reviewed pilot harness, its tests and its workflow.

The complete 100-book scenario passed. The 1,000-book scenario reached its
900-second process-tree deadline during paged search and remains incomplete.
No 1,000-book capacity qualification is granted by successful import alone.

## Actual environment and corpus

The hosted Windows Server 2022 VM exposed four logical CPUs and approximately
16 GiB RAM. The report records its exact hardware, image, CPython3.11.9 and
SQLite3.45.1 identities. All341 installed Python files matched the exact
checkout;25 strict harness controls passed without skips.

Each generated UTF-8 TXT original contains40 real form-feed segments and4,519
bytes. Books enter through public `LibraryApp.add_text` / `import_file`,
retained originals, ingestion, catalog publication and FTS indexing. There is
no SQL fixture seeding. The100 and1,000 scenarios use independent directories
and fresh child processes. Timings are observations from these small generated
books, not a promise for scanned or large production documents.

## Measured results

| Observation | 100 books | 1,000 books |
| --- | ---: | ---: |
| Actual imported books | 100 | 1,000 |
| Exact source segments checked | 4,000 | 40,000 |
| Original input bytes | 451,900 | 4,519,000 |
| Sum of public import-call times | 12.845 s | 548.237 s |
| Import p50 / p95 | 0.125 / 0.165 s | 0.548 / 0.979 s |
| Completed 100-result search windows | 40 of40 | 270 of400 |
| Exact distinct source matches visited | 4,000 of4,000 | 27,000 of40,000 |
| Sum of completed search-call times | 47.034 s | 335.690 s |
| Search-window p50 / p95 | 1.165 / 1.216 s | 1.232 / 1.335 s |
| Sampled reading-service open p50 / p95 | 0.0151 / 0.0159 s | 0.0156 / 0.0188 s |
| Overall parent-observed process time | 63.069 s | 900.080 s |
| Final status | PASS | TIMEOUT; incomplete |

All p50/p95 values use nearest rank of the actual samples. The root reviewer
recomputed all six import/search/open summaries from the saved sample arrays
and obtained identical counts, sums and quantiles.

The1,000-book import phase ended at549.498 seconds. Verification of all retained
originals, all40,000 segments and index/catalog counts, service reopen/open,
workspace refusal and three real cancellation/retry scenarios finished by
552.691 seconds. The last sealed checkpoint records270 search windows at
899.822 seconds. The remaining130 windows and the final post-search corpus
audit did not complete.

Both scenarios completed three cooperative inline-TXT cancellations after a
committed checkpoint, deleted the external original, reopened the service and
explicitly retried through the public ingestion path using retained bytes.
These are measured retained-source recovery cases. Native UI cancellation and
isolated-worker startup were not measured.

The parent reported both child trees reaped and no cleanup error. The1,000
child has no final case report; its last checkpoint remains `IN_PROGRESS`.
Progress evidence is not promoted to a completed scenario.

## What the measurements support

Import time grows substantially with corpus size. Both observed scoped locator
EXPLAIN plans scan the page table even when supplied an existing revision.
This supports reviewing direct revision predicates against the current schema,
while retaining the unfiltered global integrity branch.

Per100-result search window remains about1.2 seconds at both scales. The
measurements do not establish that SQL dominates that latency; result
admission, source hashing and attestation still execute. Any direct-key query
rewrite and the later fresh-path correction need their own run against the
same real fixture. No validation, cancellation, source or deadline requirement
is removed to improve these numbers.

## Saved primary evidence

The [machine evidence](20261128-library-growth-0e34.json) includes the full
100-book report, the exact final1,000-book checkpoint, all observed sample
arrays, source/hardware identities, outcome reports and member verification.
Its SHA-256 is `ee9e2a3fde1b863fd04018d850a656a3f23f18e3bd94ad5963dfd055517c7fb9`.

Artifact `15252706496` is3,427,639 bytes with SHA-256
`281e954c9c89e589eb219c012e2c17062cc8df9de5cccf1afcdeb042c59fca26`.
Fixed reader `f7f5de16777464368dc8910997e3f07ffb13dabc`,
[run46118964589](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46118964589),
completed successfully at07:01:27 UTC on2026-11-28.

All12 selected files /325,676 bytes verify by exact UTF-8 length and SHA-256,
with no missing chunks, duplicates or parse errors. The57 other archive
payloads are explicitly unread; their names/types/sizes are checked and the
whole ZIP is hash-bound. The archive's actual declared text total is9,210,543
bytes. The reader performs saved evidence readback, not application execution.

Cold filesystem caches, full application process restart, OCR, neural retrieval,
VRAM, native UI open/cancel, 10,000/50,000-book scale and multi-GB files remain
outside this measured fixture. The later ordinary source and any performance
correction receive separate evidence.

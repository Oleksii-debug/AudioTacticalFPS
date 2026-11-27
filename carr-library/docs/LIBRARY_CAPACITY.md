# Large-catalogue measurements and the query-shape fix

## Result and current evidence boundary

The catalogue/search query-shape fix is implemented in candidate commit
[51e55b376edd33f4977112b1ec2892b03c98e6ff](https://github.com/Oleksii-debug/AudioTacticalFPS/commit/51e55b376edd33f4977112b1ec2892b03c98e6ff).
It passed **210 source tests and 15 subtests**, including executed comparison
with the original SQL and the original fingerprint bytes. On the same retained
50,000-edition synthetic fixture, twenty first-page calls changed from
**8.065 / 8.912 seconds p50/p95** to **1.344 / 1.648 seconds p50/p95**.

The optimized 1,000- and 10,000-edition profiles completed their full result
traversals. **Completion of the optimized 50,000-edition, 500-page traversal is
not established by the currently retrieved evidence.** Its last observed durable
checkpoint covered 25 pages and 2,500 exact results. The executor transport then
became unavailable while the full measurement command was active. The final raw
receipt could not be retrieved, so elapsed time or anticipated completion is
not substituted for it.

The [observed execution record](evidence/library-capacity-observed-progress-20261127.json)
preserves the exact later values that were actually read. The earlier complete
[raw 1k/10k cases and 1,800-second limit](evidence/library-capacity-initial-20261127.json)
and [original source manifest](evidence/library-capacity-initial-source-20261127.json)
remain separate. This is V3-07 development evidence, not real-book capacity
qualification, a Windows/NVDA result or a neural retrieval benchmark.

## Exact source identities

| Phase | Executed local commit | Public commit with verified identical complete tree |
|---|---|---|
| Original synthetic construction/read profile | `c32b6c3ab8d97a84d017551df3ca4db77215ea5b` | `65a25db77d92e3130263b4d854761093558ba038` |
| Baseline verified-fixture read profile with checkpoints | `62c7d802522ebdf37b886e7efd9cf778d84fd862` | `26f5a64df0126dc0459c9865617fdebf78df5f79` |
| Optimized production queries | `d914fc65585b452e71a0d180042454cb473b1f41` | `51e55b376edd33f4977112b1ec2892b03c98e6ff` |

The corresponding complete tree identities were verified as
`3b10674ac97363315c71107a353384aeec3cf2a5`,
`30e1fc9be92115958549af2803e834e59812128f`, and
`deb0df64c31cb9dca87a9586e23ccfbf4a201596`.
The production baseline before the query fix was
`498bf205c5313caf216f75d7006465267cc801e8`.

The harness binds every Python file under `src` and `bootstrap`, its reused
search fixture, the RSS helper and itself by SHA256. Each case checks this
identity before and after execution. The original manifest remains committed.
The optimized source manifests and completed 1k/10k case files were written
locally before the transport failure. The 50k case had only progress checkpoints
at the last observation. Only the completion/checkpoint summaries shown here
had been retrieved when access was lost.

## Completed original profiles

These are complete results from the original production queries. All timing
columns are seconds, except the separately labelled action table.

| Synthetic editions | Whole case elapsed | Direct fixture construction | First page p50 / p95 | Traversal page p50 / p95 | Process peak RSS MiB | Sampled fixture-file peak MiB |
|---|---:|---:|---:|---:|---:|---:|
| 1,000 | 5.68 | 0.41 | 0.073 / 0.088 | 0.072 / 0.117 | 39.95 | 2.99 |
| 10,000 | 84.75 | 17.21 | 0.487 / 0.627 | 0.496 / 0.643 | 42.70 | 27.03 |

The full traversals returned exactly 1,000 and 10,000 results in 10 and 100
pages, with every edition ID in order and every quotation/workspace/verification
flag checked. Each case also completed five fresh-process reopen/continuation
and original-source checks.

| Synthetic catalogue size | Context open p50 / p95 ms | Application reopen p50 / p95 ms | Separate 20-file TXT imports p50 / p95 ms | Cancellation response ms |
|---|---:|---:|---:|---:|
| 1,000 | 1.49 / 1.93 | 85.88 / 107.82 | 7.94 / 8.82 | 0.432 |
| 10,000 | 1.72 / 2.45 | 249.97 / 383.24 | 6.35 / 7.25 | 0.475 |

The import column measures actual `LibraryApp.import_file` calls on twenty
unique small TXT files in a separate initially empty library. Each twenty-file
sample contains only 2,370 input bytes. It is not import speed into the large
catalogue and is not large-book throughput. The construction column above
measures the synthetic registration/revision publisher, not file intake.

Cancellation is one timer-triggered observation per size, measured from the
actual cancellation event to the exception. The previous selection remained
usable for verified source opening/copy, and a fresh search recovered. It is
not a cancellation percentile or native-window response measurement.

## Original 50k limit and preserved partial baseline

The original combined construction/read case reached its configured
**1,800-second timeout**. A separate read-only observation confirmed that the
retained database contained 50,000 measured editions and segments, plus one
edition/segment in the isolation-control workspace. The SQLite file occupied
**124,993,536 bytes**. Construction elapsed time, process peak RSS and complete
search timing for that interrupted case were not returned and are not invented.

The same committed fixture was then reused without reconstruction under an
explicit 3,600-second read-profile bound. Twenty first-page calls measured
p50/p95 **8.064800679 / 8.911887348 seconds**. Its first 25 continuation pages
measured p50/p95 **7.964986451 / 9.449401839 seconds**, with 2,500 exact results.

After a durable checkpoint at **50 pages / 5,000 exact results**, only that
owned measurement process was stopped (driver exit 130) to implement the
reviewed performance fix in an isolated checkout. Its status is
`INTERRUPTED_FOR_REVIEWED_PERFORMANCE_FIX`, not a full pass. Source identity
was checked unchanged after interruption. The checkpoint elapsed time was
582.378876614 seconds. This preserves the slower baseline and its exact limit;
it does not turn partial search into full 50k acceptance.

## What changed and why

Every result-page request still reconstructs a complete fresh fingerprint of
the selected catalogue and source identities. With 50,000 one-segment editions
and 500 result pages, that is about 50 million edition/page rows hashed across
the traversal, in addition to other samples. The fix preserves that freshness
check and its source/cancellation/resource contracts.

Two query shapes added avoidable work:

- `catalog.read_views.browse_editions` expressed optional ID continuation as
  `(:after IS NULL OR e.id > :after)`. The inspected plan used only the
  workspace portion of the existing key. The candidate adds fixed,
  parameterized `after` and `edition_id` predicates only when supplied, so
  SQLite can seek on the existing `(workspace,id)` key. Author order, filters,
  nullable values and snapshot requirements are preserved.
- The generic `active_page` view joins the active table by revision alone.
  The fingerprint issued that query once per 500-edition batch. On the exact
  current schema, the inspected plan used an automatic covering revision index
  and an additional sort. `search.pagination._fingerprint` and
  `_source_batch` now express the existing workspace/edition/revision joins
  directly and use the existing scoped keys. They retain the same selected
  identity/metadata fields and ordering. No schema migration or new index was
  introduced.

The plan comparison used an empty in-memory copy of schema V5, one workspace
and no editions/pages; it did not open or stress the active benchmark database.
A plan observation alone is not performance or semantic qualification. The
actual source tests and measured candidate queries provide that separate
evidence. The total traversal still includes fresh whole-scope verification
per page; it is not described as a single linear scan.

## Executed candidate regressions

The final passing checks comprise:

- 46 catalogue comparison cases covering each filter, combined filters,
  nullable `after`/`edition_id`, exact IDs, literal punctuation, author order
  and separate workspaces with matching edition IDs.
- 18 comparisons of the complete fingerprint bytes and counts with the original
  SQL, including filtered and empty scopes.
- Two active-revision changes, in an early and a later catalogue batch,
  proving that old cursors are rejected without withdrawing completed selections.
- All 49 existing pagination regressions, covering bounded continuation,
  stale cursors, workspace isolation, source changes, cancellation and budgets.
- Existing catalogue, metadata, application, search-operation-budget and
  retained native PDF/TXT source-integrity checks.

The final totals are 210 passing tests, 15 passing subtests, zero failures and
zero skips. Native PDF subprocess checks used absolute project/source
`PYTHONPATH`. These are Linux source executions; Windows and NVDA acceptance
remain separate evidence. The successful terminal counts and exact source
identity are recorded even though the local XML files could not subsequently
be retrieved after transport loss.

## Completed optimized profiles and observed 50k progress

| Synthetic editions | Whole case elapsed seconds | Traversal page p50 / p95 seconds | Completed pages | Exact results | Status |
|---|---:|---:|---:|---:|---|
| 1,000 | 6.62 | 0.078 / 0.359 | 10 | 1,000 | PASS |
| 10,000 | 63.24 | 0.281 / 0.459 | 100 | 10,000 | PASS |

The 1k p95 was higher in this shared-host run, so the evidence does not claim
uniform improvement at every size or on every request. Raw values and their
scope matter more than a single speedup label.

The optimized 50k read profile reused the same retained database, with twenty
first-page samples, no search-page cap and a declared 3,600-second case bound.
Its observed first-page p50/p95 was **1.344231085 / 1.648350608 seconds**.
Its last retrieved durable traversal checkpoint covered **25 pages / 2,500
exact results**. Final 500-page completion, final memory/disk statistics and
fresh-process recovery receipts must be recovered or executed before being
reported as passed.

## Reproduction and measurement method

From a source checkout with normal development dependencies, including pytest:

```sh
python -B scripts/benchmark_library_capacity.py \
  --output-dir NEW_DIRECTORY \
  --sizes 1000 10000 50000 --samples 20 --case-timeout 3600
```

To measure an already committed synthetic fixture without rebuilding it:

```sh
python -B scripts/benchmark_library_capacity.py \
  --output-dir ANOTHER_NEW_DIRECTORY \
  --reuse-fixture EXISTING_FIXTURE_DIRECTORY \
  --sizes 50000 --samples 20 --case-timeout 3600
```

The output directory must be new. Reuse mode verifies the fixture's workspace,
source digest, exact catalogue IDs and row counts, starts the read measurement
again, and explicitly records construction as unmeasured. It never removes the
reused fixture. Fresh completed fixtures are removed only after their case JSON
is flushed, unless `--keep-fixtures` is selected. Progress checkpoints record
actual measured state and retain an incomplete status until checks finish.
Positive `--max-search-pages` values are explicitly partial; zero means the
entire result traversal.

The corpus publisher is reused from
`tests/search/test_search_pagination.py:publish_text_corpus`, with actual
registration/revision transactions of at most 1,000 editions. Each measured
workspace has distinct work/edition/revision identities, one source segment
per edition and **one shared retained 94-byte UTF-8 original**. A separate
workspace supplies one isolation-control edition. This isolates catalogue
breadth and does not simulate thousands of distinct book files or long chapters.
The older `benchmark_semantic_cache.py` supplies the RSS helper; its embedding
records remain a different population, not library capacity evidence.

Catalogue pages call the production `browse` service at a limit of 500,
including a terminal empty page. Literal search calls `search_page` with
100 results per page. Context and copy call their normal retained-source
verification boundaries twenty times. Open timing means resolving context,
not launching an external reader or painting a native window. Five fresh
processes reopen and continue a cursor, verify source/copy, and reject an old
process selection capability.

Statistics use nearest-rank p50/p95 with raw durations in completed receipts.
Five-sample reopen p95 is the observed maximum. Whole-child peak RSS includes
setup/validation and measurement overhead; fresh-process restart peaks are
separate, not summed concurrent memory. Fixture-file bytes include inputs,
originals, databases and journals. The 50 ms sampled peak can miss shorter
transients and is a lower bound.

The declared host is Linux, AMD EPYC 9V74, nine visible/affinity CPUs,
Python 3.12.14 and SQLite 3.53.1. Fixtures use `tmpfs`, which consumes host
memory and does not represent Windows SSD/HDD performance. Reopens and repeated
reads use the warm OS page cache. The host is shared; no exclusive-load claim
or invented performance target is made. No GPU/VRAM measurement is involved.

Real authorized book diversity, millions of segments, thousands of result
pages, multi-gigabyte sources, large-book import throughput, EPUB/PDF reader
interaction, neural ranking and actual NVDA use need their own measured
qualification. These synthetic records do not substitute for those scenarios.

## Standalone installed Windows capacity gate

The workflow `../../.github/workflows/carr-capacity.yml` runs on
`windows-2022` with CPython 3.11 x64. It is available for manual dispatch
and new `carr/v3-acceptance-*` or `carr/capacity-*` push branches.
Its preparation does not establish a Windows result: **the new installed mode
and this workflow remain NOT_RUN until an actual successful run supplies all
receipts**.

The gate builds one wheel from the exact checked-out candidate, installs it in
a fresh environment, and runs the existing harness with isolated installed
Python:

```powershell
& $installedPython -I -B -X utf8 scripts/benchmark_library_capacity.py `
  --installed --output-dir NEW_DIRECTORY `
  --sizes 1000 10000 50000 --samples 20 --max-search-pages 0 --case-timeout 3600
```

Both `carr_library` and `bootstrap` must resolve inside that environment.
Every installed Python file is compared with the candidate checkout and wheel.
The source manifest also binds the exact corpus fixture, RSS helper and harness.
Case and restart children inherit isolation and installed mode, and must return
the same source and module-origin identities. The receipt records the Git
commit/tree, workflow/run identity, wheel hash, installed file hashes, full raw
durations, memory, fixture disk and checkpoints.

The job is bounded at 90 minutes, its measurement step at 75 minutes, and each
fresh scale at 3,600 seconds. These are execution bounds, not performance targets.
All three fresh scales must complete: 10, 100 and **500** search pages with
1,000, 10,000 and **50,000** exact results. The validator also requires catalogue
coverage, twenty context/copy samples, observed cancellation with selection
preservation and source recovery, five fresh-process continuation checks, and
the separate twenty-file tiny-TXT intake measurement for each scale.
A query that completes before the cancellation request does not establish the
cancellation gate.

Timeouts, missing receipts, partial traversal, a changed source, failed probes,
or an unexecuted measurement produce a failing gate. The workflow retains raw
completed cases, partial progress, failure logs and source receipts with
`if: always()`; it never converts a partial checkpoint into a successful
50k result. Fresh completed fixture trees are removed only after their case JSON
has been flushed. Failed fixtures are not blindly deleted; the uploaded evidence
patterns omit database bodies while retaining diagnostic JSON and logs.

This workflow qualifies the declared synthetic installed service path only.
It does not establish real-book diversity, large-file import throughput,
native GUI/NVDA use, neural models, GPU capacity, or an overall release decision.

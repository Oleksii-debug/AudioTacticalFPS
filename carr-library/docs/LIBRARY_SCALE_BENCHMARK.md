# Real library corpus measurements

This tool closes the missing **measurement workflow** for the V3-07 targets of 1,000, 10,000 and 50,000 books. It does not certify that those targets have passed. Read the status, actual counts and source identities in a newly produced report.

The existing `benchmark_semantic_cache.py` measures rows of a synthetic embedding cache. Its historical results remain unchanged. The new `benchmark_library_scale.py` creates actual catalog editions, retained UTF-8 originals, canonical imported revisions and active source segments through `LibraryApp.add_text`. It uses the existing search cursors, source-bound quote actions and `ReadingDesk`; it does not populate SQL tables directly or change application limits.

## Run an explicitly chosen measurement

From a checkout with its normal dependencies installed:

~~~sh
python -B scripts/benchmark_library_scale.py \
  --output-dir /existing/local/parent/corpus-measurement \
  --sizes 1000 10000 50000 \
  --segments-per-book 20 --segment-bytes 2048 \
  --result-page-size 37 --repeats 9 \
  --phase-timeout 3600 \
  --max-disk-bytes 25769803776 --min-free-bytes 1073741824
~~~

The output directory must be new, its parent must already exist, and paths must be ordinary local paths without symbolic links or reparse points. The tool refuses existing output; it never overwrites, prunes, downloads models or deletes an earlier corpus. Keep the directory if you want to inspect the imported library afterward.

To measure the application installed in a chosen environment, use that interpreter and add `--installed`:

~~~powershell
& 'C:\chosen-runtime\python.exe' -I -B scripts/benchmark_library_scale.py --installed --output-dir 'D:\Measurements\Resonance-corpus' --sizes 1000 10000 50000 --segments-per-book 20 --segment-bytes 2048
~~~

`--source-root` selects an explicit source tree. `--installed` resolves the package in the selected interpreter and does not insert the checkout's source directory. `--expected-application-origin` additionally binds the exact imported `application.py` path, which is useful for source and installed-wheel acceptance.

For a small functional run before committing time and disk to a larger one:

~~~sh
python -B scripts/benchmark_library_scale.py \
  --output-dir /existing/local/parent/small-corpus \
  --sizes 3 --segments-per-book 51 --segment-bytes 128 \
  --result-page-size 17 --repeats 2 --min-free-bytes 0
~~~

That example crosses both the 100-source-segment window and result-page boundaries. Its success is evidence for the harness and those particular inputs, not a 1,000/10,000/50,000-book result.

## What is actually measured

Each requested size has an independent library. Build, complete-search traversal and restart probes run in three separately created and reaped interpreter processes. A Windows venv uses the same standard base-interpreter plus `__PYVENV_LAUNCHER__` pairing as the existing import worker, preserving the selected venv while the parent owns the actual interpreter process.

| Measurement | Actual operation and limit of the result |
|---|---|
| Import P50/P95 and total | One timed canonical import per generated book, including its normal retained-source/catalog/index publication. Fixture generation and later integrity scans are separately included in total phase time. |
| Startup | Timed `LibraryApp` construction in each phase. The report also records child bootstrap and parent-observed process wall time. |
| First result page P50/P95 | Repeated actual lexical searches in the selected workspace. The empirical nearest-rank method and actual sample count are explicit. |
| Last-book filtered lookup | Actual current title-filtered search for the last deterministic edition; no synthetic database shortcut. |
| Full traversal | Every source and result continuation is followed. A bounded bitmap checks all expected edition/segment pairs, duplicates and loss; every result must have the selected workspace and verified source state. |
| Source actions | A real quote action is checked on one result per nonempty result page. The report names this sample count; it does not claim every returned handle was separately copied. |
| Reader | Actual opening and last-page reads for up to 16 evenly spaced editions, checked against the exact original Unicode text. |
| Cancellation | A real search observes its cooperative cancellation callback; prior completed handles remain usable, a later query completes, and a cursor cannot cross to the excluded workspace. This does not simulate a machine crash. |
| Restart | A fresh interpreter reopens the same canonical library, revalidates every original and catalog identity, and repeats selected searches, reading and cancellation. It does not repeat the entire already measured result traversal. |
| Integrity | Every retained original is hashed before and after measured reading/search. A deterministic corpus digest binds edition IDs to original digests. Read measurements must leave the canonical database bytes unchanged. |
| Memory | Whole-process peak RSS through the unchanged platform measurement functions from `benchmark_semantic_cache.py`, with that file's SHA-256 and measurement method recorded. |
| Disk | Actual regular-file sizes and counts from a bounded traversal of each case directory, including the real database and retained originals. |

The isolated workspace contains one additional control book with the same query. It must never appear in selected-workspace results.

The default 50,000-book case contains **1,000,000 actual imported source segments** and approximately **2.05 GB of original UTF-8 bytes**, before SQLite indexes, catalog data and auxiliary files. These figures describe generated workload size, not a completed measurement. The three default sizes use separate corpora and therefore need more aggregate disk space than the largest case alone.

These are deliberately regular synthetic TXT documents. They exercise the actual library pipeline but do not represent the distribution of a real research collection, PDF/OCR extraction, damaged documents, multilingual retrieval quality or acoustic models. A corpus measurement should be supplemented with explicitly supplied representative material before making broader product claims.

## Budgets and honest partial outcomes

`--phase-timeout` is a cooperative deadline for each build/measurement/restart phase, with a direct-interpreter outer timeout. It is not one aggregate timeout for the entire command. The configured free-disk reserve is checked during work.

`--max-disk-bytes` is a **measured checkpoint budget, not a filesystem quota**. Full disk inventory is checked before importing, after each 64-book group and at phase boundaries. One group can grow the corpus before the next measurement. The tool records actual bytes and stops when the measured budget is exhausted; leave adequate headroom or use an external filesystem quota if you require a strict physical cap.

The script accepts at most 50,000 books, two million source segments per case and 8 MiB per generated book. These are harness bounds for this finite measurement, not new application ceilings. No product limit or source/cancellation guard is raised.

| Status | Meaning |
|---|---|
| `PASS` | All requested sizes and all three phases completed, exact counts/source identities matched, and the measured correctness controls passed. No release-capacity or GUI qualification is inferred. |
| `PARTIAL` | A deadline, cancellation or measured disk/free-space budget stopped the run. Actual completed import progress and phase reports remain. Later requested sizes are `NOT_RUN`. Exit code 2. |
| `FAIL` | A source/identity/correctness check, subprocess or other operation failed. Existing data remains for diagnosis; later sizes are `NOT_RUN`. Exit code 1. |

A process terminated before producing a valid phase report cannot contribute a passing phase. A complete report from a different imported application, different harness, changed measurement kernel, wrong PID or wrong corpus size is refused. The original canonical data is not repaired or silently adopted by the harness.

## Evidence files

The fresh output directory contains:

- `configuration.json`: exact chosen parameters and expected production/measurement source identities.
- One directory per requested size, containing its canonical `library/`, passive `corpus.json`, `build.json`, `measure.json`, `restart.json`, and per-phase stdout/stderr logs for phases that were attempted.
- `library-corpus-scale.json`: the overall status, requested/actual counts, individual phase measurements, source paths and SHA-256 values, Python/SQLite/platform details, method and explicit limitations.

A source hash binds the bytes actually used; it is not a digital signature. The report identifies six central imported production modules plus both measurement scripts. It is not a substitute for the complete release source/wheel provenance already enforced by packaging.

A fresh process is **not a cold-disk test**. The OS may retain cache pages from the build and earlier phases. P95 is an empirical percentile of the reported number of requests, not a statistically qualified population estimate or promised response-time bound.

## Regression and qualification state

`tests/packaging/test_library_scale_benchmark.py` exercises the actual small imported corpus, cross-window exactness, source corruption and hard-link refusal, cancellation, replacement of the selected directory, real child origin binding, no-clobber, measured partial outcomes and percentile accounting. The same test uses the actual imported parent application path: installed tests do not force repository source into child imports.

At source handoff, all newly added source/installed tests and all 1,000/10,000/50,000-book measurements are **NOT_RUN**. Run the focused regression suite before scheduling a larger measurement. Keep exact reports from the selected final source/runtime; earlier unrelated cache-row measurements and small test successes are never substituted for the requested corpus sizes.

# Real library corpus measurements

This tool closes the missing **measurement workflow** for the V3-07 targets of 1,000, 10,000 and 50,000 books. It does not certify that those targets have passed. Read the status, actual counts and source identities in a newly produced report.

The existing `benchmark_semantic_cache.py` measures rows of a synthetic embedding cache. Its historical results remain unchanged. By default, `benchmark_library_scale.py` creates actual catalog editions, retained UTF-8 originals, canonical imported revisions and active source segments through `LibraryApp.add_text`. That ordinary-import mode and its reports remain available unchanged. An explicit optional retained-fixture mode uses the existing catalog/revision writers for bounded setup, performs a full integrity audit, and then measures the same search, source actions and reader against the populated library. Fixture setup is reported separately from actual public imports; neither mode changes application limits.

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

`--source-root` selects an explicit source tree and its existing sibling bootstrap package; the actual bootstrap origin is validated and hashed. `--installed` resolves the package in the selected interpreter and does not insert the checkout's source directory. `--expected-application-origin` additionally binds the exact imported `application.py` path, which is useful for source and installed-wheel acceptance.

For a small functional run before committing time and disk to a larger one:

~~~sh
python -B scripts/benchmark_library_scale.py \
  --output-dir /existing/local/parent/small-corpus \
  --sizes 3 --segments-per-book 51 --segment-bytes 128 \
  --result-page-size 17 --repeats 2 --min-free-bytes 0
~~~

That example crosses both the 100-source-segment window and result-page boundaries. Its success is evidence for the harness and those particular inputs, not a 1,000/10,000/50,000-book result.

## Optional retained capacity setup and public samples

Use `--construction retained-fixture` when the question is how the current application behaves with a populated, fully retained library. This mode reuses `RetainedAssets.retain`, `Catalog/register_batch` and `Storage.stage_revision` under the existing exclusive writer and SQLite transactions. A transaction contains at most 250 books and 16 MiB of declared original text. No ingestion journal or successful public-import receipt is fabricated. A full `Storage.check()` and verification of every retained original must pass before a `corpus.json` receipt makes the fixture available to later phases.

The default remains `--construction ordinary-import`. Do not use fixture construction time as public import throughput or transfer a fixture result to the ordinary-import baseline.

~~~sh
python -B scripts/benchmark_library_scale.py \
  --output-dir /existing/local/parent/retained-capacity \
  --sizes 1000 10000 50000 --construction retained-fixture \
  --segments-per-book 20 --segment-bytes 256 --public-samples 12 \
  --result-page-size 37 --repeats 9 --phase-timeout 3600 \
  --max-disk-bytes 25769803776 --min-free-bytes 1073741824
~~~

The same `--installed`, `--source-root` and `--expected-application-origin` choices apply. Retained fixture mode defaults to 12 actual public samples; select `--public-samples 0` to run only setup, full traversal and restart. Explicit public samples must be between 3 and 20. Ordinary-import mode defaults to zero additional samples.

Declared capacity profiles are:

| Books | Segments per book | UTF-8 bytes per segment | Selected source segments | Selected original bytes | New-profile execution |
|---:|---:|---:|---:|---:|---|
| 1,000 | 20 | 256 | 20,000 | 5,139,000 | NOT_RUN |
| 10,000 | 20 | 256 | 200,000 | 51,390,000 | NOT_RUN |
| 50,000 | 20 | 256 | 1,000,000 | 256,950,000 | NOT_RUN |
| 50,000 | 40 | 256 | 2,000,000 | 513,950,000 | NOT_RUN |

Counts and byte totals in this table are generated geometry, not measured capacity results. They exclude the isolation control and the later public samples. The final row requires a separate run with `--sizes 50000 --segments-per-book 40`; 50,000 books with 20 segments contain one million segments. The shorter 256-byte segments are a distinct declared workload from the 2,048-byte ordinary-import baseline. SQLite indexes, journals and auxiliary state require additional disk space.

The optional `operations` phase runs after the baseline traversal and fresh-process restart. In a separate workspace in the same populated database it measures actual `LibraryApp.import_file` calls, exact retries, and refusal to replace an immutable edition with different source bytes. It checks returned source identities against sealed revisions, actual retained originals, current reading and durable checkpoints. These are bounded samples against a populated catalog, not sustained construction of the whole declared corpus.

The phase also creates one synthetic input for the existing `plan_import_batch/run_import_batch/resume_import` path. It requests cancellation only after observing at least two actual durable checkpoints, requires the owned worker and its resource lease to settle, removes that generated external input, then resumes the same retained request through a fresh `LibraryApp`. Its accepted checkpoint prefix, job, source, reconciled batch row and immutable publication receipt must survive. A further terminal resume must leave that publication receipt unchanged. This is cooperative process cancellation and recovery; it is not a power-loss test.

## Optional complete backup and independent restore

Add `--complete-backup` to request an actual final `backup` phase. It calls the current complete-library backup, verification and restore APIs. Omitting a profile preserves the ordinary product limits and existing report fields. For example, append the option to a small or 1,000-book retained fixture run after confirming the declared disk budget is sufficient.

Default admission records the observed catalog asset count, database bytes and original bytes against 10,000 referenced assets, a 512 MiB database and 8 GiB of catalog originals. A 10,000-book fixture already has an additional isolation original, so it exceeds the default asset count; a 50,000-book fixture does too. A requested backup outside its selected admission is `NOT_ADMITTED` and the measurement is `PARTIAL`, never a successful backup or restore.

The caller can explicitly select the supported `large-source-v1` product profile:

```text
--complete-backup --backup-capacity-profile large-source-v1
```

The profile option requires `--complete-backup`. It is never inferred from corpus size or a backup manifest. The harness passes that exact choice to creation, verification and independent restore, and records all selected allowances beside the observed admission. The product's profile provides finite limits: database 64 GiB, journal 32 GiB, originals 128 GiB, pending bytes 64 GiB, manifest 64 MiB, 100,000 originals, 100,000 jobs, 250,000 pending files and 2,097,152 checkpoints, with a 3,600-second journal validation allowance. These are admission ceilings, not a claim that those workloads have completed.

Both profiles retain all content, source, checkpoint and typed-state checks. The complete version-4 and pending version-1 formats are unchanged; no profile field is added to a backup manifest, and no manifest can grant a larger allowance. The harness does not modify product constants. A small backup created under the explicit larger allowance remains readable under the default bounds when its actual contents fit. Phase deadlines, measured disk/free-space budgets and their honest partial outcomes still apply. Large-profile backup/restore remains **NOT_RUN** until the exact selected implementation completes its actual measurement.

The phase reserves a conservative measured disk allowance for backup and a separate restore. After the real backup and verification succeed, it renames only its own synthetic source library to `retained-baseline-library/`. The prior absolute source root is absent when restoring to `restored-library/`; the baseline files are preserved. The restored catalog identity digest, every selected original and actual reader output must match. Any failure leaves the created diagnostic files intact. No existing external library is pruned or repaired.

## What is actually measured

Each requested size has an independent library. Build, complete-search traversal and restart probes run in three separately created and reaped interpreter processes. Optional operations and backup phases each receive another fresh interpreter. A Windows venv uses the same standard base-interpreter plus `__PYVENV_LAUNCHER__` pairing as the existing import worker, preserving the selected venv while the parent owns the actual interpreter process.

| Measurement | Actual operation and limit of the result |
|---|---|
| Ordinary import P50/P95 and total | One timed canonical import per generated book, including its normal retained-source/catalog/index publication. Text generation and later integrity scans are separately included in total phase time. Retained-fixture setup explicitly reports an empty import distribution with `NOT_MEASURED_BY_FIXTURE_CONSTRUCTION`. |
| Startup | Timed `LibraryApp` construction in each phase. The report also records child bootstrap and parent-observed process wall time. |
| First result page P50/P95 | Repeated actual lexical searches in the selected workspace. The empirical nearest-rank method and actual sample count are explicit. |
| Last-book filtered lookup | Actual current title-filtered search for the last deterministic edition; no synthetic database shortcut. |
| Full traversal | Every source and result continuation is followed. A bounded bitmap checks all expected edition/segment pairs, duplicates and loss; every result must have the selected workspace and verified source state. |
| Source actions | A real quote action is checked on one result per nonempty result page. The report names this sample count; it does not claim every returned handle was separately copied. |
| Reader | Actual opening and last-page reads for up to 16 evenly spaced editions, checked against the exact original Unicode text. |
| Cancellation | A real search observes its cooperative cancellation callback; prior completed handles remain usable, a later query completes, and a cursor cannot cross to the excluded workspace. This does not simulate a machine crash. |
| Restart | A fresh interpreter reopens the same canonical library, revalidates every original and catalog identity, and repeats selected searches, reading and cancellation. It does not repeat the entire already measured result traversal. |
| Integrity | Every retained original is hashed before and after measured reading/search. A deterministic corpus digest binds edition IDs to original digests. Read measurements must leave the canonical database bytes unchanged. |
| Memory | Whole-process peak RSS through the unchanged platform measurement functions from `benchmark_semantic_cache.py`, with that file's SHA-256 and measurement method recorded. During operations this is the supervising phase interpreter; the nested import worker's peak memory is explicitly `NOT_MEASURED`. |
| Disk | Actual regular-file sizes and counts from a bounded traversal of each case directory, including the real database and retained originals. |

The isolated workspace contains one additional control book with the same query. It must never appear in selected-workspace results.

The default 50,000-book case contains **1,000,000 actual imported source segments** and approximately **2.05 GB of original UTF-8 bytes**, before SQLite indexes, catalog data and auxiliary files. These figures describe generated workload size, not a completed measurement. The three default sizes use separate corpora and therefore need more aggregate disk space than the largest case alone.

These are deliberately regular synthetic TXT documents. They exercise the actual library pipeline but do not represent the distribution of a real research collection, PDF/OCR extraction, damaged documents, multilingual retrieval quality or acoustic models. A corpus measurement should be supplemented with explicitly supplied representative material before making broader product claims.

## Budgets and honest partial outcomes

`--phase-timeout` is a cooperative deadline for every requested phase, with the existing direct-interpreter outer timeout. It is not one aggregate timeout for the entire command. The public recovery route also uses its existing owned import-worker timeout and reserves time for cleanup. The outer supervisor's emergency termination owns only the direct phase interpreter; it is not evidence of process-family cleanup after a hard phase timeout. No passing recovery result is accepted without ordinary worker/lease settlement. The configured free-disk reserve is checked during work.

`--max-disk-bytes` is a **measured checkpoint budget, not a filesystem quota**. Full disk inventory is checked before ordinary importing, after each 64-book group and at phase boundaries. Retained setup checks after each bounded transaction, public samples after each completed sample, and backup between actual operations. One group or operation can grow the corpus before the next measurement. The tool records actual bytes and stops when the measured budget is exhausted; leave adequate headroom or use an external filesystem quota if you require a strict physical cap.

The script accepts at most 50,000 books, two million source segments per case and 8 MiB per generated book. These are harness bounds for this finite measurement, not new application ceilings. No product limit or source/cancellation guard is raised.

| Status | Meaning |
|---|---|
| `PASS` | All requested sizes and every selected phase completed, exact counts/source identities matched, and the measured correctness controls passed. No release-capacity or GUI qualification is inferred. |
| `PARTIAL` | A deadline, cancellation, measured disk/free-space budget or explicit backup admission refusal stopped the run. Actual completed progress and phase reports remain. Later requested sizes are `NOT_RUN`. Exit code 2. |
| `FAIL` | A source/identity/correctness check, subprocess or other operation failed. Existing data remains for diagnosis; later sizes are `NOT_RUN`. Exit code 1. |

A process terminated before producing a valid phase report cannot contribute a passing phase. A complete report from a different imported application, different harness, changed measurement kernel, wrong PID or wrong corpus size is refused. The original canonical data is not repaired or silently adopted by the harness.

## Evidence files

The fresh output directory contains:

- `configuration.json`: exact chosen parameters and expected production/measurement source identities.
- One directory per requested size, containing its canonical `library/`, passive `corpus.json`, `build.json`, `measure.json`, `restart.json`, and per-phase stdout/stderr logs for phases that were attempted. Selected optional phases add `operations.json` and/or `backup.json`. Successful independent restore preserves the old files in `retained-baseline-library/` and creates `restored-library/`.
- `library-corpus-scale.json`: the overall status, requested/actual counts, individual phase measurements, source paths and SHA-256 values, Python/SQLite/platform details, method and explicit limitations.

A source hash binds the bytes actually used; it is not a digital signature. Default reports identify six central imported production modules, the existing bootstrap package and both measurement scripts. The optional extension adds thirteen exact module origins covering registration, membership, source attestation, retained assets, ingestion, batch/worker/publication/writer ownership, resource admission, complete/pending backup and the selected backup-capacity validator: twenty imported module identities in total. It is not a substitute for the complete release source/wheel provenance already enforced by packaging.

Retain or upload only configuration, reports, finite text logs and source identities for a public measurement artifact. Generated libraries, original corpora, backup payloads, installed runtimes and executable binaries are not required report artifacts.

A fresh process is **not a cold-disk test**. The OS may retain cache pages from the build and earlier phases. P95 is an empirical percentile of the reported number of requests, not a statistically qualified population estimate or promised response-time bound.

## Regression and qualification state

`tests/packaging/test_library_scale_benchmark.py` exercises the actual small imported corpus, cross-window exactness, source corruption and hard-link refusal, cancellation, replacement of the selected directory, real child origin binding, no-clobber, measured partial outcomes and percentile accounting. The same test uses the actual imported parent application path: installed tests do not force repository source into child imports.

The companion `tests/packaging/test_library_capacity_measurement.py` contains 24 statically counted cases using actual retained files and SQLite, preserving its original 18 cases. They cover bounded seed transaction rollback, a rejected whole-library integrity audit, current cursor/reader exactness, independent phase origins, real public import/cancel/resume/checkpoint receipts, unchanged publication on retry, declined replacement, a forged returned source identity, retained-source corruption, current backup/restore, disk refusal, explicit options and entry cancellation. Six additional cases cover invalid or implicitly enabled backup profiles, a real isolated phase using the explicit profile, and forwarding to all three actual backup APIs while keeping the old manifest format. The existing 24-case benchmark suite remains unchanged. Both suites need the actual `benchmark_library_scale.py` and the unchanged neighboring `benchmark_semantic_cache.py` companion.

At this extension's source handoff, all its newly added source/installed regressions and retained-fixture profiles are **NOT_RUN**. Keep exact reports from the selected final source/runtime; prior baseline results, small regressions and cache-row measurements do not qualify the new profiles.

## Retained ordinary-import baseline

The baseline [run 46037427185](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46037427185) used source `fb39ed3cb1e23ffa3be8bd768c1dd64216f110da`, Python 3.11.18, SQLite 3.45.1, four reported logical CPUs and Linux x86-64. Its selected inputs had 20 segments per book and 2,048 UTF-8 bytes per segment. This source predates the optional construction mode and later query/publication improvements.

| Requested books | Reported completed imports | Outcome | Observed timings and scope |
|---:|---:|---|---|
| 1,000 | 1,000 | Build, measure and restart PASS | Timed imports 969.628494 s; import P50 0.981507 s, P95 1.847835 s. Full 20,000-result traversal 172.100855 s. Sum of phase wall times 1,155.645279 s. |
| 10,000 | 1,688 | Build PARTIAL | Build process wall time 3,602.749192 s against a 3,600 s cooperative phase limit; full corpus integrity not reached, measure/restart NOT_RUN. |
| 50,000 | 1,568 | Build PARTIAL | Build process wall time 3,601.449795 s against the same phase limit; full corpus integrity not reached, measure/restart NOT_RUN. |

The complete 1,000-book report was independently read from artifact `15223331300`, SHA-256 `22d16b19a4a29af7d29ac589ac4cff03902e03e7f30be715cf813ef8c9bb2283`, in [read-only inspection run 46049229528](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46049229528). The two partial progress reports were read from artifacts `15225356586` and `15226521241` in [read-only inspection run 46055539153](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46055539153), verifying exact archive sizes, SHA-256 values and source identity. Their partial builds did not retain a per-import latency distribution or a final full-corpus/disk audit. These results establish a measured ordinary-import construction bottleneck on that profile; they do not establish completed 10,000/50,000-book capacity or predict the unexecuted retained-fixture profiles.

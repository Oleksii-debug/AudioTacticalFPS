# Synthetic LibraryApp capacity baseline

This is an actual local measurement of **1,000 short synthetic TXT editions**
through the production `LibraryApp`, recorded on 27 November 2026. It establishes
a small, reproducible baseline; it does not qualify large academic books,
physical-disk throughput, 50,000 editions or multi-gigabyte libraries.

The complete [machine-readable report](evidence/library-capacity-1000-20261127.json)
contains source fingerprints, hardware/runtime details, corpus bytes, sample
counts, latency distributions, memory, disk footprint and recovery outcomes.
The measured source commit was
`19f334283a7c35a97b10e2757466bfc9561998c9`. The harness checked the committed
production Python tree before and after execution. Later integration commits
do not inherit this measurement automatically.

## Workload and environment

The primary corpus contains 1,000 unique UTF-8 files of 237 bytes each:
**237,000 source bytes**, one logical segment per edition, with deterministic
English and Ukrainian text. Each file went through `LibraryApp.import_file`:
the ordinary source snapshot, retained original, request/checkpoint journal,
catalog publication and source checks all remained enabled. The corpus was
created through public application APIs; no rows or indexes were seeded directly.

The execution environment reported Linux 6.18.57, Python 3.12.14, SQLite 3.53.1,
an AMD EPYC 9V74 processor label and nine available logical CPUs. Reported memory
was 10,451,427,328 bytes. The corpus lived on **tmpfs in `/dev/shm`**, with warm
filesystem caches. Reopening `LibraryApp` did not flush OS caches. The synthetic
corpus was removed after the run.

## Observed latency

All values below are milliseconds. Percentiles use nearest rank. These are
observations from this corpus and execution, not latency guarantees.

| Operation | Observations | p50 | p95 |
|---|---:|---:|---:|
| Import and publish one edition while growing from 0 to 1,000 | 1,000 | 29.34 | 51.22 |
| Reopen the persisted library and check its schema/integrity | 12 | 24.02 | 36.05 |
| Read one catalog page, up to 500 editions | 72 | 2.25 | 3.06 |
| Traverse the complete 1,000-edition catalog | 24 | 5.72 | 8.00 |
| Execute one search page | 128 | 30.33 | 37.24 |
| Complete one unique phrase query across both source windows | 64 | 60.78 | 79.61 |
| Open freshly verified quotation context | 64 | 1.71 | 2.24 |
| Revalidate and copy an exact quotation | 64 | 1.65 | 2.20 |
| Prepare a byte-exact original view copy | 64 | 3.64 | 4.78 |

The catalog-page distribution includes the final empty page used to detect the
end of an exact multiple of 500. The import distribution spans a growing corpus;
it is not a fixed-size steady-state throughput measurement. Twelve reopen
observations are particularly limited evidence for tail latency.

The 64 unique queries sampled sources from the beginning through the end of the
catalog. Every query traversed both bounded source windows, every result retained
its expected edition identity, and exact context/copy/original bytes were checked.
A separate common phrase returned all **1,000 distinct editions across 10 result
pages** in 2.128 seconds. The application retained the 500-edition and 100-result
per-page bounds. Each page used fresh authorization and cursor validation;
these pages are neither one simultaneous global snapshot nor a global relevance
ranking. The harness did not launch an external document viewer.

## Memory, footprint and interruption

The parent Python process reached a **31,985,664-byte peak RSS** (30.50 MiB),
measured with `resource.getrusage(RUSAGE_SELF).ru_maxrss`. This is a process
high-water mark, including the harness and production services. It excludes the
separate resume worker's RSS; no VRAM measurement applies to this workload.

After the recovery exercise, the private library contained 6,741,759 logical
file bytes and 13,369,344 allocated bytes across 2,068 regular files. This includes
the primary corpus, one additional 45-byte recovery source in a second workspace,
the two SQLite databases, ordinary request journals and 64 prepared source-view
copies. The report separates these categories. It is not a storage estimate for
long documents or extracted media.

A search was cancelled during actual catalog traversal, after its 64th
cancellation check. The existing API returned `TimeoutError` 0.119 ms after the
callback requested cancellation, and a previous exact quotation remained usable.
This is one cooperative-cancellation observation, not a worst-case shutdown bound.

A separate two-segment TXT import stopped after one durable checkpoint. After
reopening the library and removing the external input file, normal
`resume_import` completed the **same job** in 334.76 ms. The first checkpoint
digest was unchanged; retained source bytes and the final exact quotation were
revalidated. No unfinished result was labelled complete. The whole measured
exercise took 37.57 seconds.

## Reproduce and extend

Use a committed clean source checkout and a new report filename:

```bash
PYTHONDONTWRITEBYTECODE=1 TMPDIR=/dev/shm python scripts/benchmark_library_capacity.py \
  --work-parent /dev/shm --editions 1000 --queries 64 \
  --report /new/report-directory/library-capacity.json
```

The harness reuses the existing semantic-cache benchmark's platform RSS helper.
It creates one temporary synthetic data tree, preserves all application guards,
uses one explicit time budget and requires a new report file. It downloads no
documents or models. A 20-edition execution was used to check the harness before
the reported 1,000-edition run.

The next useful comparisons are the same declared workload on a real Windows
local disk, longer source texts, and separately specified PDF/OCR or semantic
workloads. None of those scenarios, native accessibility, concurrent writers,
ASR quality or audio rendering is qualified by this report.

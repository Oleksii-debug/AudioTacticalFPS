# Retained source-page measurements — 2026-11-27

## Result and scope

Linux source and installed measurements completed for actual retained corpora of
1,000, 10,000 and 50,000 logical pages. Windows source and installed measurements
completed the 1,000- and 10,000-page cases, then exceeded the existing 600-second
owned-child limit while **building** the 50,000-page corpus. There is no Windows
50,000-page query result or complete Windows aggregate report.

These are measured samples, with **performance acceptance NOT_ASSESSED**.
The configured three result pages per trial do not exhaust any tested corpus.
They do not establish native application, human accessibility, neural inference,
cold-disk, retrieval-quality, physical-device or large-PDF acceptance.

## Exact evidence

- [Measurement run 46059259121](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46059259121)
  tested commit `8cb61be3dfd9be2447737dafacb428d850be774d`.
  Its application source is the product parent
  `7471d0455549b5598248b4e51d67ad613f86f5ca`; the measurement commit only adds the
  explicit experiment workflow.
- [Read-only receipt verification 46062277884](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46062277884)
  checked exact artifact ID, run, head, archive SHA256 and bounded member parsing.
  It executed no application test.
- [Retained parsed receipts](evidence/source-pages-20261127.json) include every
  original member hash, all raw timing samples, corpus/configuration/runtime
  identities, complete Linux aggregates and both incomplete Windows receipts.
  JSON values are preserved in a new wrapper; member hashes identify the original
  artifact bytes, not the wrapper serialization.
- The production Python inventory in both complete Linux reports is
  `2c74f90eb6466c8ada372e67556cecb0ffc32a65241e78c309c74a38ef7068fc`.
  The receipt carries the harness and configuration hashes separately.

| Platform / import | Job | Artifact | Archive SHA256 |
| --- | --- | --- | --- |
| Linux / source | 140709243261 | 15228227464 | `1dd577683a9c242f376f27284d80c44b37529f47780564020614d511cd718844` |
| Linux / installed | 140709243329 | 15227738360 | `53a20719a1e5d968ee787a0f0a89b35169d49ea554ec7b8ffa4d7add5ccadee1` |
| Windows / source | 140709243181 | 15228013635 | `46eda90e4d8832b5d48dc8fba05a2f932be3f1beb6810b78066b2ef02a3239fe` |
| Windows / installed | 140709243113 | 15228407289 | `d2d08628cdb1794ad063466fc74987f7fc5f620534b5900aa1527e5011257c61` |

## Workload and interpretation

The corpus uses 512 characters per form-feed-delimited page, 128 pages per book,
and an exact query occurrence every seventh page. Each original is retained by
the actual `LibraryApp.add_text` path. Query settings are a 64-page candidate
window, 1,000,000-character budget, at most 20 results, three repetitions and at
most three result pages per repetition. Every returned result remains verified;
sampled source copying is checked separately.

Every `search_page` call validates the complete selected source snapshot.
That cost is included below. The bounded candidate count is therefore not the
total amount of source-validation work. Thematic ranking remains local to the
current source window.

Each measurement uses a fresh owned child; corpus setup uses another child.
The first-page distribution has three observations and continuation has six.
Percentiles use nearest rank, so these small-sample p95 values can be maxima.
OS caches are not flushed, and repeated calls retain their process.
RSS is whole-process high-water resident memory, including imports and inventory
verification, not a live allocation count for the search operation.

Linux runners report four logical CPUs and x86_64, Python 3.11.18 and SQLite
3.45.1. Their platform receipts identify different cloud-kernel descriptions.
A matching reported-hardware fingerprint is not evidence of the same physical
machine. Source-versus-installed timing differences cannot be attributed to
installation mode from this experiment.

## Actual sampled latency

All latencies are **seconds**; RSS is MiB (1,048,576 bytes). Each completed
measurement below reported zero search timeouts and zero exhausted trials.
The full raw values and per-call source-copy evidence are retained in the JSON.

| Platform | Import | Pages | Search | First p50 | First p95 | Continuation p50 | Continuation p95 | Peak RSS MiB |
| --- | --- | ---: | --- | ---: | ---: | ---: | ---: | ---: |
| Linux | installed | 1,000 | phrase | 0.057 | 0.065 | 0.057 | 0.059 | 34.023 |
| Linux | installed | 1,000 | thematic | 0.025 | 0.025 | 0.019 | 0.020 | 34.219 |
| Linux | installed | 10,000 | phrase | 0.165 | 0.176 | 0.160 | 0.167 | 36.645 |
| Linux | installed | 10,000 | thematic | 0.120 | 0.129 | 0.120 | 0.122 | 36.367 |
| Linux | installed | 50,000 | phrase | 0.644 | 0.658 | 0.641 | 0.648 | 36.504 |
| Linux | installed | 50,000 | thematic | 0.604 | 0.610 | 0.605 | 0.613 | 36.594 |
| Linux | source | 1,000 | phrase | 0.066 | 0.084 | 0.066 | 0.068 | 36.852 |
| Linux | source | 1,000 | thematic | 0.027 | 0.044 | 0.024 | 0.024 | 37.285 |
| Linux | source | 10,000 | phrase | 0.198 | 0.212 | 0.193 | 0.195 | 39.410 |
| Linux | source | 10,000 | thematic | 0.156 | 0.172 | 0.150 | 0.155 | 39.512 |
| Linux | source | 50,000 | phrase | 0.788 | 0.801 | 0.778 | 0.800 | 39.699 |
| Linux | source | 50,000 | thematic | 0.742 | 0.754 | 0.741 | 0.747 | 39.543 |
| Windows | installed | 1,000 | phrase | 0.114 | 0.191 | 0.117 | 0.123 | 34.156 |
| Windows | installed | 1,000 | thematic | 0.041 | 0.045 | 0.043 | 0.044 | 33.836 |
| Windows | installed | 10,000 | phrase | 0.331 | 0.343 | 0.330 | 0.336 | 37.230 |
| Windows | installed | 10,000 | thematic | 0.267 | 0.268 | 0.263 | 0.270 | 37.133 |
| Windows | source | 1,000 | phrase | 0.142 | 0.167 | 0.128 | 0.133 | 37.906 |
| Windows | source | 1,000 | thematic | 0.044 | 0.076 | 0.044 | 0.045 | 38.160 |
| Windows | source | 10,000 | phrase | 0.372 | 0.406 | 0.374 | 0.384 | 38.691 |
| Windows | source | 10,000 | thematic | 0.279 | 0.304 | 0.276 | 0.281 | 38.582 |

## Windows incomplete case

Both Windows jobs retain the same explicit incomplete outcome: two completed
sizes, followed by an `AudioError` from the owned child's unchanged time
allowance. The timeout happened in the 50,000-page **setup phase**, before its
phrase or thematic query measurement. The completed Windows source setup took
4.547 seconds for 1,000 pages and 57.703 seconds for 10,000 pages; installed setup
took 6.125 and 54.937 seconds respectively. These values describe those runs.

The receipts do not isolate the cause of the nonlinear setup cost. They do not
prove a query timeout, a memory leak, a device problem, or a Windows scale pass.
Further profiling or a separately declared measurement budget is required
before drawing a 50,000-page Windows conclusion; no application acceptance
deadline or guard has been changed.

See [the benchmark protocol](SOURCE_PAGE_BENCHMARK.md) for invocation,
identity checks, owned process cleanup and interpretation limits.

# Retained source-page measurements — 2026-11-27

## Result and scope

Linux source and installed measurements completed for actual retained corpora of
1,000, 10,000 and 50,000 logical pages. Windows source and installed modes now
also have completed 50,000-page query measurements from a separately declared
fresh experiment. The original Windows run completed its smaller cases and
exceeded its 600-second setup bound at 50,000 pages; that historical outcome
remains incomplete and is retained below.

These are measured samples, with **performance acceptance NOT_ASSESSED**.
The configured three result pages per trial do not exhaust any tested corpus.
They do not establish native application, human accessibility, neural inference,
cold-disk, retrieval-quality, physical-device or large-PDF acceptance.

## Completed Windows 50,000-page follow-up

[Run 46071323514](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46071323514)
completed successfully in source and isolated installed modes at commit
`87939e06609e133774c5b7361f345d028f0514db`. Both modes first passed
**19/19 benchmark contract cases**. They then created independent fresh
50,000-page corpora, with a declared 1,200-second setup-only allowance and
unchanged 600-second query-child and 60-second public-search budgets.

Both corpora have the deterministic content SHA256
`9da1e4d252251e444f2b964b07f31a50d645793c306614671a19b758728e1edb`.
The application source tree is `9e29c116a1db0644ff6a1a2ca4ed36a02e1f2e16`;
both full production Python inventories have SHA256
`bf94d2d8d5a3d739fefda55e4f7a2a8f88fd9f50a118f14f3d70dc2d88339eac`.
This is the named P4 measurement source, not a qualification of a later
application composition or a controlled comparison with the earlier Linux run.

All times below are seconds and RSS is whole-process peak MiB. Each measurement
contains three first-page and six continuation observations, **zero search
timeouts**, and zero exhausted trials. The configured sample bound does not
traverse the full result set. Small-sample nearest-rank p95 may be the maximum.

| Import | Search | Corpus setup | First p50 | First p95 | Continuation p50 | Continuation p95 | Peak RSS MiB |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| source | phrase | 695.798 | 1.436 | 1.442 | 1.423 | 1.443 | 38.621 |
| source | thematic | 695.798 | 1.327 | 1.339 | 1.306 | 1.319 | 39.031 |
| installed | phrase | 698.922 | 1.457 | 1.493 | 1.455 | 1.494 | 37.746 |
| installed | thematic | 698.922 | 1.395 | 1.397 | 1.394 | 1.406 | 37.660 |

| Import | Job | Artifact | Archive bytes | Archive SHA256 |
| --- | --- | --- | ---: | --- |
| source | 140748673508 | 15234155805 | 40,334 | `bec43ecf0d2e65fa31fbd12939f05b51e765fe042ca7336a3a4ebf325adb7c42` |
| installed | 140748673678 | 15233482059 | 40,617 | `37b513e16ab299f9c4154774d632c6ec5b09146687e49b991695b82e044148a4` |

The [capture manifest](evidence/source-pages-windows50000-20261127/capture-manifest.json)
and all 28 original members are retained byte-for-byte in this repository,
including the [source report](evidence/source-pages-windows50000-20261127/source/experiment-windows50000/source-pages.json)
and [installed report](evidence/source-pages-windows50000-20261127/installed/experiment-windows50000/source-pages.json).
[Read-only verification run 46074147848](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46074147848)
validated exact run/head/attempt, archive metadata/digests, bounded member
inventories and original-byte hashes without executing artifact contents.
Configuration, phase reports, the harness, all 376 Python source identities and
19 strict JUnit cases per mode were cross-checked. The 231,325 retained original
bytes preserve BOM/CRLF; the capture manifest is a new provenance wrapper.
Each fresh corpus contains 391 editions, 25,600,000 logical characters and
26,349,609 retained UTF-8 bytes. Setup peak RSS measured 103.980 MiB for source
and 102.391 MiB for installed; it is separate from the query peaks above.
The earlier 600-second Windows setup interruption below remains incomplete.
This separate successful experiment changes no application deadline and does
not establish performance acceptance, cold-disk latency, retrieval quality,
native interaction, neural inference or human accessibility.

The initial follow-up at `e2ed6814473fe0443a195d79264217579d420655`,
[run 46070047919](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46070047919),
stopped before corpus creation when an existing deadline fixture relied on a
1e-12-second real clock difference. Eighteen other cases passed in each mode.
The successful successor drives the real application's monotonic budget
deterministically, retaining actual corpus/search execution and every original
no-fabricated-result assertion. Search implementation and budgets are unchanged.

## Original experiment and exact evidence

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

## Original experiment: actual sampled latency

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

## Original experiment: Windows incomplete case

Both Windows jobs retain the same explicit incomplete outcome: two completed
sizes, followed by an `AudioError` from the owned child's unchanged time
allowance. The timeout happened in the 50,000-page **setup phase**, before its
phrase or thematic query measurement. The completed Windows source setup took
4.547 seconds for 1,000 pages and 57.703 seconds for 10,000 pages; installed setup
took 6.125 and 54.937 seconds respectively. These values describe those runs.

The receipts do not isolate the cause of the nonlinear setup cost. They do not
prove a query timeout, a memory leak, a device problem, or a Windows scale pass.
The separately declared follow-up above now supplies actual Windows 50,000-page
query observations. The original interrupted receipt itself remains incomplete;
no application acceptance deadline or guard has been changed.

See [the benchmark protocol](SOURCE_PAGE_BENCHMARK.md) for invocation,
identity checks, owned process cleanup and interpretation limits.

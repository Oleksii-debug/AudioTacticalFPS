# Actual retained corpus evidence — 431ac087

Source: 431ac08712b4c31c70a6253711481c02d092de94. Runtime: Linux CPython 3.11.18. This is prior-source evidence; later integrated heads need their own required gates.

| Corpus | Actual segments | Exact result hits | Pagination requests including terminal probe | Job | Result |
|---|---:|---:|---:|---|---|
| 1,000 retained books | 20,000 | 1,000 | 41 | [140527316366](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46004721023/job/140527316366) | PASS |
| 10,000 retained books | 200,000 | 10,000 | 401 | [140527316279](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46004721023/job/140527316279) | PASS |
| 50,000 retained books | Pending | Pending | Pending | [140527316349](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46004721023/job/140527316349) | IN_PROGRESS at inspection |

Completed logs independently contain all fixture batches, full exact-result traversal, 12 real public imports, actual owned-worker cancellation/recovery and the final PASS. Small contract preflight independently passed 8 tests with zero skips.

Artifacts contain bounded JSON and logs, not the corpus: [1,000](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46004721023/artifacts/15203590104), SHA256 cbbfe203caedebdffdfa07f64ee52c851cef25102799d9740804857240f063ee; [10,000](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46004721023/artifacts/15202911859), SHA256 3ccf30b802f60498880ae66cca1bff9f1595b88a5e0c23b63dbb418b84629218.

The fixture uses actual retained originals, catalog rows and SQLite segments. Its initial bulk construction is not a public import-throughput measurement. Fresh startup means a new isolated process; OS disk cache was not evicted. The parent/startup Python socket guard is not an OS-level native-worker egress qualification. The original 431 report's network label was corrected in16207681. No physical audio, NVDA, model-language quality, release signing or arbitrary production-library performance is inferred.

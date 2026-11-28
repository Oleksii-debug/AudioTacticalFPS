# Exact f77 main source PASS and subsequent loopback failure

The integrated-desktop job completed **FAILURE** on `f77cdd445c158f3f66d13c03d920418965840d16` (tree `88a99ee91806d71dca04c75c0aca8c963f5292e1`). Its required source workflows and protected-source inventory passed. The next synthetic local-planning HTTP fixture failed two cancellation cases. This report does not qualify skipped stages or transfer any result to a successor.

## Observed run

- [Run 46078342596](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46078342596), attempt 1, pull-request event.
- [Integrated job 140771249163](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46078342596/job/140771249163), 2026-11-27T23:13:04Z through 2026-11-28T00:23:08Z.
- Unchanged decoded job log: 4,762,934 characters, SHA-256 `aa8fc252ae1da3edab7e74793b67970f53c563bb13b1612a4c840c429f963292`.
- Artifact 15239558005: 305121 bytes, `sha256:1273db3f66c99d213a3e620fd3e488e27fc5e3698898ec5e30cc56bfc38a5a3f`. Its API metadata was read independently. This verifier did not download or inspect the ZIP.

## Passed source boundary

Step 20 ran from **23:19:12 UTC to 00:22:48 UTC**, lasting **63 minutes 36 seconds** within its 90-minute allowance. All **451 required selectors passed**: **11,455 successful executions, zero failures and zero skips**. The independent CASE_START count is also 11,455, covering 5,223 function identities. The six bootstrap compatibility cases are included.

Step 21 passed the exact **5,813-case / 252-module** protected source oracle. Every module's complete function Counter was compared. Twenty-six protected modules are selected twice through directory and individual-file entries, so the expected and observed weighted protected execution population is **6,469**. This overlap does not create additional unique tests.

The companion JSON retains all 451 suite verdict rows, all 252 per-module comparisons, the duplicate-module multiplicities, the actual producer receipt, the exact source blobs and canonical Counter digests.

## Sole failed acceptance step

Step 22, “Exercise only the explicit synthetic local-planning HTTP fixture”, produced **12 passed, 2 failed, 0 skipped** in 10.61 seconds. Both failures are parameters of `test_real_cancellation_does_not_create_an_approved_job`:

| Variant | Observed public exception |
| --- | --- |
| `before` | `ProviderError: Provider operation cancelled before transmission.` |
| `during` | `ProviderError: Provider operation did not complete. Review its uncertain charge status before retrying.` |

The frozen fixture expects `InterruptedError` or `RuntimeError`. The current provider defines `ProviderError(ValueError)`. The trace and source support a fixture exception-contract mismatch. Assertions following the failing `pytest.raises` blocks did not complete, so their downstream request/job/ledger postconditions are not qualified by this failed execution. No product behavior or exception policy is changed by this report.

## Unexecuted later gates

All 31 acceptance steps from 23 through 53 were skipped after step 22. The API reports 32 skipped steps in total because a later Python housekeeping step was also skipped. Artifact upload and the remaining checkout cleanup completed.

The later full sealed-bundle installed population of **5,485 protected cases**, **21 r7 native families**, installed **14-case** local planner, sealed translation **324/324 + 8 native**, and standard installer/update/library **6/4/15/50** gates did not execute in this job. Earlier installed self-tests and native preflights did succeed; “skipped” here describes the later integrated gates only.

Separate f77 auxiliary and standard-generation proofs keep their own source-bound results. They do not turn this integrated job into a pass. Earlier documents recording main PENDING remain historical observations; this document records the later completed failure.

## Qualification boundary

No tests were launched for this review. No candidate, product, workflow, ref or pull request was changed. The full candidate remains unqualified by this run. Real model/language quality, manual Windows 11/NVDA and physical audio, real corpus measurements and distribution rights are outside this source-bound result.

Detailed evidence: [main-source-f77-loopback-boundary-20261128.json](main-source-f77-loopback-boundary-20261128.json).

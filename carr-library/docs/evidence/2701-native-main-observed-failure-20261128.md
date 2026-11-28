# Exact 2701 early native run: observed Import Center failure

**Status: observed failure; this candidate is not integratable.** The separate early-native run on Windows completed with one failed step. The corrected source-copy scenario passed all 21 current checks before the Import Center geometry assertion failed.

## Exact execution

| Item | Observed identity |
|---|---|
| Source commit | `2701b5717360996d706199e370ce1d1cc47f3a61` |
| Source tree | `f7e60bc176f53e810a046640394766fc91209ad7` |
| Tracked files | 1816 |
| Workflow run | [46128793730, attempt 1](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46128793730) |
| Job | [140929198970](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46128793730/job/140929198970) |
| Job API timestamps | 2026-11-28T09:10:20Z to 2026-11-28T09:16:13Z |
| Full main | Not launched |

The complete API step table and actual source-bound receipts are retained in [the JSON record](2701-native-main-observed-failure-20261128.json).

## Failure that stopped the run

Focused step **28**, copied unchanged from main step **30**, executed the real native Import Center fixture. It failed at `native_import_center_smoke.py:99`:

```python
assert dialog.GetSizer().GetMinSize().height <= dialog.GetClientSize().height
```

The actual traceback is an immediate `AssertionError`; the job API records this step from 2026-11-28T09:16:08Z to 2026-11-28T09:16:09Z. No timeout occurred. The minimum layout height exceeded the dialog client height. The fixture did not print numeric dimensions, DPI or display resolution, so no values for those measurements are inferred.

The current product source constructs a fixed 920 by 760 dialog with an 800 by 650 minimum and stacks its full body, including the streamed UTF-8 row, without scrolling. This is the source context for the observed layout failure. The planned import, source search and reopen assertions were not reached. Focused steps **29 through 47**, including the final before/after payload comparison, were skipped.

## Corrected search-copy flow: actual PASS

Focused step **24** executed all **21** expected labels with exact multiplicities. Both the inner and wrapper receipts report native keyboard input, source-copy verification and an unchanged synthetic corpus. The wrapper names this exact commit and installed package. All four module-origin hashes were independently matched to current Git sources, with origins in the fresh installed environment.

The preceding worker-completion fixture repair is now exercised successfully on 2701. This result does not qualify the failed 7c run or a future successor. Manual NVDA and real-corpus performance remain `NOT_RUN`.

## Binding and evidence limits

The source-reviewed before-binding step completed successfully and emitted the exact commit/tree, **1816 tracked files**, **442 installed package files**, and receipt digest `dbce52cb9a653ea93124b00bc2247d6ebca9131f9843f4b64327b0e634c5871a`. Its producer enforces exact Git bytes and the complete installed payload. The full binding maps were not independently decoded from the uploaded ZIP. The after-binding step was skipped after the failure.

Other early step outcomes are preserved as API results. This record does not claim independent inner-receipt qualification for every passed step. The Office-specific receipt explicitly records `NOT_RUN` when Word is unavailable. Separate audio and translation focused records apply only to their exact 2701 runs.

The complete decoded raw job log contains **574150 UTF-8 bytes** (SHA256 `a627987d1107996eeea3c632bc6d0564ebaf2d830aa5ad0566b9cafa647fedac`). Its original BOM and line endings were retained for hashing.

The uploaded artifact is **15257371340**, **245720 bytes**, SHA256 `ca19658b0b97e9dd1492083b1640a225f9878bf2b6fbad29b7079cd3a13cc1d6`. API metadata and the upload log agree. The uploader reports **39 files**; this is not an independently decoded ZIP inventory.

## Next required acceptance

Correct the actual Import Center layout while preserving geometry, native-control and durable import assertions, then run the new exact source through the focused gates. Full Windows CI, its retained installed/bundle/update gates, exact main-artifact verification and canonical integration remain required. The complete platform remains **NOT_QUALIFIED** for its separate external owner-engine, real-model, hardware, manual NVDA and distribution inputs.

This record adds evidence only. It does not change application code, fixtures, workflow blocks, oracles, existing evidence or repository refs.

# Main014: retained source failure

The exact candidate `014b128f13d47a900008f8970de4d0024ff0047d` (tree `b084ad222fcc966c8fd4d9a6b6d8257f1ee5102b`) failed main run **46089514448**, job **140806885393**, attempt1, completed **2026-11-28 02:09:19 UTC**.

## Observed boundary

The moved real-loopback fixture passed **14/14**, with zero failures or skips and pytest exit0. Its exact one-suite gate passed at00:57:20 UTC. The later source invocation retained all451 selectors and reported **448 passing suites and3 failing suites**. Protected-source inventory and all later native, complete-bundle, installed and standard O/A/B qualification stages were skipped on this candidate.

The450 completed source-suite receipts account for **10,797 passing and2 failing execution events**, with no skipped cases. These counts include intentional selection overlap. The timed-out broad assistant invocation is excluded from that case total. Its656 collected cases produced613 start markers, an exact ordered prefix of the prior f77 execution;43 cases had not started. **A start marker is not a passing result.** The JSON contains every completed selector receipt and its canonical counter digest.

## Three failures

| Selector | Actual failure | Evidence limit |
|---|---|---|
| `tests/assistant` | The existing600-second outer suite limit expired at01:07:28.021 UTC after continued progress into `test_workflow_templates.py`. | No final JUnit receipt; no specific stuck-case cause established. |
| `tests/operations/test_resources.py` |31 passed/1 failed. A real child failed during lease release when `model_manifest.local_path` classified a path as a network share. | The displayed parent path is local; the underlying runtime classification must be diagnosed before changing admission. |
| `tests/security/test_idle_renewal.py` |3 passed/1 failed. `store.current()` encountered the nonblocking trust lock after the test observed `not worker.running`, while the enabled idle host remained active. | Fixture synchronization and product ownership remain separate questions. |

The resource stack is `_contend:71 → broker.reserve:371 → Lease.release:78 → _gate:172 → NativeLock:63 → _unheld_path:47 → _state.local_path:18 → model_manifest.local_path:37`. The idle stack is `test_idle_renewal:47 → EntitlementStore.current:269 → security._files.locked:173`.

## Provenance

- [Completed main job](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46089514448/job/140806885393)
- [Retained artifact15242629034](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46089514448/artifacts/15242629034): **304113 bytes**, SHA256 `96deebbd3952c15beaa4faa93c0f70d89e85c704245f225338575bfca3f4205c`.
- Exact decoded log: **4740173 UTF-8 bytes**, SHA256 `1a1decd5250c4c2535748cc407d58548afe5491cd92d482c2c13fdebfc7bb01f`.
- Runner blob: `5937231e2f3a00a163b0e10b9707df27f279b134`; workflow blob: `6a826f25a0a4724b5665b3cfbcf34ab97336971d`; loopback fixture blob: `052f67e286ad8fd6eef5ca3274575dd6c177e9ab`.

The artifact metadata and completed decoded log were read. The ZIP was not independently downloaded or inspected. Earlier f77 source success and separate component passes do not fill this run's incomplete or skipped stages. This evidence component changes no application, fixture, runner, gate, workflow or ref.

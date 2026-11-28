# Exact c31 Windows concurrency preflight

## Observed result

**PASS within the targeted Windows source scope: 77 passed, 0 failed, 0 skipped.**
The complete decoded job log was independently checked against all 77 expected
function occurrences and the three JUnit-derived suite verdicts. Every job step
completed successfully.

| Complete module | Passed | Failed | Skipped | Pytest duration |
| --- | ---: | ---: | ---: | ---: |
| `tests/operations/test_resources.py` | 40 | 0 | 0 | 8.09 s |
| `tests/security/test_idle_renewal.py` | 4 | 0 | 0 | 1.16 s |
| `tests/audio/test_model_manifest.py` | 33 | 0 | 0 | 120.71 s |

The resource selection includes both actual Windows final-path leaf-deletion
phases, native descriptor/inode identity, exclusion of a concurrent peer,
release and admission transfer, six rejected caller namespaces/ADS spellings,
and the unchanged real cross-process contention test. The idle-renewal
selection verifies signed sequence progression and exact renewal call counts.

The model-inventory module emitted one existing `PytestWarning` because
`record_property` is incompatible with the selected `xunit2` reporting format.
It still completed all 33 cases without failure or skip. The warning was not
suppressed.

## Exact identity

- Source: `c31e680b15d2334420f6cf19d5587636e948258b`.
- Tree: `f5766d63cec62a4723191ac10e22da9f7ddeb7f0`.
- Workflow: `.github/workflows/carr-regression-preflight.yml`, blob
  `a495aced2fbdbbb42ede3a3c2ce6982b64f14da4`.
- [Actual run 46098640771, attempt 1](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46098640771);
  [job 140835756807](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46098640771/job/140835756807).
- Job execution: **2026-11-28 02:49:07–02:52:17 UTC**; test step:
  **02:49:53–02:52:13 UTC**.
- Actual interpreter: **CPython 3.11.9 on Windows**, pytest **9.0.2**.
  All **56** recorded runtime versions exactly match the committed integrated
  lock `d2ced1652d80b33680a7247ca1afa47fd271d8f4`.

## Retained evidence and limits

The [machine-readable proof](windows-concurrency-preflight-c31-observed-20261128.json) retains the full expected and
observed function Counters, all three actual suite receipt lines and durations,
all 56 runtime pins, step timestamps, and six source blob/SHA256 bindings. Those
six hashes are independently computed **expected candidate bindings**; they
are not described as decoded values from the artifact's `source.json`.

The complete decoded log has **77,899 UTF-8 bytes**, SHA256
`1d420651429d2f368e047e126bf664457f7a224f6596f963b2d630d856952963`.
The [retained artifact 15244708300](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46098640771/artifacts/15244708300) has **4,280 bytes**,
SHA256 `3d695ecc7b9e871bb7806e937ee8a6d0c4e1e8f165c6457e4d5ccd3a71556ba0`.
Its API metadata and uploader digest agree and bind exact c31/run/attempt1.
**The ZIP and its members were not independently opened.**

This is source execution under the targeted Windows workflow. It does not
qualify the installed wheel, complete-main population, native-family matrix,
bundles, standard installation/update, or final artifact cross-check. The
assistant's 900-second aggregate allowance was not exercised here. The
[failed014 predecessor](main-014b-source-failure-20261128.json) remains failed.
Real model inference, acoustic/language quality, physical audio, manual NVDA,
signing and redistribution clearance are outside this proof.

This evidence-only component has sole parent c31 and changes neither the
candidate application nor any test, workflow, branch or PR reference.

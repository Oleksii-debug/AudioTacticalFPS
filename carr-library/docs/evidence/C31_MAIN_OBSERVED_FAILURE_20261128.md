# Frozen c31 full-main result — observed failure

The actual Windows full-main run for `c31e680b15d2334420f6cf19d5587636e948258b` **failed**. Its source step completed every selected suite: **450 passed and 1 failed out of 451**. The sole failure is the local voice helper deadline case. The protected-source gate and all later full-bundle, installed and standard-update gates were skipped.

The complete suite receipts, all 5,225 observed function-start identities with their multiplicities, step inventory and retained traceback are in [the structured evidence](c31-main-observed-failure-20261128.json).

## Exact execution identity

| Field | Observed value |
|---|---|
| Source commit | `c31e680b15d2334420f6cf19d5587636e948258b` |
| Source tree | `f5766d63cec62a4723191ac10e22da9f7ddeb7f0` |
| Run / attempt | [46099459968](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46099459968), attempt 1 |
| Integrated job | `140838351981` |
| Job conclusion | Failure; completed 2026-11-28 03:59:17 UTC |
| Source step | 03:07:38–03:59:09 UTC, 51 minutes 31 seconds |
| Workflow source | `6a826f25a0a4724b5665b3cfbcf34ab97336971d` |
| Runner source | `2d04b9b269ae85c24349ae8d4ad9b295737866fb` |

## Complete source population

The retained log contains exactly the 451 selector receipts declared by the frozen workflow. Their totals are **11,462 passed, 1 failed and 0 skipped case events**. The 11,463 `CASE_START` events match these completed populations. Existing directory and file selectors overlap, so these totals must not be described as 11,463 unique tests.

An independent comparison with the current `b350bdab38415c42d13c69116dd876747e8f0354` oracle found all **252 protected modules and 5,821 declared cases**, with the exact expected function-start Counter after selector multiplicity: **6,477 events**. Their direct module receipts contain 5,820 passes and the same one failure. This identity match does not qualify the protected gate: its actual workflow step was **SKIPPED**.

The earlier repaired boundaries completed here: the assistant selector passed all 656 cases in 408.93 seconds, resources passed all 40 cases, and measured book duration passed all 51 cases in 746.03 seconds. The separate early loopback step passed its exact 14 cases. These observations do not qualify any skipped later step.

## Sole observed failure

`tests/audio/test_local_voice_helper.py::test_actual_running_child_stops_before_private_pcm_cleanup[deadline]` failed. Its module completed with 66 passes and 1 failure.

The call first raised the intended two-second segment `TimeoutError` at `audio/engines.py:276`, reached through `local_voice_helper._call` and the existing `engines._run`. While unwinding `LocalVoiceHelperEngine.synthesize`, the existing `TemporaryDirectory` attempted to remove its private `.speech-...` root. Windows refused `os.rmdir` with **WinError 32**. The standard-library fallback also reported WinError 5, then the final root removal repeated WinError 32 and masked the timeout.

The deadline case took 2.44 seconds; its cancellation variant passed in 0.44 seconds. Assertions following the expected-exception block were not completed for the failing variant. This traceback does not establish which process or handle retained the directory. It does not justify treating the issue as a fixture-only error or applying an unrestricted cleanup retry.

The frozen source files are bound in the structured record. No product repair, timeout change, skipped test or retry is part of this evidence commit.

## Raw evidence identity and limits

The complete decoded job log returned by the GitHub connector contains **4,746,287 characters / 4,746,293 UTF-8 bytes**, SHA256:

`8ad4fd29622354685701500be6617965ab1053b8e2543cd018f5bd7fa708fc26`

The actual main artifact is **15247755531**, named `carr-windows-c31e680b15d2334420f6cf19d5587636e948258b-1`, **300,102 bytes**, SHA256:

`6b5401f32c39f52319b6e82d573a2d0997748dfafb7af03816b0fbf3df903619`

Its API metadata and upload-log identity agree. The upload log reported 24 files. **No artifact ZIP was downloaded or read**, so this record makes no independent ZIP member or payload verification claim.

The candidate and all frozen references remain unchanged. This run is historical evidence for c31; it cannot qualify the later mandatory silence-inspection source or any cleanup repair.

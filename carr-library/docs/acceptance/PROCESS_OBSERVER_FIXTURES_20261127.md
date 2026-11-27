# Process-observer fixtures — actual integrated failures, 2026-11-27

## Observed execution

The complete integrated run on source `c55d24e310e2ed4e8919a9b63b1695cbbcca49fb`
(run [46029884541](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46029884541))
reached these terminal outcomes:

| Job | Actual strict result | Failure |
| --- | --- | --- |
| Linux [140610712095](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46029884541/job/140610712095) | 181 suites: 179 passed, 2 failed; 3,965 cases passed, 3 failed, 0 skipped | Two spelling process-observer cases and one Calibre descendant observation |
| Windows [140610712300](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46029884541/job/140610712300) | 121 suites across early and integrated stages: 120 passed, 1 failed; 2,798 cases passed, 2 failed, 0 skipped | The same two spelling process-observer cases |
| Native commands [140610711745](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46029884541/job/140610711745) | All 18 workflow steps succeeded; 9 emitted native reports passed | None |

These are source-bound outcomes, not a passing verdict for the whole integrated
workflow. Stages after a failed strict gate are not inferred to have executed.
Manual NVDA and paid external-provider execution remain separate.

The Linux log also records 156 passing legacy transcription recovery cases
(including the 130 added canonical cache compatibility cases), 40 typed
transcription cases, 21 legacy PCM cases and 34 shared JSON state cases. These
counts overlap the integrated result and must not be added to it.

## Spelling observer

The current `capture_process.run_process(..., owned_tree=True)` invokes
`windows_job.JobProcess` on Windows and `subprocess.Popen` on POSIX directly.
The earlier test wrapped the separate `start_owned_process` API, which this
path never calls. Consequently it recorded no child; the cancellation timer
never armed, and the cancellation case reached its worker timeout instead.

The corrected existing test wraps the actual platform launch boundary, calls
the real saved constructor and records a live child before arming cancellation.
It retains the 0.6-second independent timeout case and the 5-second cancellation
allowance, and checks the specific public error category. Child settlement and
released resource admission are asserted before the test's final cleanup.

Reviewed candidate:
`fa5415fce20c114b5fa1bebd45e2da28463db00b`, parent
`5f18481654277425ab8af170575de978e7a2a69c`.

## Calibre descendant observation

After real native-family cleanup, Linux procfs raised
`ProcessLookupError(ESRCH)` while the fixture read `/proc/PID/stat`.
The helper already accepted `FileNotFoundError(ENOENT)` for that observation
race but did not accept this second exact process-gone condition.

Only those two absence exceptions now mean exited. Four focused controls
exercise disappearance before open/during read and demonstrate that permission
denial and unrelated I/O failure still propagate. They do not send a signal to
an arbitrary process or change the interpreter's global platform identity.
The three actual owned-descendant cases remain unchanged.

Reviewed candidate:
`f274cc942bb3d91069f6621d5694bb002dc469c2`, parent
`5f18481654277425ab8af170575de978e7a2a69c`.

## Scope and verification

Both corrections received independent source review. At composition time the
new fixture bodies are **NOT_RUN**. Their actual Linux and Windows replay is
required before claiming closure.

This composition changes exactly the two existing test files and this evidence
note. All application source, package/build inputs, workflows, the current
17-core / 53-semantic locks, both SBOMs and FULL FFmpeg9.1 identity remain byte
identical to `5f18481654277425ab8af170575de978e7a2a69c`. No product timeout,
process ownership, resource limit or cleanup assertion is weakened.

The already queued large-source and complete-corpus gates on `5f184816` cover
unchanged production owners. Their results must retain that exact source SHA;
they must not be relabeled as execution of this test-only commit. The new
integrated and installed bundle runs establish the new commit/tree identity.

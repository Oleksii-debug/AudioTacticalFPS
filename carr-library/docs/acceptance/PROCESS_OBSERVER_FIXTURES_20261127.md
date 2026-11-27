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

## Scope and verification at d9fc48b0

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

## Subsequent exact 5f184816 replay and observer follow-up

The actual Linux job [140619371071](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46032443327/job/140619371071)
on `5f18481654277425ab8af170575de978e7a2a69c` completed 181 strict suites:
177 passed and 4 failed; 3,963 cases passed, 5 failed, 0 skipped.

Its five failures were the two already diagnosed spelling cases, one owned
audio procfs observation, one Calibre descendant observation, and one CI
runner fixture PID record. No other failure appears in that terminal verdict.
This Linux suite set does not contain `test_pending_backup.py`; it does not
establish execution of the new eight hot-journal recovery cases.

- Owned audio: a real terminal traceback reports `ESRCH` during procfs read.
  The observer now routes this condition through its existing second
  same-PID probe. The additional boundary control holds a real procfs
  descriptor, waits for actual child exit, then injects the read errno; it
  requires the real second process probe before claiming absence.
- Calibre: a further failure reported a live result from the observer; that
  traceback alone does not prove which branch ran or that the child was gone.
  Source review identified that a disappearing stat entry could fall through
  to true after an earlier successful signal probe. The helper now rechecks
  the same PID when stat is absent. Live processes, unavailable procfs and
  denied probes remain failures; it does not infer exit from absent procfs.
  Actual rerun remains necessary to distinguish this race from a real leak.
- Shared CI runner fixture: the reader observed an empty PID record while the
  real descendant was still publishing it. The descendant now writes,
  flushes and closes a private sibling before atomically publishing the
  marker. The real child identity, existing 30-second startup barrier,
  0.15-second timeout case, family quiescence and report-cleanup checks remain.

The reviewed follow-up candidates are
`69ca74dcc96c67f8e7f66fa685e1dbc58239b9a2` (audio observer),
`8615da518abb83bf645381ac7079167a0f099105` (Calibre observer), and
`a96a201e3f6ee68511f24c1991eae6fc152885d9` (complete PID marker).
All have parent `d9fc48b0c1a423e2e22b63c72f0d951a92fc9fb1`.
They change test fixtures only, received independent source review and were
**NOT_RUN at composition time**. Product process ownership and deadlines are
unchanged. The independently merged canonical Save All changes are recorded
separately in the incremental canonical resolution.

## Native spelling probe after strict Windows source success

The exact `18a61e7ec6601119f9a4ffa7228274e836856ca4`
[Windows job 140647931643](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46040854213/job/140647931643)
passed all 123 early required source suites: 2,854 passed, zero failures and
zero skips. Its subsequent installed native spelling probe handled the reading
editor's Review spelling button, then reached the existing 45-second deadline
without any successful review-dialog checks. The final generic assertion hid
the stored error and the editor's status. This is a failed native gate; those
early suites do not include the separate Save All suite.

Source review found that the probe assigned a raw temporary-directory path to
its frame's selected library, while NarrationStore resolves that root. The
actual LibraryApp also resolves its selected root. Windows can expose short
temporary path spellings, so the probe's inconsistent identities can fail the
existing strict nested-editor guard before the factory opens. The old log does
not print the compared paths or status: it does **not** prove that an 8.3 alias
caused that particular run.

Candidate `9a636480cab914ad9dc7e70a58cdff1c60c35644` resolves only the probe's
owned root after creating it, matching the actual application's contract.
It also reports bounded ASCII phase, stored error, editor status and factory
entry/failure evidence, fails immediately if the action returns without
opening the required dialog, and re-raises the original stored exception.
The real editor, spelling backend, selected emoji/CRLF source, shared operation
session, original-byte checks, apply/Undo/cancel assertions and 45/60-second
deadlines remain intact. No application source, library identity guard,
dependency, runtime payload or product timeout changes.

The native probe must pass on the composed source. This correction is
**NOT_RUN at preparation time** and does not establish successful Windows
spelling, language quality or NVDA acceptance.

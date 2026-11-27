# Native document probe owner and completion CI

Source checkpoint: 28 November 2026 (Europe/Budapest). The failed execution below occurred on 27 November UTC.

## Observed execution

The exact source `fd0cca2405f33c813c4cf43c9661edf1f2e93a55`, tree `1511a644162260d1eb7a7029613d2831ea750021`, ran in [MAIN 46077094891, Windows job 140767264555](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46077094891/job/140767264555).

All 392 required source suites completed with 9,644 PASS, zero failures and zero skips. These are execution counts for the selected overlapping stages, not a sum of unique tests across the project. The 239-suite integrated source block itself ran from 23:18:29 to 23:45:02 UTC and passed 6,106 cases.

The subsequent native citation, translation, language-evidence, Import Center and reading stages succeeded. The corrected V3 reading probe passed all 26 checks in installed mode, following its earlier 26-check source-mode PASS and the companion native job's 300 strict cases and 12 successful native reports. Its five module origins were checked in the applicable execution mode.

The next stage, `tests/ui/native_document_formats_smoke.py`, failed at line 180 in `planned()`:

```text
button = next(child for child in dialog.GetChildren()
StopIteration
```

The log already contained successful native control-name, asynchronous preflight and two-document queue assertions. This was a stale probe assumption about the control's parent. The current Import Center creates the exact `wx.Button` labeled `&Start queued files` in `dialog.body`, the scrolling panel introduced by the prior layout correction. The status and Cancel/Close footer remain direct children of the dialog.

The full bundle and standard setup stages were NOT_RUN in this execution. The successful source and earlier native checks do not turn this failed MAIN execution into a PASS.

Failure artifact: [15238087881](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46077094891/artifacts/15238087881), 275,477 bytes, SHA-256 `98f55b607371a24f11f4c094ac4c8d638ac8bfba2b40ffadcd802214de7dbdd0`.

## Minimal probe correction

Detached probe candidate: `240f048a6bbce0de8e87a9dab84a8d514d43e22c`, parent exact fd0, tree `a4e86c917ae41fd5a4726b11454f5c437254652b`.

Only the Start lookup changes from `dialog.GetChildren()` to `dialog.body.GetChildren()`. The candidate still requires the same native button type and exact label, dispatches the same button event through its handler, and requires the actual dialog to become busy. The real HTML/Markdown import, immutable original/hash checks, hidden-script exclusion, reader focus, Unicode text, unverified logical-location provenance and next-paragraph checks remain unchanged.

All 27 literal acceptance-check call sites, their predicates, and every other byte of this probe are preserved. The 90-second acceptance deadline, polling cadence and error/cleanup behavior remain unchanged. The declaration of the `check` helper is not an additional acceptance check.

The root independently fetched the candidate commit, one-file compare and complete source and proved that reversing this one replacement restores the exact original file. The current reading controls and callback interfaces used after the repaired lookup were also checked; no second concrete mismatch was found in that bounded review.

## Earlier native feedback with the same installed checks

Workflow candidate: `1911d5c059ffe3ace3b8ea44ea12fb1efb5fa030`, after workflow-only parent `e2fe72beac5d7ab43dfcc220514890b9d3d1a05c`, both descending from exact fd0.

The existing contiguous block of 17 late native steps is moved intact to immediately before `Run required integrated source workflows`, after the existing installed capture/clip/audiobook controls. This runs those already-required installed checks before the longest source block. The commands, environments, individual native step deadlines, failure propagation and assertions are retained.

The installed desktop and the reviewed components required by those checks are prepared by earlier steps. The moved block does not depend on an output or exported environment from the long source block. The source block still executes in full after the native checks; model preparation, launcher build, the full bundle and standard setup still follow it.

The corrected document probe remains an installed-package probe in this workflow. An additional early invocation of the same existing probe is added to the native-command companion job, after its reading probe. That job already installs the exact application and its pinned core desktop dependencies. The new invocation uses the existing default installed mode and `--native`; it introduces no source override, dependency, retry or relaxed check. Its new step budget is three minutes, the existing companion job remains twelve minutes, and the probe's own ninety-second limit is unchanged. The later MAIN invocation is retained.

A bounded independent audit of 46 unique late/native and full-bundle probes found no second incorrect Import Center/reader parent lookup. The Import Center's fixed Close footer remains a direct dialog child, and the directly queried EPUB-reader/help controls are still constructed on their respective dialogs. This source review does not replace actual native execution.

## Operational CI budget

The integrated Windows job limit changes from 80 to 150 minutes; the full-bundle step limit changes from 20 to 60 minutes. These are workflow scheduling budgets for building, installing and exercising the complete package. Product operation limits, per-test time limits, the 90-second document-probe deadline, the long source block's 30-minute limit, and the other native/setup step limits remain unchanged.

This change addresses a measured capacity risk before the first complete execution of the full bundle. The step selects 204 unique pytest selectors in one process (165 contents selectors and 39 CLI selectors), in addition to acquisition/build/install, approximately 37 native/script invocations, installed synchronization and loopback checks.

A bounded comparison with the actual 192 MAIN log found 739 unique printed call phases in that selection. Taking the minimum for repeated observations, those call phases totaled 2,031.42 seconds; including their printed setup/teardown phases gave 2,252.96 seconds. Separate pytest-process startup and unprinted faster cases were not included. These observations came from `ci_check --jobs 3`; the one-process installed run has different contention and cache behavior. The sums are therefore not a portable lower bound and do not prove that the earlier 20-minute budget must fail. Overlapping Audio totals were not added. The previous MAIN failure was the control-parent lookup, not a timeout.

The larger operational budget preserves the complete selected workload while allowing its actual duration to be observed. It is not evidence of successful runtime, product throughput or installer performance.

## Qualification at this source checkpoint

The one-line probe and workflow changes have source review and exact-delta checks. Corrected native execution and complete full-bundle/setup execution are NOT_RUN at the time of this source checkpoint. Subsequent actual results must retain their executed commit, tree and artifact identities in PR99.

All application source bytes, version `0.2.0.dev3`, the 17-core / 53-semantic dependency profiles and the exact FULL FFmpeg 9.1 identity are retained. The code is not qualified for private-model inference, per-feature 135-language coverage, physical audio/listening, manual NVDA, full DAISY/external-reader acceptance, production signing/authority or distribution rights merely by this probe or CI-order change.

Historical Large and Corpus results retain their original 203bee5a and 5f184816 execution identities. The current transitive native-lock helper differs from those scale baselines; no whole-current-wheel identity is claimed.

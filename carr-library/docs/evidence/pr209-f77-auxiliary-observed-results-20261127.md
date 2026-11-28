# PR 209: exact-f77 auxiliary Windows results

All five assigned auxiliary Actions jobs concluded successfully on commit `f77cdd445c158f3f66d13c03d920418965840d16`, tree `88a99ee91806d71dca04c75c0aca8c963f5292e1`. The audio run has seven explicit skips. These are preserved as skips, not counted as passes.

**The separate full integrated desktop job is still pending completion.** Run 46078342596 / job 140771249163 remains in progress at 2026-11-27 23:51:37 UTC. This document records the five completed auxiliary jobs; it does not declare the full integrated run passed.

The companion JSON records the complete current function Counters, native label Counters, receipts, source readbacks, artifact metadata and decoded-log digests. Historical receipts were used only to compare populations. No earlier head's outcome was transferred to this commit.

## Actual results

| Lane | Source execution | Installed/native execution | Actual run and job |
| --- | --- | --- | --- |
| Bibliography | 28 strict suites; 1066 passed; 0 failed or skipped; 420 function identities | 108 native checks; exact current wheel commit/tree; five reported installed-module hashes independently matched | [46078342568 / 140771249081](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46078342568/job/140771249081) |
| Encrypted snapshots | 322 passed; 0 failed or skipped | 154 isolated installed cases and 44 native checks | [46078342585 / 140771248969](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46078342585/job/140771248969) |
| Keyboard and draft recovery | 11 strict suites; 291 passed; 0 failed or skipped; 142 function identities | Five native probes: 35 keyboard, 23 reader, 15 draft recovery, 38 Save All/Edit/recovery, and 35 progress checks; 146 total | [46078342596 / 140771249457](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46078342596/job/140771249457) |
| Native launcher | 19 launcher and 27 bundle cases; all 46 passed | Nine actual native launcher checks using the explicit synthetic child | [46078342648 / 140771249104](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46078342648/job/140771249104) |
| Audio | 2266 collected executions: 2259 passed, 7 skipped, 0 failed; 22 passed subtests reported separately; 1175 function identities across 104 modules | Installed smoke 16; exact native effects 151, clips 26, publication 11; installed self-test 9 | [46078342645 / 140771249153](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46078342645/job/140771249153) |

The audio source phase took 1579.47 seconds. Its required real SAPI, owned-process shutdown/isolation and 1/3/10-hour streamed-PCM cases all passed, including both repaired exited-leader fixture cases. The three native wrappers report the exact current commit and tree. All twelve distinct reported installed modules, and each native probe digest, independently match current source bytes.

These lanes overlap one another and the integrated main job. Their counts must not be added as a unique-test total. The keyboard result qualifies only the secondary keyboard job, not the separate integrated desktop job in the same Actions run.

## Seven explicit audio skips

| Count | Actual reason |
| --- | --- |
| 1 | POSIX SIGINT delivery; native Windows cancellation is separately probed. |
| 1 | POSIX process-group cleanup; Windows uses its owned Job Object. |
| 2 | The actual FluidSynth 2.4.4 / reviewed TimGM6mb runtime is unavailable. |
| 1 | The non-Windows playback-unavailable branch does not apply to this Windows runner. |
| 1 | Windows reports no physical audio output device. |
| 1 | The headless ffplay control requires an installed POSIX ffplay. |

The native playback receipt independently reports `UNAVAILABLE` with the same no-device reason. PCM generation, measurement and transformation passed; physical listening was not qualified.

## Retained provenance

| Lane | Artifact | Bytes | SHA256 |
| --- | --- | ---: | --- |
| Bibliography | 15236627802 | 23094 | `60d8f604973e47d472cdfb9599658eb995ae3b6a29342d4efdcdae2f368668c2` |
| Encrypted snapshots | 15236882551 | 10130 | `69136105fc3d395ef52f73ddc0638294e3f1cf131e0582350a52092be59023f7` |
| Keyboard | 15237372638 | 11409 | `eae659fe58308eaa76489dfff2532736aa403391811299d669ff996f1fb28081` |
| Audio | 15238755957 | 55199 | `6393daa124095f49064067e02d22c3af8a25698b15d895ed3594b7a52bc7299c` |

The launcher intentionally uploads no binaries. Its native receipt and build receipt agree on executable SHA256 `d4d0870e8e8135a84019fc79101ef5e6a431a47a5dfd9dfdb5b79cfa4c4202ff`; all seven compiler-input hashes independently match this source commit. The executable itself was not downloaded or independently rehashed here.

This review parsed the complete decoded GitHub job logs and read artifact metadata. It did not independently download/read these auxiliary ZIPs. Snapshot source JUnit remains in its identified artifact; the source count here is the actual pytest summary. The installed snapshot population is additionally retained as its observed 154-event function Counter.

Ten human snapshot PASS lines have codepage replacements in the decoded log presentation; the ASCII-escaped terminal JSON retains all 44 exact labels and is the basis for their Counter. One human audio-smoke label has the same presentation limitation and is preserved unchanged in the JSON. The three strict audio native Counters and all five keyboard native Counters are independently exact.

The native launcher receipt explicitly does not verify an actual Python installation, Authenticode or NVDA. Manual NVDA, physical listening, unprovided production-model quality, signing and redistribution clearance are not claimed by any aggregate in this document. The separate integrated main job and any later commit require their own evidence.

# PR 209: observed c31 auxiliary Windows results

**The full integrated job is still pending.** This document records five completed auxiliary jobs at `2026-11-28 03:37:09 UTC`; it is not a full V3 release qualification.

Tested commit: `c31e680b15d2334420f6cf19d5587636e948258b`  
Tested tree: `f5766d63cec62a4723191ac10e22da9f7ddeb7f0`

The [complete evidence JSON](pr209-c31-auxiliary-observed-results-20261128.json) retains current run/job metadata, all source and installed function Counters available in the logs, exact native-label multiplicities, raw JSON receipts, source/installed bindings, log digests and artifact metadata. Every result below comes from the current run. The older 014 proof is used only as an unchanged expected-population comparison.

## Current completed jobs

| Lane | Actual source / installed result | Actual native result | Current run |
|---|---|---|---|
| Native launcher | 46 unittest identities, all passed | 9 checks; repeated build/native receipts agree | [46099459969](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46099459969/job/140838351663) |
| Encrypted snapshots | 322 source passed; 154 installed passed, 68 function identities | 44 exact JSON labels | [46099459983](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46099459983/job/140838351870) |
| Keyboard and draft recovery | 291 passed, 142 function identities, 11 suites; zero skipped | Five probes: 35 + 23 + 15 + 38 + 35 = 146 checks | [46099459968](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46099459968/job/140838351772) |
| Bibliography and export | 1,066 passed, 420 function identities, 28 suites; zero skipped | 108 exact labels; five installed/source hashes | [46099459989](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46099459989/job/140838351778) |
| Audio | 2,259 passed, 7 explicitly skipped, 22 subtests; all 2,266 ordered starts and ends, 1,175 functions, 104 modules | Effects 151, clips 26, publication 11; installed audio smoke 16 and library self-test 9 | [46099459972](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46099459972/job/140838351449) |

These populations overlap and must not be summed as a count of distinct application tests. The snapshot source result is the actual 322-case pytest summary; its complete printed function Counter is available for the separate 154-case installed run.

The audio source suite took **1,877.22 seconds**. Every per-case outcome agrees with the aggregate 2,259/7 result. Its four warnings concern pytest `record_property` with the xunit2 reporting format; the recorded warning list contains no application exception.

## Seven retained audio skips and device limits

The current short summary records seven skips: POSIX SIGINT, POSIX process-group cleanup, two unavailable FluidSynth/TimGM6mb controls, the non-Windows playback branch, absent native Windows audio output hardware, and the POSIX ffplay control. Their exact case names, source lines and reasons remain in the JSON.

The real installed SAPI/FFmpeg smoke measured nonzero stereo PCM from two available SAPI voices. This does not establish Ukrainian/Slovak voice quality, neural music quality, physical listening or manual NVDA acceptance. The separate current physical-playback receipt is **UNAVAILABLE: Windows reports no audio output device**. Manual listening remains `NOT_RUN`.

The bibliography receipt also retains `NOT_RUN` for manual keyboard/NVDA, native file pickers, overwrite accessible focus and installer qualification. Controlled file choices are identified explicitly.

## Source and evidence boundaries

The actual bibliography wheel receipt binds c31 and its exact tree, 442 packaged payload files and 373 required runtime files, with SHA256 `cf04c2711d22a23f3cee65b21de6e35773e31bab6ec86301d64c8e0845debac7`. The audio outer receipts independently bind the same application commit/tree and the actual installed module hashes. The native launcher is an unsigned private candidate; it does not claim Authenticode, an installed Python scenario or manual NVDA.

Snapshot human-readable PASS lines contain ten replacement-character/codepage-damaged labels. They are kept separate from the complete, exact **44-label ASCII JSON Counter**, which passed unchanged. One audio-smoke A–B label also has a decoded replacement character; its raw value is retained. None of the strict 151/26/11 audio Counters is rewritten to conceal this presentation issue.

Artifact IDs, byte counts and SHA256 digests match both the current GitHub metadata and uploader output. This verifier did **not** download or rehash the ZIP payloads. The launcher lane intentionally has no artifact. Source-file blob inventories identify the current fixtures; they are not additional runtime claims.

The [full integrated main job](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46099459968/job/140838351981) was still executing **Run required integrated source workflows** at this observation. Its later installed, complete native, artifact-read and promotion decisions remain separate. These results do not transfer to any successor, including the newly authored completed-audio silence-inspection feature.

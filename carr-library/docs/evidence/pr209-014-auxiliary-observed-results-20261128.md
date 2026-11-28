# PR209 — five auxiliary results on exact014

**All five auxiliary jobs passed on `014b128f13d47a900008f8970de4d0024ff0047d`, tree `b084ad222fcc966c8fd4d9a6b6d8257f1ee5102b`. The full integrated-desktop job is still running.** This document does not qualify its later sealed-bundle, installed, native, standard-update or artifact-read gates.

Observed on 28 November 2026, attempt 1 of the pull-request workflows. Every result below was independently checked against its current raw job log, complete available function Counter, native receipts and artifact metadata. Historical f77 results remain separate.

## Actual completed jobs

| Lane | Exact run / job | Observed result |
| --- | --- | --- |
| Bibliography | [46089514445 / 140806885234](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46089514445/job/140806885234) | 28 suites, **1,066 passed**, no failures/skips; 420 function identities. **108 native checks**, five installed-module hashes and wheel provenance bind current014/b084. |
| Encrypted snapshots | [46089514425 / 140806885037](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46089514425/job/140806885037) | **322 source +154 installed**, no failures/skips; 68 installed function identities. **44 native checks** and installed module paths verified. |
| Keyboard secondary | [46089514448 / 140806885122](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46089514448/job/140806885122) | 11 suites, **291 passed**, no failures/skips; 142 function identities. **146 native checks**: 35 keyboard settings, 23 reader remapping, 15 evidence drafts, 38 Save All, 35 progress. |
| Native launcher | [46089514415 / 140806884878](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46089514415/job/140806884878) | **46 source cases +9 actual native checks**, seven source hashes. Compiled launcher SHA-256 `d4d0870e8e8135a84019fc79101ef5e6a431a47a5dfd9dfdb5b79cfa4c4202ff`. Synthetic child scope; actual Python installation, signing and NVDA remain unverified. |
| Audio | [46089514416 / 140806885022](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46089514416/job/140806885022) | **2,259 passed, zero failures, seven explicit skips**; 22 passing subtests recorded separately. Complete 2,266-event/1,175-function/104-module Counter. Installed smoke 16, native effects 151 / clips 26 / publication 11 and self-test 9 passed. |

Audio ran from 00:47:22 to01:19:01 UTC. Its source phase took **1,810.33 seconds**. The real 1/3/10-hour PCM cases, installed SAPI, owned process-family cancellation and both repaired exited-leader cases passed. The four warnings concern pytest's `record_property`/xunit2 representation; they are not application exceptions.

## Seven audio skips remain explicit

- POSIX SIGINT delivery.
- POSIX process-group cleanup.
- Two actual FluidSynth tests: the selected runtime/fixture is unavailable.
- A test for non-Windows device behavior.
- Actual Windows playback: the runner reports no audio output device.
- POSIX ffplay dummy-device playback.

Physical playback is **UNAVAILABLE**, and manual listening/NVDA are **NOT_RUN**. These limits are not converted into passes by synthetic or headless processing.

## Retained artifacts and display boundaries

| Lane | Artifact | Bytes | SHA-256 |
| --- | ---: | ---: | --- |
| Bibliography | 15240469522 | 22845 | `bf20bfc24ab0093b54cbfd9a147542483ffd968c6610ac11f3d315e1df242f93` |
| Snapshots | 15240980847 | 10118 | `06dc0aff975bc313efd5fcc86f053fd7b5193e17ae007064d5e4fd7aac652936` |
| Keyboard | 15240458422 | 11457 | `e83e555719c74425e42d7d2014c08e5d523f2081c2ba9ca84f1ca18323252236` |
| Audio | 15241018624 | 55260 | `501e4dec64beb13b97d2a2227311260dda92f946a3570bf7911516fdd3ed360d` |

The launcher workflow deliberately retains no binary artifact. This review read artifact metadata and raw job logs; it did not download ZIPs or independently rehash packaged binaries.

Ten snapshot human-readable PASS lines have codepage replacement characters. The exact **44-label JSON array is intact** and supplies the verified native Counter. One audio-smoke label has the same presentation limitation; its raw spelling is retained. Strict effects/clips/publication label Counters match exactly.

## Scope of this proof

The companion [JSON evidence](pr209-014-auxiliary-observed-results-20261128.json) retains complete available Counters, suite rows, receipts, current source bindings, log hashes, step outcomes and every skip identity/reason.

The [main integrated job 140806885393](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46089514448/job/140806885393) remains **PENDING**, in required source workflows at observation. The five auxiliary selections overlap and are not a combined unique-test total. Full V3 remains **NOT_QUALIFIED** for uncompleted integrated gates and separate real-model/language, manual Windows 11/NVDA/audio, real-corpus and distribution-rights requirements. No successor inherits these results.

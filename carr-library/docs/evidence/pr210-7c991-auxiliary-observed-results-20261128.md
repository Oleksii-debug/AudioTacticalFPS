# Exact 7c991 auxiliary Windows results — 2026-11-28

**Five auxiliary jobs passed on the exact candidate below. The integrated main job and its final artifact verification remain separate and are not qualified by this report.**

Candidate: `7c991dac1581faa63cb32eaaa5e21cf548287b38`, tree `4929a01d460a1184d0552fdcda0fe392f1e7e21b`, [PR 210](https://github.com/Oleksii-debug/AudioTacticalFPS/pull/210). All five results are from attempt 1. The source branch retains its earlier 424cc name; every reported checkout and receipt binds the current 7c991 commit.

Observed: 2026-11-28 08:01:54 UTC. The integrated job [140905458267](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46121064501/job/140905458267) was `in_progress` with conclusion `pending` at this observation. Its complete qualification and final archive inspection are owned by their separate evidence.

## Current observed populations

| Lane | Exact run/job | Actual test population | Actual native checks |
|---|---|---|---|
| Launcher | [46121064498/140905457311](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46121064498/job/140905457311) | 46 unittest identities, 19 + 27 across two modules | 9; repeated native/build receipts agree |
| Keyboard | [46121064501/140905458033](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46121064501/job/140905458033) | 291 passed; 142 function identities; 11 passing suites | 146 = 35 + 23 + 15 + 38 + 35 |
| Snapshots | [46121064493/140905458089](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46121064493/job/140905458089) | 322 source summary; 154 installed cases and 68 function identities | 44 occurrences / 32 distinct labels |
| Bibliography | [46121064503/140905457270](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46121064503/job/140905457270) | 1,066 passed; 420 function identities; 28 passing suites | 108 occurrences / 60 distinct labels |
| Audio | [46121064509/140905457444](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46121064509/job/140905457444) | 2,364 passed, 0 failed, 7 skipped; 22 passing subtests; 4 pytest warnings | 151 effects + 26 clips + 11 publication; installed smoke 16; installed library self-test 9 |

The complete JSON companion contains every parsed function Counter, suite row, native receipt, source binding, job step outcome and decoded-log hash. Overlapping suites are not added into a misleading combined test total.

## Independent verification

Every reported actual population was reconstructed from the completed current logs. Expected contracts were reused only after their immutable source/test/probe/workflow blobs matched this candidate. The source binding manifest contains 205 current paths, and 52 selected files were freshly fetched and SHA256 hashed. Current installed native hashes were compared to those source bytes; module paths are retained only where the producer actually reports them.

Audio has exactly 2,371 sequential CASE_START and CASE_END markers, 1,214 function identities and 104 modules. The current run retains all seven printed skips: two POSIX-only process/signal checks, two unavailable FluidSynth/TimGM6mb checks, one non-Windows playback branch, the unavailable physical Windows output device, and one POSIX ffplay check. All four warnings concern pytest record_property with xunit2; they are not omitted from the JSON.

The snapshots source count is its printed pytest summary, while the installed 154-case population has a complete printed Counter. The 44 native snapshot labels are checked from the exact ASCII-escaped JSON. Damage in separately captured human captions is preserved and is not silently normalized into acceptance evidence.

For example, a human line reads `PASS: native command is enabled: Publish snapshot�`, while the final raw JSON retains `Publish snapshot\u2026`. In `tests/ui/probe_portable_sync_native.py`, `click()` reads the actual wx button label; `check()` stores that same string before human printing; the final `json.dumps` preserves it with ASCII escapes. This demonstrates a human-output/capture representation issue, not an observed product-facing Unicode defect. The decoded log alone cannot identify the exact encoding stage.

## Retained artifact metadata

| Lane | Artifact ID | Bytes | SHA256 |
|---|---:|---:|---|
| keyboard | 15253569001 | 11393 | `2ce3d37d336e9ee7106a4d012d3d679963a90f7eff78a5bb0c4a52815a391d3f` |
| snapshots | 15253784349 | 10108 | `1811cf33a3082607cda511867440312582a6b614a6a80b96c0eacda93ca39474` |
| bibliography | 15253898202 | 22949 | `ea9644b02b7d570ea9d33a669daba9e6aeb1e0a9009dac5d64e6b9b722a1f0d4` |
| audio | 15255415308 | 56998 | `3224b996d6951123b966e205bd939719601ea686776fb1ded3b7be366546bc7d` |

Each ID, size, digest and source commit agrees between the GitHub artifact API and the actual uploader log. The launcher workflow does not retain a binary artifact. No artifact ZIP or raw JUnit archive was downloaded, decoded, imported or executed by this verifier. Complete decoded logs are individually hashed as their UTF-8 bytes; original compressed log archives are not claimed to have been rehashed.

## Scope and retained history

This report qualifies only the existing five auxiliary workflows on this exact source. It does not establish full integrated-main success, manual NVDA accessibility, physical audio playback/listening, real-model or language quality, real-corpus acceptance, third-party reading-system interoperability, signing or distribution rights. Physical playback is explicitly UNAVAILABLE in the actual audio receipt.

The prior 424cc proof, including its dependency-download TLS failure and later audio retry, remains immutable historical evidence. It supplies unchanged expected contracts only. None of its outcomes is transferred to the current attempt 1, and no earlier failure is erased.

[Complete current JSON proof](pr210-7c991-auxiliary-observed-results-20261128.json)

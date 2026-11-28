# Completed 95d acceptance and native playback evidence

This record is bound to source `95d305d527f8fa4f36202d40fc069d71f2551069`
(tree `7a5b2458a05edb4af3262d482c1df682f1f5b5b4`). It preserves the finished
cycle before the later source corrections. It does not grant a PASS to a successor.

The [machine evidence](20261128-95d-complete-and-endpoint-playback.json)
contains all 28 terminal job identities and full-log SHA-256 values, the exact
405 required source receipts, canonical V3 selections, all saved-artifact
identities, the 18 current-source JUnit skip cases, and the separate endpoint
evaluation. Its SHA-256 is `34f9a870ee5c5556d54a0d4f60111e42c935d8c2e6a7ee0ac5cb7f53ac76efc5`.

## Completed ordinary cycle

| Selection | Actual result | Interpretation |
| --- | --- | --- |
| All ordinary jobs | 26 succeeded; 2 failed; all 28 terminal | There is no whole-head PASS. |
| Required Windows source selection | 405/405 receipts; 10,222 passed; zero failed/skipped | 401 bulk occurrences plus the four first-phase occurrences. |
| Canonical V3 source | 139/139 selected suites; 3,104 passed; zero failed/skipped | Three separate 22-case selection guards also passed. |
| Broad V3 selection | 20/20 selectors; 3,116 passed; zero failed; 18 skipped | Two separate 26-case selection guards also passed. Exact skips are classified below. |
| Integrated desktop | Failed | Real source EPUB playback returned MCI 326. The installed Book scenario exceeded its original 90-second bound. |
| V3 native studio | Failed | Real source EPUB playback returned MCI 326; later native scenarios were not completed. |

Counts overlap across suites and jobs and are not added as unique tests.
The original 404 source occurrence list contains 403 distinct paths; its two OCR
occurrences remain intentional. The extra 99-case manifest guard makes 405
required reports. Metadata stream hashing is included exactly once at
occurrence 207.

All six source shards completed under their unchanged 35-minute source-step limits
inside 60-minute jobs.
The main job's four required source reports also passed. Previous incomplete
405-report coverage on an older commit is superseded only for this exact source.

## Eighteen skips verified from this source's own saved JUnit

Artifact `15252047132` belongs to run `46110898337` and source `95d305d…`.
Its 49,394-byte ZIP SHA-256 is
`d7f55b6c1e80f4dd4642d5845045f3bbcaae128a06873e8b0855f739d2844473`.

Reader `09643075b52fe52a708a4479db7c4c4bf8cb3fb1`,
[run 46117769661](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46117769661),
completed successfully at 06:47:40 UTC on 2026-11-28. The verified 4,204-byte
ASCII report has SHA-256
`651578c270de802ad9d313df44ec499ba9fc42b595bce5730f3c47062054bc58`.
It records 2,333 cases: 2,315 passed, 18 skipped, zero failed.

| Actual group | Cases | Recorded reason |
| --- | ---: | --- |
| `tests.speech.test_runtime` | 6 | Installed FFmpeg lacks the optional Flite filter. |
| `tests.studio.test_production` | 11 | Actual production acceptance requires three installed Flite voices. |
| `tests.studio.test_production_hardening` | 1 | Actual speech requires the installed Flite slt voice. |

Every exact test name, classname, original reason and `pytest.skip` type is
preserved in the machine evidence. These identities were read from this
artifact, not copied from the older f186 result.

Eight of nine raw member readbacks verify. The raw XML transported through the
connector is 19 bytes shorter than the saved 386,835-byte member and fails its
hash. It is excluded without repair. The separately verified structured report
is generated inside the reader from the sealed original XML, whose SHA-256 is
`e8391531528c986d1913240da99a2539fa77fcc7f5a10fe764344a5275e2cc9f`.

## Separate real Windows endpoint evaluation

Source `4e85ee4bfe63bc6f51de11f5cacd75c1040090ef` preserves all 1,357 ordinary
95d files exactly and adds only the six reviewed endpoint helper files and its
diagnostic workflow. All 341 installed Python files were compared with this
exact checkout.

[Run 46111830309](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46111830309),
job `140876841029`, failed at 06:19:06 UTC on 2026-11-28. Its complete
697,454-character terminal log SHA-256 is
`215be9881fd2fbbbf2782ccb1f45daf8b76dc368e120bae86233f980d45c707a`.

The guarded finite evaluation installed the pinned, normally trusted virtual
audio endpoint on a disposable hosted VM. Actual Authenticode and format
queries passed; two distinct waveform outputs were present. No reboot, trust
store, test signing or security-policy change was performed. This experiment
does not authorize recurring use, redistribution or installation on a user
machine.

The unchanged EPUB native scenario completed **all 50 checks**, including exact
Unicode selection, actual WavePlayer synthetic PCM output, pause/resume/stop,
navigation and source-archive cleanup. Human listening and NVDA remain NOT_RUN.

A later ReaderPlayer scenario failed because the old adapter-interface query
gave ambiguous output identities for the two waveform endpoints. This is a
different failure from EPUB playback and motivated the later endpoint-ID fix.

The unchanged installed Book scenario verified its first completed result at
89.078 seconds, issued the second explicit Run approval, then failed at 90.016
seconds while checking saved items. Its full resume/invariance scenario did
not pass. At this source, the later 20 native plus 8 validator ledger and
transfer/Inno paths were not reached.

Saved reader `e7eadf65218db3f5439c67b4209551bb92fe5d74` independently read all
25 selected primary members with exact UTF-8 length and SHA-256, including the
source proof, endpoint receipt, EPUB PASS, ReaderPlayer failure and Book timeout.
The machine evidence records every selected member and the 32 explicitly
absent optional paths.

## Successor work and remaining acceptance

Source `981292a2143f4344acbe393f3ed27a0c7266330b` combines three reviewed
corrections: fresh durable path snapshots without duplicate stat calls;
independent collection of all early installed native outcomes; and opaque
per-endpoint WinMM identities with correct native `size_t` output.

That successor has its own in-progress 28-job cycle. Its four observed
resource-race test failures remain explicit and under correction. The old
95d results are not inherited. Separate bd06 and 32eb runs exercise the
installed bundle and actual endpoint identities on their own exact source.

Full-platform acceptance still requires successful required installed suites,
actual NVDA and listening/language-quality evidence, the authorized real
engines and samples for their claimed capabilities, and applicable signed
distribution and rights review. Synthetic controls or an adapter's presence
do not satisfy those independent requirements.

The [active PR](https://github.com/Oleksii-debug/AudioTacticalFPS/pull/152)
tracks later source and execution outcomes. This document is a completed
historical snapshot; later results are recorded separately.

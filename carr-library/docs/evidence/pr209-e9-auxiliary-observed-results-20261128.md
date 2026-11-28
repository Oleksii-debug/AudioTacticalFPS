# PR209 e9 auxiliary results — 28 November 2026

The five auxiliary jobs for **e9bba260fd66cfe5217f8081778fcdfc9dfd202c**, tree **c6a3df5fe5e6ed328ee6d1552d7360f1b94e0d9e**, have all completed. **The audio job failed.** The other four jobs passed their recorded scopes. This is not a full-platform or integrated-main qualification.

[The complete JSON record](pr209-e9-auxiliary-observed-results-20261128.json) contains the actual function and native-label Counters, receipts, source bindings, complete-log hashes, step outcomes, artifact metadata and scope limits.

| Lane | Run / job | Actual result |
| --- | --- | --- |
| Native launcher | 46107707798 / 140864071003 | 46 exact unittest identities; 9 native checks; 7 source hashes. PASS. |
| Keyboard and recovery | 46107707855 / 140864071260 | 291 source cases across 142 function identities; 146 native checks across five probes; 7 Save All source hashes. PASS. |
| Encrypted snapshots | 46107707910 / 140864071215 | Source pytest summary of 322; installed 154 cases across 68 function identities; 44 native checks. PASS. |
| Bibliography | 46107707920 / 140864071502 | 28 strict suites; 1066 source cases across 420 function identities; 108 native labels; 5 installed source hashes and the current wheel/source receipt. PASS. |
| Audio | 46107707873 / 140864071217 | 2371 collected; 2362 passed, 2 failed, 7 skipped; 22 subtests; 1076.51 seconds. FAIL. |

## Exact audio failure and retained passing subscopes

The only two failing cases are the `deadline` and `cancel` variants of `test_helper_private_directory_cleanup_waits_for_owned_process_and_reader_settlement`. Both reached the assertion requiring an observed real sharing refusal after the expected timeout/interruption had propagated. They used the e9 metadata-only directory handle. The separately reviewed fixture repair requests directory-list/read-data access as well; that correction was not part of this run.

The complete audio function Counter contains **2371 events, 1214 function identities and 104 modules**, with all start/end indices 1 through 2371 present. Its scope is the prior 2266 cases plus helper 10, production-integrity 50 and Studio 45. The 21 new CLI-resource cases are outside this auxiliary workflow's selectors. No failed or skipped case is counted as a pass.

Before source regression failed, the installed audio smoke passed 16 checks. The three strict installed native gates passed effects 151, clips 26 and publication 11; their label Counters, inner/outer receipts, current source/tree and 12 unique installed module SHA256 values reconcile. The physical playback probe reported **UNAVAILABLE: Windows reports no audio output device**. The later installed-library self-test was skipped after the source failure.

Four pytest warnings concern `record_property` with the xunit2 JUnit family: the three long-duration PCM parameter cases and the complete model-runtime inventory case. Their exact warning text is retained in JSON.

## The seven actual audio skips

| Exact case | Recorded reason |
| --- | --- |
| `tests/audio/test_cli_signals.py::test_actual_cli_sigint_preserves_completed_chunks_and_returns_resume_status` | POSIX SIGINT delivery; native Windows cancellation is separately probed. |
| `tests/audio/test_engine_boundaries.py::test_cancellation_stops_descendant_after_its_process_group_leader_exits` | POSIX owned process-group cleanup; Windows uses an owned Job Object. |
| `tests/audio/test_music_adapters.py::test_actual_fluidsynth_renders_original_midi_and_resumes` | Actual installed FluidSynth 2.4.4 / reviewed TimGM6mb fixture is unavailable. |
| `tests/audio/test_music_adapters.py::test_actual_production_scene_selects_fluidsynth_and_rechecks_cached_assets` | Actual installed FluidSynth 2.4.4 / reviewed TimGM6mb fixture is unavailable. |
| `tests/audio/test_playback_platform.py::test_non_windows_reports_unavailable_and_never_starts_fallback_process` | Windows uses its native device gate |
| `tests/audio/test_playback_platform.py::test_actual_native_winmm_playback_controls_or_explicit_missing_device` | Windows reports no audio output device. |
| `tests/audio/test_playback_platform.py::test_actual_ffplay_headless_decode_and_queue_completion` | Actual ffplay dummy-device probe requires an installed POSIX ffplay. |

These remain skips. The document does not convert unavailable hardware, private assets or platform-specific tests into successful execution.

## Evidence boundaries

Every lane is bound to its actual e9 checkout, run and completed job. The bibliography wheel receipt binds e9 and its exact source tree; the native receipts retain their actual installed-source hashes. Prior c31 source contracts were reused only after exact Git-blob comparison, and all runtime Counters were read afresh.

The complete decoded job logs were read and hashed, preserving BOM and line endings; their UTF-8 byte counts are recorded separately from character counts. Uploader and GitHub API artifact IDs, byte sizes and SHA256 digests agree. **No artifact ZIP was downloaded or decoded.** The launcher workflow intentionally uploads no binary artifact. Snapshot source 322 is supported by its printed pytest summary; the complete printed function Counter is available for its installed 154 cases.

The independent integrated-main and final-artifact requirements remain separate. No result here qualifies the corrected 424cc successor, manual NVDA, physical listening, real-model/language/corpus quality or redistribution rights.

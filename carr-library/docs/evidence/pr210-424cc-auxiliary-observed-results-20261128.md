# Current 424cc auxiliary Windows results

All five auxiliary lanes completed successfully for source commit `424cc456992c69c1ee945bc4d5ab73bc21e4e7b8`, tree `be936242ecf2aa62ab3344de8874a92605aea39e`. The audio lane passed on attempt 2 after a retained dependency-download TLS failure on attempt 1. This is an auxiliary qualification record; it does not assert completion of the integrated main cohort or final archive verification.

The complete decoded-log Counters, receipts, source bindings, step outcomes and artifact metadata are in [the accompanying JSON](pr210-424cc-auxiliary-observed-results-20261128.json). No archive was downloaded, decoded, imported or executed by this verification.

## Actual execution

| Lane | Run / job / attempt | Source or installed population | Native scope | Result |
| --- | --- | --- | --- | --- |
| Launcher | 46109011463 / 140868122239 / 1 | 46 exact unittest identities: 19 launcher + 27 bundle | 9 checks; 7 source hashes | PASS |
| Keyboard | 46109011419 / 140868122004 / 1 | 291 cases, 142 function identities, 11 suites | 35+23+15+38+35 = 146 checks; 7 Save All source hashes | PASS |
| Encrypted snapshots | 46109011367 / 140868121476 / 1 | Source 322 from printed summary; installed 154 cases / 68 function identities | 44 checks; 2 installed origins | PASS |
| Bibliography | 46109011354 / 140868121653 / 1 | 1,066 cases, 420 function identities, 28 suites | 108 labels; 5 source hashes; current-source wheel | PASS |
| Audio | 46109011406 / 140871488840 / 2 | 2,364 passed, 0 failed, 7 skipped, 22 subtests, 4 warnings; 1,827.96s | Installed audio 16; effects 151; clips 26; publication 11; installed-library 9 | PASS |

The audio source population has exactly 2,371 ordered starts and ends, 1,214 function identities and 104 modules. It preserves the earlier 2,266 population and adds 105 selected cases: 10 helper cleanup, 50 completed-silence backend and 45 Studio UI cases. The separately added 21 CLI resource cases are not selected by this auxiliary workflow. Both corrected helper deadline/cancel cases passed; every other case identity is retained.

Native effect labels are counted with multiplicity. Every inner terminal native receipt equals the enclosing gate receipt, and all 12 recorded installed module SHA256 values match the exact candidate source. The final nine-check installed-library self-test also passed. Physical playback reports no output device; no listening or NVDA result is inferred.

The snapshot source 322 count is a printed pytest summary, not an independently read complete source function Counter. Its installed 154 Counter and native 44 labels were independently reconstructed from the raw log.

## Retained first audio attempt

Run 46109011406 attempt 1, job 140868121929, failed in dependency installation while downloading `pywin32-312-cp311-cp311-win_amd64.whl`. The recorded error is `SSL: DECRYPTION_FAILED_OR_BAD_RECORD_MAC`; pip exited 1. All seven subsequent test/native steps were skipped. The uploader reported no files, matching the empty artifact API result. No product defect or underlying network cause is inferred from that traceback.

Its complete decoded log is 39,776 UTF-8 bytes, SHA256 `0920de267234e9247d5ee65631f1f4b4b6a8f2583e60e3a08c76244da3610812`. The authorized retry reran only that job on the same immutable source; no reference, source, assertion, timeout or gate changed for the retry.

Historical e9 remains a separate failure record: its audio source run had 2,362 passed, 2 failed, 7 skipped and 22 passed subtests. Current 424cc success does not rewrite that result.

## Exact audio skips

| Case | Actual reason |
| --- | --- |
| `tests/audio/test_cli_signals.py::test_actual_cli_sigint_preserves_completed_chunks_and_returns_resume_status` | POSIX SIGINT delivery; native Windows cancellation is separately probed. |
| `tests/audio/test_engine_boundaries.py::test_cancellation_stops_descendant_after_its_process_group_leader_exits` | POSIX owned process-group cleanup; Windows uses an owned Job Object. |
| `tests/audio/test_music_adapters.py::test_actual_fluidsynth_renders_original_midi_and_resumes` | Actual installed FluidSynth 2.4.4 / reviewed TimGM6mb fixture is unavailable. |
| `tests/audio/test_music_adapters.py::test_actual_production_scene_selects_fluidsynth_and_rechecks_cached_assets` | Actual installed FluidSynth 2.4.4 / reviewed TimGM6mb fixture is unavailable. |
| `tests/audio/test_playback_platform.py::test_non_windows_reports_unavailable_and_never_starts_fallback_process` | Windows uses its native device gate |
| `tests/audio/test_playback_platform.py::test_actual_native_winmm_playback_controls_or_explicit_missing_device` | Windows reports no audio output device. |
| `tests/audio/test_playback_platform.py::test_actual_ffplay_headless_decode_and_queue_completion` | Actual ffplay dummy-device probe requires an installed POSIX ffplay. |

Four warnings retain the actual pytest `record_property` / `junit_family=xunit2` incompatibility messages: three long-streaming parametrizations and one model-manifest case. They are retained in the JSON rather than reported as zero warnings.

## Artifact and log bindings

| Lane | Decoded UTF-8 bytes | Complete decoded-log SHA256 | Artifact |
| --- | ---: | --- | --- |
| Launcher | 46605 | `1d011a120533dae928f662c9f7b8b72ea1f01e2d6f5613beef700956d1843cf2` | None; this workflow declares no uploader |
| Keyboard | 162104 | `e34dd19b197dc16b197ad202f068d1aa346d36e9c4ec01468afb0d3b91e626ea` | 15248972282; 11413 bytes; `bd302436426dc45d266d019d36f9242d34ef334ef2f34034ca1b385ddff283d6` |
| Snapshots | 73702 | `8b63a69491e6f9c1ca95018009d6a91c67e9a286800bbf278b9ad4a119e38e5b` | 15249161212; 10079 bytes; `bf8c2ea6fbc804c5ee4b2291c9a4baac9d2dafa3048eb6cb831ac06fd3fef1bf` |
| Bibliography | 506889 | `3261a7295ad1bd76e1e12b28ab8fd4e3309defab420e761aa3d37543febd8829` | 15248792679; 22865 bytes; `624356812b6dd1573758cccce99ce805a3f508a6c9326b65577f5e6274aad2d6` |
| Audio attempt 2 | 1487914 | `311f81c998dc0a9de47a15d49e476e8e485fb9838b11665c8472983767c816a4` | 15250062205; 56947 bytes; `4898bd730f574dea55e5def466f95f5753669569cda3a03fda625b42b549e9be` |

For each of the four uploaded artifacts, the job uploader's ID, byte count and SHA256 match the API metadata and the current source head. These are recorded archive identities, not an independent ZIP content read. The bibliography wheel receipt binds SHA256 `3e0316175af1a1e9cf3dd228aa7ece7aa9c357312bd966c3622738d9c48bea9a` to current 424cc source/tree; this verifier has not downloaded that wheel.

## Qualification boundary

The five auxiliary jobs are successful within the exact scopes above. The integrated main job 140868122250, its protected source/installed inventories and independent final artifact read remain separate. No added qualification is claimed for manual NVDA, physical listening, real private models, language/corpus quality, publisher authority or redistribution rights. Earlier failures remain recorded.

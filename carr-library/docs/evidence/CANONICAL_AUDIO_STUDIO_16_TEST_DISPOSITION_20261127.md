# Canonical audio, Studio and conversion test disposition

Canonical source: `e5c64a5245583f8f8b404a0f042f6ca438c24d7f` (tree `344567db5c0d99f5d6d7ad24c105645ea75b65e8`). Current comparison source: `5070b0e9808e1242936fac314bab9a8d9f20213a` (tree `66231ae8ebc92073e63bf5998be75a786f7973de`).

All 16 assigned files were read against their exact Git blobs. They contain 83 test-function definitions; this number is not a parameter-expanded case count. The JSON companion records each old assertion group, current product owner, exact test selector and source blob. Source mapping is complete for this bounded assignment. Runtime acceptance of the newly added components remains pending.

## File disposition

| Canonical path under tests | Functions | Current owners | Disposition |
| --- | ---: | --- | --- |
| audio/test_audio_process.py | 12 | audio/recording_process.py, audio/engines.py, audio/windows_process.py, audio/cleanup.py, audio/jobs.py | new_regression_runtime_pending; mapped_to_current_contract |
| audio/test_bundled_runtime_settings.py | 4 | audio/runtime.py, runtime_settings.py, ui/audio_studio.py | mapped_to_current_contract |
| audio/test_dsp.py | 21 | audio/ffmpeg.py, audio/models.py, audio/clip_audio.py, audio/music.py, audio/jobs.py | mapped_to_current_contract; new_regression_runtime_pending |
| audio_acceptance/__init__.py | 0 |  | Package marker; no executable test contract |
| audio_acceptance/oracle.py | 0 | audio/ffmpeg.py, audio/production.py, audio/media_conversion.py | test_helper_replaced |
| audio_acceptance/test_primary_publication_budget.py | 3 | audio/jobs.py, audio/production.py, ui/audio_studio.py, operations/admission.py | current_policy_and_tests_split; old_service_owner_replaced |
| conversion/fixtures/ordered_slides.pptx | 0 | conversion/service.py, conversion/worker.py, extract/document_formats_xml.py | exact_fixture_port_runtime_pending; mapped_to_current_contract |
| operations/test_production_admission.py | 4 | operations/admission.py, audio/production.py, audio_cli.py, audio/backup.py | mapped_to_current_contract |
| studio/test_music_scheduling.py | 2 | audio/music.py, audio/production.py, audio/models.py | mapped_to_current_contract; explicit_current_checkpoint_policy |
| studio/test_production.py | 11 | audio/production.py, audio/models.py, audio/jobs.py, audio/ffmpeg.py, audio/clip_audio.py | mapped_to_current_contract |
| studio/test_production_cancellation_outcomes.py | 3 | audio/production.py, audio/jobs.py, ui/audio_studio.py, progress.py | mapped_to_current_contract; current_type_gap_fixed_runtime_pending |
| studio/test_production_hardening.py | 4 | audio/production.py, audio/jobs.py, audio/engines.py | product_gap_fixed_runtime_pending; mapped_to_current_contract; peer_component_source_go_runtime_pending |
| studio/test_studio_controller.py | 6 | ui/audio_studio.py, audio/managed.py, audio/models.py, audio/legacy_project.py | mapped_to_current_contract |
| ui/test_studio_keyboard_registration.py | 1 | ui/audio_studio.py, ui/builtin_keyboard_commands.py, ui/keyboard_settings.py | restored_current_routes_runtime_pending |
| ui/test_studio_progress_isolation.py | 8 | ui/audio_studio.py, ui/operation_progress.py, progress.py | mapped_to_current_contract; mapped_plus_detached_completion_repair_pending; repaired_component_runtime_pending |
| ui/test_studio_progress_results.py | 4 | ui/audio_studio.py, ui/operation_progress.py, progress.py | mapped_to_current_contract; mapped_plus_typed_cancellation_repair_pending; repaired_component_runtime_pending |

## Concrete corrections

| Component | Correction | Added cases | Runtime |
| --- | --- | ---: | --- |
| `61adc656bc863db40e2ea2f8487eb0c74eb6526e` | Completed speech returned at a cancellation boundary was discarded before its reusable checkpoint. | 10 | Pending in this audit |
| `7f901654c2257cc1d4796bcab2e54d038a916186` | Canonical actual-child process bounds and concrete signal measurements lacked direct current-family regressions. | 15 | Pending in this audit |
| `e0ba9cfe516eaa2533db752e7db28b94bff7d5b8` | Full canonical producer-created presentation fixture was absent from current relationship-order coverage. | 1 | Pending in this audit |
| `ba68cf6b94d19b4b5dbcec22772258d864cb0602` | Closed/stale Studio callbacks did not settle their captured shared progress; DSP cancellation was not recognized alongside speech cancellation. | 8 | Pending in this audit |
| `6e6d2599f76b489b8081f3d453afbe8c1e477f9a` | Compatibility SAPI host selection did not enforce the exact architecture-specific serviced OS path. | 34 | Pending in this audit |

## Deliberate current differences and evidence limits

- An absent retired Python module or helper is not automatically a missing user feature. This ledger names the current API and concrete tests instead of importing an alternate process, job or backup framework.
- The old external ffprobe/full-file publication helper and exact 4-to-32-MiB subprocess budget population were not executed here; current measured PCM, cache, admission and publication assertions are individually mapped.
- Whole-score duration and disk budget participate in current typed checkpoint/run identities. Cross-duration same-destination resume and same-completed-run identity after a budget change are not claimed.
- Keyboard semantic routes use the current command identities; an old literal nine-item inventory/default ordering is not claimed unchanged.
- The parent composition and its actual Windows source, installed and native receipts remain the acceptance authority. Component source review cannot supply a missing runtime PASS.

## Per-file notes

### audio/test_audio_process.py

Canonical blob: `8a1cbc082a9fcb1cd1647e5b658c48a5810587db` (4523 bytes).

- The old audio.process module is not imported or reintroduced. Current capture/converter work retains the engine launcher and CleanupOwner. The FFmpeg file-log runner separately retains test_ffmpeg_process_boundaries.

### audio/test_bundled_runtime_settings.py

Canonical blob: `1e48791b33ae256ff5ede4ac7c24dd64afb78beb` (3853 bytes).

- The old audio_runtime()/voice_registry() factories and ffprobe settings field are replaced by current runtime selection and typed engine profiles. Their old signatures and persisted executable paths are not claimed active. Current EngineProfileStore preserves legacy settings as inert bytes.

### audio/test_dsp.py

Canonical blob: `8522fa552beba8910aa2e749c19369eb4f90e09a` (14277 bytes).

- Current Effect stores validated parameter tuples. Old DSP adapter signatures are not claimed compatible; the current product still owns bounded filter graphs and native workers.

### audio_acceptance/__init__.py

Canonical blob: `0d40b5d7b4211ff87600e42ad634e576f53aa403` (82 bytes).

- This 82-byte package marker has no test or product behavior. It is not recreated as a second test framework.

### audio_acceptance/oracle.py

Canonical blob: `2b3b7e80a1da81324a21f0895bc344006f0c89cf` (8502 bytes).

- Canonical helpers file_sha256/probe_pcm/decode_pcm/sequence_pcm/verify_publication are test utilities, not shipped APIs. The old ffprobe rational-time-base and complete external-decoder helper are not claimed byte-for-byte equivalent or rerun here; current assertions and their limits are listed independently.

### audio_acceptance/test_primary_publication_budget.py

Canonical blob: `405552e3ef7f5494678a7a583b42cf0eb84cf9a4` (15662 bytes).

- The old three subprocess scripts are not counted as three executed current cases. Their exact 4→32 MiB and ffprobe/full-FLAC oracle populations have not been replayed here.
- Current disk_budget_bytes is a typed ProductionPlan field and therefore part of the exact run identity. Raising it preserves shared content-addressed chunks, but changing it is not advertised as the same completed run; resource saturation replay keeps the exact plan.
- The retired ProductionService.close API is not emulated. Current VoiceRegistry has no persistent native process to close; current _run/recording_process, playback and UI executor lifetimes own their actual resources.

### conversion/fixtures/ordered_slides.pptx

Canonical blob: `58b0d7cce78a50883ed4bcf1e73cbbcc40675d44` (29150 bytes).

- Canonical usage is tests/conversion/test_conversion.py::test_presentation_order_uses_relationship_order, blob e8cae855b1d64bc85fe06b945f840976f07c67a3.
- Component e0ba9cfe copies the existing canonical Git blob rather than regenerating it or asserting the minimal ZIP is byte-equivalent. Installed test staging must include this binary fixture.

### operations/test_production_admission.py

Canonical blob: `ad7403a7e65033c8777a8804988d474a8776c948` (8502 bytes).

- No canonical saved admission document becomes a live current ResourceLease; current broker/project/backup schemas remain authoritative.

### studio/test_music_scheduling.py

Canonical blob: `f60561a823a3c6757c84aad83b40cfa67b53f3a0` (1158 bytes).

- The old music_segments(MusicSpec, rate, 30) generator is absent. Procedural/FluidSynth uses four-bar chunks, MusicGen 25 seconds plus context, ACE-Step its reviewed chunk_seconds.
- Current checkpoint settings bind whole-score frames. Changed duration requires a new destination; cross-duration checkpoint reuse is not claimed from the old pure-generator prefix test. No neural inference/quality claim follows.

### studio/test_production.py

Canonical blob: `2036c36cab2fe931d72014ad75eabaf8381bcd6c` (15992 bytes).

- Current run_key/complete.json/render-journal.json replace the old ProductionService node schema. Old enum/receipt layouts are not claimed identical; exact current-plan/source hashes are authoritative.

### studio/test_production_cancellation_outcomes.py

Canonical blob: `1ce1d7a0852158c3a59f8fa071867930369cbbd1` (5526 bytes).

- Current render-journal uses cancelled, and shared progress uses cancelled/paused/complete/failed; old service pause-vs-cancel enum fields are not claimed structurally identical. Exact typed outcomes, retained chunks and publication point remain authoritative.

### studio/test_production_hardening.py

Canonical blob: `79fda93171f594bb0554a6a48096201c1c734d4d` (4916 bytes).

- Completed-speech component61adc is already in source5070. Compatibility-host component6e6d2599 is separately source-reviewed; its new engine blob4c4c318b is not falsely represented as present in baseline5070 blob13bd4832.
- Native COM remains the ordinary 64-bit SAPI owner; the explicit PowerShell compatibility host has its own narrow path exception. General engine/model path policy is unchanged.

### studio/test_studio_controller.py

Canonical blob: `5182c9804d169e1a64a77dd8dbd2b96b073616d4` (3283 bytes).

- The canonical ProjectStore/controller layout is replaced by the current managed JSON/CAS service plus explicit legacy-project import. This is a behavior mapping, not an assertion that retired callable signatures or serialized documents are byte-compatible.

### ui/test_studio_keyboard_registration.py

Canonical blob: `49a5178e9627b1766eebc7485a3855f87bb443a3` (2989 bytes).

- Source component 104bf86e1ea5900c316758ae19c60b9d15d64905 is integrated at the 5070 baseline. Four parameter-expanded cases are authored in the current keyboard module.

### ui/test_studio_progress_isolation.py

Canonical blob: `aa28e07dad812e94e4c5d2f53fb029fd308f3747` (11887 bytes).

- The ba68cf6b94d19b4b5dbcec22772258d864cb0602 successor is separate from the 5070 source baseline. Its captured ProgressReporter settlement avoids touching destroyed controls or a replacement task; it has not been executed in this audit.

### ui/test_studio_progress_results.py

Canonical blob: `54684fa708f027d8e7c5e456186ec2874f27b956` (5084 bytes).

- The original dropped closed-dialog canonical regression revealed a real current gap. The ba68 successor fixes the existing completion callback and adds eight parameter-expanded cases; no runtime PASS is inferred from source review.

## Verification boundary

The ledger builder verified all 83 canonical function names were covered and all current selectors existed at their recorded blobs. Exact canonical fixture reuse preserves the existing Git object rather than a regenerated approximation. These are source checks. No local Python execution, native-window pass, signal result or publication result is inferred from them.

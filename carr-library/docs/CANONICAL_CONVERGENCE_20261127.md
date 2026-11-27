# Canonical convergence — 2026-11-27

## Reviewed identities and authority

This ledger records reconciliation into the current completion candidate. It does not change the product requirements or declare a qualified release.

- Original comparison base: `3beebd5aca900b8bf2e86498ae79db4015c96705`.
- Reviewed shared-services main: `e69ba2403515f21509686d66f4bc8b3673756d81` (24 commits, 104 changed paths).
- Subsequent canonical export correction: `e3cf74c243d4ef930b526b5648ca9003c0900057`; its six-commit ancestry changes the export approval guard and regression file. The earlier root already contained the guard; the additional four before-open cases are retained.
- Selected completion checkpoint before this reconciliation: `ad679edd1b326f00d073e8994b0e2b2158a374ca`, tree `5ca9f32acc354e3b44b39a8d6a5ddcb062f639e0`.
- Export/native scope component: `5a11469a599cf6eaf666abf2e065c7485ea9e414`.
- Supplied ASR backend: `8840be97ce67c099a23227a82fd19cb7ba078a17` plus final `6a9b5d96b0a7286d3c6a51b03a869a1c423daedf`; native/CLI: `65a2c287e2c1d31607538a1474616216f9fa0b19`.
- Observed-CI contract corrections: `0d7f41c5b8739256b486da31007ce3b1d530d8be` (owned monitor fixture pointers) and `d0c38b8ff1d173c218bc1c7c8cc9ff9279180ffb` (ASR durable control and restore-review assertions).

Source selection was by behavior and exact file review. The existing flat supplied-worker service, one resource broker, one native desktop, current durable stores and QuickJS-ng CSL remain canonical. The alternative transcription package would shadow the existing module and therefore is not installed alongside it.

## File-level disposition

Paths in the resolved column are relative to `carr-library/` unless they start with `.github/`. A retained alternative is an explicit integration decision, not a claim that omitted tests ran unchanged.

| Canonical changed path | Canonical blob | Resolved path / behavior | Decision |
| --- | --- | --- | --- |
| `.github/workflows/carr-bibliography.yml` | `6d0ee34dfcbc71c8570711e34c81dca65c4eea70` | `.github/workflows/carr-bibliography.yml` | Union required gates and exact installed-origin probes; keep strict zero-skip and actual owned lifetime acceptance. |
| `.github/workflows/carr-native-launcher.yml` | `c65208de5275b74ec1a9378d5186c147763fc790` | `.github/workflows/carr-native-launcher.yml` | Union required gates and exact installed-origin probes; keep strict zero-skip and actual owned lifetime acceptance. |
| `.github/workflows/carr-v3.yml` | `700a88782aad660cdd06bb0814d1b47e31c23ebe` | `.github/workflows/carr-v3.yml` | Union required gates and exact installed-origin probes; keep strict zero-skip and actual owned lifetime acceptance. |
| `.github/workflows/carr-windows.yml` | `986a17cfc5d7fcfd78552462556f8eee31110157` | `.github/workflows/carr-windows.yml` | Union required gates and exact installed-origin probes; keep strict zero-skip and actual owned lifetime acceptance. |
| `carr-library/docs/assistant_runtime.md` | `8008718f70c71d747a683f5c08e972d94646d7cf` | `docs/assistant_runtime.md` | Retain current service documentation and reconcile status/entrypoints through this ledger; historical execution counts are not current qualification. |
| `carr-library/docs/REUSE_DECISIONS_V2.md` | `771d901cb7f7343df41fcbbabc7b1bf1f69c15cd` | `docs/REUSE_DECISIONS_V2.md` | Retain current service documentation and reconcile status/entrypoints through this ledger; historical execution counts are not current qualification. |
| `carr-library/docs/RUNNING.md` | `d2a2da18aff0117ab20cca466aab37ee3fd84d63` | `docs/RUNNING.md` | Retain current service documentation and reconcile status/entrypoints through this ledger; historical execution counts are not current qualification. |
| `carr-library/docs/runtime_resources.md` | `1535d48a97fa7fd4148b1eeeb5d5746b36221236` | `docs/runtime_resources.md` | Retain current service documentation and reconcile status/entrypoints through this ledger; historical execution counts are not current qualification. |
| `carr-library/docs/speech_runtime.md` | `78dc3b52f5605c235d7f88b8c686a34ae6a16462` | `docs/speech_runtime.md` | Retain current service documentation and reconcile status/entrypoints through this ledger; historical execution counts are not current qualification. |
| `carr-library/docs/START_UK.txt` | `1dea2ddb25632f3864b66bee4ad3e43cf9c68318` | `docs/START_UK.txt` | Retain current service documentation and reconcile status/entrypoints through this ledger; historical execution counts are not current qualification. |
| `carr-library/docs/START_V3_UK.txt` | `5778be0192ca10633d76df704f046dafc4775f1b` | `docs/START_V3_UK.txt` | Retain current service documentation and reconcile status/entrypoints through this ledger; historical execution counts are not current qualification. |
| `carr-library/docs/transcription_runtime.md` | `1061f777d7edd90bca579315e060214291045a7e` | `docs/transcription_runtime.md` | Retain current service documentation and reconcile status/entrypoints through this ledger; historical execution counts are not current qualification. |
| `carr-library/docs/translation_workstation.md` | `b8339ab9a5676f4b483316761b5e40bca5147579` | `docs/translation_workstation.md` | Retain current service documentation and reconcile status/entrypoints through this ledger; historical execution counts are not current qualification. |
| `carr-library/docs/V3_RELEASE_READINESS.md` | `69e33ddeebc242cc68469e8520c4e869638c3c4f` | `docs/V3_RELEASE_READINESS.md` | Retain current service documentation and reconcile status/entrypoints through this ledger; historical execution counts are not current qualification. |
| `carr-library/packaging/CI_ENTRYPOINT.md` | `2439430d4347faf54b29c4d87b288e6ab8746e1b` | `packaging/CI_ENTRYPOINT.md` | Retain current service documentation and reconcile status/entrypoints through this ledger; historical execution counts are not current qualification. |
| `carr-library/packaging/windows-sbom.cdx.json` | `a18692577c78ff4dc053cc0ca9be284966409b53` | `packaging/windows-sbom.cdx.json` | Align 0.2.0.dev2; retain reviewed QuickJS-ng CSL, richer packaging closure and operations package; omit shadowing transcription package. |
| `carr-library/packaging/windows-semantic-sbom.cdx.json` | `a7e536afbe13617942188ed41a673b6013802c48` | `packaging/windows-semantic-sbom.cdx.json` | Align 0.2.0.dev2; retain reviewed QuickJS-ng CSL, richer packaging closure and operations package; omit shadowing transcription package. |
| `carr-library/pyproject.toml` | `ab769a044f386e8ece581b03a4f18481bbe79d04` | `pyproject.toml` | Align 0.2.0.dev2; retain reviewed QuickJS-ng CSL, richer packaging closure and operations package; omit shadowing transcription package. |
| `carr-library/scripts/windows_local_planning_smoke.py` | `1af86d5b9e55a8611346ddf5ce07e11c857260e3` | `scripts/windows_local_planning_smoke.py` | Retain selected local-planner proposal/review, exact model/profile binding, typed execution and owned native lifecycle. |
| `carr-library/scripts/windows_platform_tools_smoke.py` | `2a35c468ec377ebe2c9111526ddcca4fa3338ecb` | `scripts/windows_transcription_smoke.py`; `windows_operations_smoke.py`; `windows_local_planning_smoke.py`; `windows_translation_v3_smoke.py` | Use dedicated native probes for the selected canonical APIs and their cleanup tests; omit the package-shaped combined driver. |
| `carr-library/scripts/windows_translation_v3_smoke.py` | `8c03ad29b98ba215415ea9b51a97d9f343481b6a` | `scripts/windows_translation_v3_smoke.py` | Retain Advanced v3 model/glossary/OAuth and chunk approvals; preserve stronger transport deadlines, exact source binding and passive backup checks. |
| `carr-library/src/carr_library/__init__.py` | `ee09576cfebe843794b34e7609dd3bb1b86b59a0` | `src/carr_library/__init__.py` | Adopt canonical version 0.2.0.dev2. |
| `carr-library/src/carr_library/__main__.py` | `1a668e92de24009bed30cbd52bca3ceac44e6d86` | `src/carr_library/__main__.py` | Retain guarded launcher; provide both transcription and translation aliases plus existing operations routing. |
| `carr-library/src/carr_library/assistant/llm.py` | `ee73d023bc8bb8932239a4e569f6a5e812dcf1a5` | `src/carr_library/assistant/llm.py` | Retain selected local-planner proposal/review, exact model/profile binding, typed execution and owned native lifecycle. |
| `carr-library/src/carr_library/assistant/profiles.py` | `68ebee4c87963b6c0728be5dc2e2e82f0c79d7fa` | `src/carr_library/assistant/profiles.py` | Retain selected local-planner proposal/review, exact model/profile binding, typed execution and owned native lifecycle. |
| `carr-library/src/carr_library/assistant/providers.py` | `b46ad52dad1c034ab79c2c333cf33354225abc79` | `src/carr_library/assistant/providers.py` | Retain Advanced v3 model/glossary/OAuth and chunk approvals; preserve stronger transport deadlines, exact source binding and passive backup checks. |
| `carr-library/src/carr_library/assistant/service.py` | `946a1a28ced7de30d97893e7edb8b01333b986c1` | `src/carr_library/assistant/service.py` | Retain selected local-planner proposal/review, exact model/profile binding, typed execution and owned native lifecycle. |
| `carr-library/src/carr_library/assistant/translation_cli.py` | `eadf1f502df05f3db7ff998c28fcea98f3573423` | `src/carr_library/assistant/translation_cli.py` | Retain Advanced v3 model/glossary/OAuth and chunk approvals; preserve stronger transport deadlines, exact source binding and passive backup checks. |
| `carr-library/src/carr_library/assistant/translation.py` | `485da14ea15b3ce10b68217a1b42ac9ee872f330` | `src/carr_library/assistant/translation.py` | Retain Advanced v3 model/glossary/OAuth and chunk approvals; preserve stronger transport deadlines, exact source binding and passive backup checks. |
| `carr-library/src/carr_library/audio/process.py` | `c05538de93ec0b5a85f2955ca0d2b58670d7a186` | `src/carr_library/audio/process.py` | Port diagnostic-thread allocation inside owned process cleanup, retaining existing Job/process guards; actual allocation/start failure cases passed ad679. |
| `carr-library/src/carr_library/backup_producers.py` | `ea6c5c8b195ec5e4bce874c051d10d71df61cba4` | `src/carr_library/backup_producers.py` | Retain common v4 with explicit legacy adapters, complete typed studio/reader/ASR state, immutable originals and restore trust revocation; no second backup model. |
| `carr-library/src/carr_library/backup_sidecars.py` | `a3e0c46a11f86c576ca98d4190b1248b505c00dc` | `src/carr_library/backup_sidecars.py` | Retain common v4 with explicit legacy adapters, complete typed studio/reader/ASR state, immutable originals and restore trust revocation; no second backup model. |
| `carr-library/src/carr_library/backup_transcription.py` | `245ac2aab7a40e12a53008c0f72a45b5912c59f2` | `src/carr_library/backup_transcription.py` | Adopt reviewed 6a9b5d96 backend / 65a2c287 native and CLI convergence; retain exact scope, reviewed environment and no automatic fallback. |
| `carr-library/src/carr_library/durable_state.py` | `030ecb184c76c17bc0913758d7ac81de284661b2` | `durable_state.py`; `windows_state.py`; `tests/storage/test_state_replacement.py`; `test_durable_state_windows_read.py` | Retain owned Windows handle replacement and real sharing/denial barriers; do not replace with the alternative os.replace retry strategy. |
| `carr-library/src/carr_library/library_backup.py` | `b585cf04946caa2db6ec103b5a2eaf6e4d13d7fc` | `src/carr_library/library_backup.py` | Retain common v4 with explicit legacy adapters, complete typed studio/reader/ASR state, immutable originals and restore trust revocation; no second backup model. |
| `carr-library/src/carr_library/operations_cli.py` | `b03cf6a4668e88757193b7a8e94fe6c5557124ea` | `src/carr_library/operations_cli.py → operations/cli.py` | Compatibility forwarding module; one canonical command implementation. |
| `carr-library/src/carr_library/operations/__init__.py` | `618d53c90f9586156fa7b0d84d11483d16d6d04b` | `src/carr_library/operations/__init__.py` | Retain richer shared broker/facade and same production/CLI service, exact workspace authorization, durable controls and cleanup-held leases. |
| `carr-library/src/carr_library/operations/_locks.py` | `fcf6872d1b5b8a131bf33edd2e42fd0163b9afd9` | `src/carr_library/operations/_locks.py` | Retain richer shared broker/facade and same production/CLI service, exact workspace authorization, durable controls and cleanup-held leases. |
| `carr-library/src/carr_library/operations/broker.py` | `6a8854e18316521939848bd1719e9ab0484c0746` | `src/carr_library/operations/broker.py` | Retain richer shared broker/facade and same production/CLI service, exact workspace authorization, durable controls and cleanup-held leases. |
| `carr-library/src/carr_library/operations/facade.py` | `4eaf0157b171b4ae4fdd9c2a8e4da06b6700cd48` | `src/carr_library/operations/facade.py` | Retain richer shared broker/facade and same production/CLI service, exact workspace authorization, durable controls and cleanup-held leases. |
| `carr-library/src/carr_library/operations/models.py` | `2cbf7ebddfa4492ca2b0774a1cf9e6be42f915af` | `src/carr_library/operations/models.py` | Retain richer shared broker/facade and same production/CLI service, exact workspace authorization, durable controls and cleanup-held leases. |
| `carr-library/src/carr_library/pending_backup.py` | `2a15b362f3b25cefeefdfa5f6edcb8527cd35e43` | `src/carr_library/pending_backup.py` | Adopt canonical regression/legacy compatibility changes while retaining current publication ownership. |
| `carr-library/src/carr_library/research/export_file.py` | `cb6eb7b4b93f9eaec8feca9586742d3ffd9d1afa` | `src/carr_library/research/export_file.py` | Retain bounded complete-content, no-clobber, cancellation and Windows ChangeTime checks; port receipts and authorization-before-read regression. |
| `carr-library/src/carr_library/studio/cli.py` | `714af31ab62055f24ecbd04b0c8c95a202195d98` | `src/carr_library/studio/cli.py` | Retain richer shared broker/facade and same production/CLI service, exact workspace authorization, durable controls and cleanup-held leases. |
| `carr-library/src/carr_library/studio/render.py` | `0c05eebaa029d43bf83bdd35ec0de36a93edd198` | `src/carr_library/studio/render.py` | Retain richer shared broker/facade and same production/CLI service, exact workspace authorization, durable controls and cleanup-held leases. |
| `carr-library/src/carr_library/transcription_cli.py` | `5ba20a26633653e23e658b611a3e61a663d301d3` | `src/carr_library/transcription_cli.py` | Adopt reviewed 6a9b5d96 backend / 65a2c287 native and CLI convergence; retain exact scope, reviewed environment and no automatic fallback. |
| `carr-library/src/carr_library/transcription/__init__.py` | `61bc62f8984afaffbde4073c49812738321f018b` | `src/carr_library/transcription.py`; `transcription_runtime.py` | Reconcile into the existing supplied-worker service; omit the shadowing second package. |
| `carr-library/src/carr_library/transcription/models.py` | `e744e7c0c0b7cbaf47d2cb926f9fca81690c54ab` | `src/carr_library/transcription.py`; `transcription_runtime.py` | Reconcile into the existing supplied-worker service; omit the shadowing second package. |
| `carr-library/src/carr_library/transcription/runtime.py` | `99b1a086122b75ae2f7fc48dbe3b4109db059c0a` | `src/carr_library/transcription.py`; `transcription_runtime.py` | Reconcile into the existing supplied-worker service; omit the shadowing second package. |
| `carr-library/src/carr_library/transcription/service.py` | `e12335721580f9e28db3396278dc68861f528252` | `src/carr_library/transcription.py`; `transcription_runtime.py` | Reconcile into the existing supplied-worker service; omit the shadowing second package. |
| `carr-library/src/carr_library/ui/academic_actions.py` | `9311eb9f272f11dbd166681b3a34aea6d75c5138` | `src/carr_library/ui/academic_actions.py` | Use reviewed export component 5a11469a: queued library/source checks and truthful receipt/cleanup notice; retain reader, keyboard and lifetime extensions. |
| `carr-library/src/carr_library/ui/academic_library_dialog.py` | `52b328714a72fb0d650789477c28082e33dc2647` | `src/carr_library/ui/academic_library_dialog.py` | Use reviewed export component 5a11469a: queued library/source checks and truthful receipt/cleanup notice; retain reader, keyboard and lifetime extensions. |
| `carr-library/src/carr_library/ui/assistant_dialog.py` | `930c84fe473a2de22be6386077d197f1ee813720` | `src/carr_library/ui/assistant_dialog.py` | Retain selected local-planner proposal/review, exact model/profile binding, typed execution and owned native lifecycle. |
| `carr-library/src/carr_library/ui/catalog_insights_actions.py` | `f61007a75fd87111466cbeffb7b8ffe511b97bf9` | `src/carr_library/ui/catalog_insights_actions.py` | Use reviewed export component 5a11469a: queued library/source checks and truthful receipt/cleanup notice; retain reader, keyboard and lifetime extensions. |
| `carr-library/src/carr_library/ui/catalog_tools_dialog.py` | `2cd9743e3e279c138fb4c655b11d3e1e9e52c973` | `src/carr_library/ui/catalog_tools_dialog.py` | Use reviewed export component 5a11469a: queued library/source checks and truthful receipt/cleanup notice; retain reader, keyboard and lifetime extensions. |
| `carr-library/src/carr_library/ui/desktop_app.py` | `c264eb3cf2f47d0ade514234cbf3eba13512d9bd` | `src/carr_library/ui/desktop_app.py` | Use reviewed export component 5a11469a: queued library/source checks and truthful receipt/cleanup notice; retain reader, keyboard and lifetime extensions. |
| `carr-library/src/carr_library/ui/import_center.py` | `bde66d23bebe46a05a79e27418984bc5d612011c` | `src/carr_library/ui/import_center.py` | Use reviewed export component 5a11469a: queued library/source checks and truthful receipt/cleanup notice; retain reader, keyboard and lifetime extensions. |
| `carr-library/src/carr_library/ui/operations_dialog.py` | `7e687b9480dea4da3e232f561b1bfb919dc43223` | `src/carr_library/ui/operations_dialog.py` | Retain richer shared broker/facade and same production/CLI service, exact workspace authorization, durable controls and cleanup-held leases. |
| `carr-library/src/carr_library/ui/reading_desk.py` | `c15a3930c781743a307302b23fcddc99a616bb8e` | `src/carr_library/ui/reading_desk.py` | Use reviewed export component 5a11469a: queued library/source checks and truthful receipt/cleanup notice; retain reader, keyboard and lifetime extensions. |
| `carr-library/src/carr_library/ui/research_actions.py` | `20348348fadaba3a941031d2508c542e46a29fb9` | `src/carr_library/ui/research_actions.py` | Use reviewed export component 5a11469a: queued library/source checks and truthful receipt/cleanup notice; retain reader, keyboard and lifetime extensions. |
| `carr-library/src/carr_library/ui/transcription_dialog.py` | `12f4be1fa61fd89ca208ff00663bf9961753e307` | `src/carr_library/ui/transcription_dialog.py` | Adopt reviewed 6a9b5d96 backend / 65a2c287 native and CLI convergence; retain exact scope, reviewed environment and no automatic fallback. |
| `carr-library/src/carr_library/ui/translation_dialog.py` | `65292429f2caab7b98d0896d31f0adcebb953fc0` | `src/carr_library/ui/translation_dialog.py` | Retain Advanced v3 model/glossary/OAuth and chunk approvals; preserve stronger transport deadlines, exact source binding and passive backup checks. |
| `carr-library/tests/assistant/test_local_assistant.py` | `1a915067511c15a57413018e37a493beafb72827` | `tests/assistant/test_local_assistant.py` | Retain selected local-planner proposal/review, exact model/profile binding, typed execution and owned native lifecycle. |
| `carr-library/tests/assistant/test_local_planning_loopback.py` | `c2eaabc11dedce8a34022456a857271d1ec18a97` | `tests/assistant/test_local_planning_loopback.py` | Retain selected local-planner proposal/review, exact model/profile binding, typed execution and owned native lifecycle. |
| `carr-library/tests/assistant/test_local_planning.py` | `30b1df87da05c412ea645ba06a3f8ef8aa5317a0` | `tests/assistant/test_local_planning.py` | Retain selected local-planner proposal/review, exact model/profile binding, typed execution and owned native lifecycle. |
| `carr-library/tests/assistant/test_planning_handlers.py` | `4106a29238039fa9faf5d24192dfedf11eb17e3d` | `tests/assistant/test_planning_handlers.py` | Retain selected local-planner proposal/review, exact model/profile binding, typed execution and owned native lifecycle. |
| `carr-library/tests/assistant/test_translation_v3.py` | `b994aafda65f3497878302233e4a9f559aac8622` | `tests/assistant/test_translation_v3.py` | Retain Advanced v3 model/glossary/OAuth and chunk approvals; preserve stronger transport deadlines, exact source binding and passive backup checks. |
| `carr-library/tests/audio_acceptance/test_primary_publication_budget.py` | `405552e3ef7f5494678a7a583b42cf0eb84cf9a4` | `tests/audio_acceptance/test_primary_publication_budget.py` | Adopt canonical regression/legacy compatibility changes while retaining current publication ownership. |
| `carr-library/tests/e2e/test_export_destination.py` | `de16c8bd85889e60cc25bf680b8927210eb560d0` | `tests/e2e/test_export_destination.py` | Use reviewed export component 5a11469a: queued library/source checks and truthful receipt/cleanup notice; retain reader, keyboard and lifetime extensions. |
| `carr-library/tests/e2e/test_import_worker_publication.py` | `8a68c6572e06ed77bc463583388e1c6104bfedfa` | `tests/e2e/test_import_worker_publication.py` | Union actual child outcomes and delayed startup with existing deadline and competing publication barriers. |
| `carr-library/tests/e2e/test_operations_cli.py` | `f5477d021d9b644506002e1219e27b2dc6f82a00` | `tests/e2e/test_operations_cli.py` | Retain richer shared broker/facade and same production/CLI service, exact workspace authorization, durable controls and cleanup-held leases. |
| `carr-library/tests/e2e/test_transcription_cli.py` | `a986c2d36773ac9b031d915268d59f9798a4c59b` | `tests/e2e/test_transcription_cli.py` | Adopt reviewed 6a9b5d96 backend / 65a2c287 native and CLI convergence; retain exact scope, reviewed environment and no automatic fallback. |
| `carr-library/tests/ingest/test_import_replay_identity.py` | `724445325865adc020416552b877d895e95a7107` | `tests/ingest/test_import_replay_identity.py` | Adopt canonical regression/legacy compatibility changes while retaining current publication ownership. |
| `carr-library/tests/ingest/test_worker_publication_boundary.py` | `7fd9a9486696f2995d96c82b6b546ffb68f65e93` | `tests/ingest/test_worker_publication_boundary.py` | Port bounded diagnostics/transaction receipt/legacy watermark cases to actual _recover_publication(request, attempt, digest), preserving competing-commit checks. |
| `carr-library/tests/operations/test_facade.py` | `c41babf1fc93cfe5d60c7fcb7a56efd8009aee35` | `tests/operations/test_facade.py` | Retain richer shared broker/facade and same production/CLI service, exact workspace authorization, durable controls and cleanup-held leases. |
| `carr-library/tests/operations/test_production_admission.py` | `ad7403a7e65033c8777a8804988d474a8776c948` | `tests/operations/test_production_admission.py` | Retain richer shared broker/facade and same production/CLI service, exact workspace authorization, durable controls and cleanup-held leases. |
| `carr-library/tests/operations/test_resources.py` | `4e6aa26cff15cc91fa4aae53e17070f741a3572f` | `tests/operations/test_resources.py` | Retain richer shared broker/facade and same production/CLI service, exact workspace authorization, durable controls and cleanup-held leases. |
| `carr-library/tests/operations/test_worker_start_cleanup.py` | `d9b053b955b63c3852fca07ae180702aabfd30d2` | `tests/operations/test_worker_start_cleanup.py` | Port diagnostic-thread allocation inside owned process cleanup, retaining existing Job/process guards; actual allocation/start failure cases passed ad679. |
| `carr-library/tests/reader/test_export_publication_boundary.py` | `db2dceee443248f8374766ea730057fc9a1eff61` | `tests/reader/test_export_publication_boundary.py` | Adopt canonical regression/legacy compatibility changes while retaining current publication ownership. |
| `carr-library/tests/reader/test_export_reconciliation.py` | `395d1bc709990c324e6bdfdc4e1711ecd10ae1d6` | `tests/reader/test_export_reconciliation.py` | Retain bounded complete-content, no-clobber, cancellation and Windows ChangeTime checks; port receipts and authorization-before-read regression. |
| `carr-library/tests/storage/test_atomic_state_publication.py` | `79ad42095697deb943b9e0a6f7372a5fff3f036d` | `durable_state.py`; `windows_state.py`; `tests/storage/test_state_replacement.py`; `test_durable_state_windows_read.py` | Retain owned Windows handle replacement and real sharing/denial barriers; do not replace with the alternative os.replace retry strategy. |
| `carr-library/tests/storage/test_backup_translation.py` | `ed2ca50c88f66182c3d3cfaa98735888405800c2` | `tests/storage/test_backup_translation.py` | Retain Advanced v3 model/glossary/OAuth and chunk approvals; preserve stronger transport deadlines, exact source binding and passive backup checks. |
| `carr-library/tests/storage/test_transcription_backup.py` | `761ad7a1b363e62663b74ee201fc60b4aab88f17` | `tests/transcription/test_supplied_worker_bridge.py`; `test_v5_worker_profile.py`; `test_runtime_review.py`; `test_profile_controls.py`; `tests/storage/test_backup_transcription.py`; `test_transcription_review_restore.py` | Reconcile scenarios against actual retained-originals worker protocols, runtime review, durable controls, rejection and typed restored authority. |
| `carr-library/tests/storage/test_v3_state_backup.py` | `01d1d3d70345e5719e3c64aed6b01ed103d78d4e` | `tests/storage/test_v3_state_backup.py` | Retain common v4 with explicit legacy adapters, complete typed studio/reader/ASR state, immutable originals and restore trust revocation; no second backup model. |
| `carr-library/tests/transcription/conftest.py` | `4bfaa8631937c0e36f625efe4f569a925175809a` | `tests/transcription/test_supplied_worker_bridge.py`; `test_v5_worker_profile.py`; `test_runtime_review.py`; `test_profile_controls.py`; `tests/storage/test_backup_transcription.py`; `test_transcription_review_restore.py` | Reconcile scenarios against actual retained-originals worker protocols, runtime review, durable controls, rejection and typed restored authority. |
| `carr-library/tests/transcription/fixture_runtime.py` | `df13b49732e8f0fe60660bc21fb32b9c5a2f7374` | `tests/transcription/test_supplied_worker_bridge.py`; `test_v5_worker_profile.py`; `test_runtime_review.py`; `test_profile_controls.py`; `tests/storage/test_backup_transcription.py`; `test_transcription_review_restore.py` | Reconcile scenarios against actual retained-originals worker protocols, runtime review, durable controls, rejection and typed restored authority. |
| `carr-library/tests/transcription/fixtures/protocol_worker.py` | `f28e80b33bd4856063e306883fd05f7e85cb537a` | `tests/transcription/test_supplied_worker_bridge.py`; `test_v5_worker_profile.py`; `test_runtime_review.py`; `test_profile_controls.py`; `tests/storage/test_backup_transcription.py`; `test_transcription_review_restore.py` | Reconcile scenarios against actual retained-originals worker protocols, runtime review, durable controls, rejection and typed restored authority. |
| `carr-library/tests/transcription/test_admission_boundaries.py` | `94acab74ef2613daabd2fff06f3bab680caaeefa` | `tests/transcription/test_supplied_worker_bridge.py`; `test_v5_worker_profile.py`; `test_runtime_review.py`; `test_profile_controls.py`; `tests/storage/test_backup_transcription.py`; `test_transcription_review_restore.py` | Reconcile scenarios against actual retained-originals worker protocols, runtime review, durable controls, rejection and typed restored authority. |
| `carr-library/tests/transcription/test_protocol_fixture.py` | `a49073f894c97d8a2c78c4672752f68d420e122e` | `tests/transcription/test_supplied_worker_bridge.py`; `test_v5_worker_profile.py`; `test_runtime_review.py`; `test_profile_controls.py`; `tests/storage/test_backup_transcription.py`; `test_transcription_review_restore.py` | Reconcile scenarios against actual retained-originals worker protocols, runtime review, durable controls, rejection and typed restored authority. |
| `carr-library/tests/transcription/test_restore.py` | `251553b767cca2435f6f4a4424d5cf5268cc026e` | `tests/transcription/test_supplied_worker_bridge.py`; `test_v5_worker_profile.py`; `test_runtime_review.py`; `test_profile_controls.py`; `tests/storage/test_backup_transcription.py`; `test_transcription_review_restore.py` | Reconcile scenarios against actual retained-originals worker protocols, runtime review, durable controls, rejection and typed restored authority. |
| `carr-library/tests/transcription/test_runtime_admission.py` | `591e946db0bc8301a30e5114ab2cba9159979e96` | `tests/transcription/test_supplied_worker_bridge.py`; `test_v5_worker_profile.py`; `test_runtime_review.py`; `test_profile_controls.py`; `tests/storage/test_backup_transcription.py`; `test_transcription_review_restore.py` | Reconcile scenarios against actual retained-originals worker protocols, runtime review, durable controls, rejection and typed restored authority. |
| `carr-library/tests/transcription/test_service.py` | `ef7a2f3167dac743cd762a4925b6c3f8c2444771` | `tests/transcription/test_supplied_worker_bridge.py`; `test_v5_worker_profile.py`; `test_runtime_review.py`; `test_profile_controls.py`; `tests/storage/test_backup_transcription.py`; `test_transcription_review_restore.py` | Reconcile scenarios against actual retained-originals worker protocols, runtime review, durable controls, rejection and typed restored authority. |
| `carr-library/tests/ui/native_catalog_tools_smoke.py` | `0ce4eedaacd358ea3ebee21cd66f282db0e0043a` | `tests/ui/native_catalog_tools_smoke.py` | Use reviewed export component 5a11469a: queued library/source checks and truthful receipt/cleanup notice; retain reader, keyboard and lifetime extensions. |
| `carr-library/tests/ui/probe_catalog_insights_actions.py` | `0c655c919e019f914fffbeb49fc2ed05e6889073` | `tests/ui/probe_catalog_insights_actions.py` | Use reviewed export component 5a11469a: queued library/source checks and truthful receipt/cleanup notice; retain reader, keyboard and lifetime extensions. |
| `carr-library/tests/ui/test_academic_actions.py` | `fed9225d2689b116735a4adf1e57d0b8ccea6522` | `tests/ui/test_academic_actions.py` | Use reviewed export component 5a11469a: queued library/source checks and truthful receipt/cleanup notice; retain reader, keyboard and lifetime extensions. |
| `carr-library/tests/ui/test_academic_export_handler.py` | `63320ade10dbfbf0553663193172f03dc25298a7` | `tests/ui/test_academic_export_handler.py` | Use reviewed export component 5a11469a: queued library/source checks and truthful receipt/cleanup notice; retain reader, keyboard and lifetime extensions. |
| `carr-library/tests/ui/test_academic_library_dialog.py` | `56e179cbda725e91a8f4d64a717db3b73b1f50df` | `tests/ui/test_academic_library_dialog.py` | Use reviewed export component 5a11469a: queued library/source checks and truthful receipt/cleanup notice; retain reader, keyboard and lifetime extensions. |
| `carr-library/tests/ui/test_catalog_tools_dialog.py` | `2af5b6f65af7e23cacbe209e17250a1f7c5bb5de` | `tests/ui/test_catalog_tools_dialog.py` | Use reviewed export component 5a11469a: queued library/source checks and truthful receipt/cleanup notice; retain reader, keyboard and lifetime extensions. |
| `carr-library/tests/ui/test_desktop_handlers.py` | `7fe0eb959f496fcd6365b53d60e7ae7e72f3df62` | `tests/ui/test_desktop_handlers.py` | Use reviewed export component 5a11469a: queued library/source checks and truthful receipt/cleanup notice; retain reader, keyboard and lifetime extensions. |
| `carr-library/tests/ui/test_operations_policy.py` | `f033b76f132c457f6ff1533fbff96a1a84e699c5` | `tests/ui/test_operations_policy.py` | Retain richer shared broker/facade and same production/CLI service, exact workspace authorization, durable controls and cleanup-held leases. |
| `carr-library/tests/ui/test_platform_modal_lifecycle.py` | `02f28a69643399c92e2f1200b6a94d3f54b95422` | `scripts/windows_transcription_smoke.py`; `windows_operations_smoke.py`; `windows_local_planning_smoke.py`; `windows_translation_v3_smoke.py` | Use dedicated native probes for the selected canonical APIs and their cleanup tests; omit the package-shaped combined driver. |
| `carr-library/tests/ui/test_reading_desk_actions.py` | `ceaa2ec6a6f3d8f1300e05c43a46ce712ffa159f` | `tests/ui/test_reading_desk_actions.py` | Use reviewed export component 5a11469a: queued library/source checks and truthful receipt/cleanup notice; retain reader, keyboard and lifetime extensions. |
| `carr-library/tests/ui/test_studio_keyboard_registration.py` | `49a5178e9627b1766eebc7485a3855f87bb443a3` | `tests/ui/test_studio_keyboard_registration.py` | Retain current regression/native command implementation with additional ownership and scope cases; canonical behavior preserved. |
| `carr-library/tests/ui/test_transcription_dialog.py` | `b13e4c59a67effc05df1e8332b45f3aff505f157` | `tests/ui/test_transcription_dialog.py` | Adopt reviewed 6a9b5d96 backend / 65a2c287 native and CLI convergence; retain exact scope, reviewed environment and no automatic fallback. |

## Verification and remaining qualifications

The exact `ad679edd` reader-completion job passed 396 cases in 22 isolated suites, with zero failures and zero skips. It includes actual FFmpeg listening/measurement, actual incremental backup/restore and native-handler lifetime fixtures. See [the complete source-job record](acceptance/20261127-ad679-reader.json) and [Actions run 45995072764](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45995072764). Later changed paths still require their exact-head run.

The authoring executor became unavailable during this session. Recovery and further source work were preserved through immutable Git objects and expected-head branch updates; no unexecuted Python, native test or model inference is labelled executed. Runtime evidence comes from returned completed CI jobs.

The updated specification also identified finite additions beyond main convergence: playback queue/live levels, audio conversion batches, translation document batches, larger bounded intake and typed assistant tool breadth. Their source components and exact validation are tracked separately in candidate readiness. Private model/voice inputs, actual device/NVDA review, signing authority and approved redistribution remain separate qualification requirements.

## Canonical PR191 follow-up

Canonical main advanced from `e3cf74c243d4ef930b526b5648ca9003c0900057` to
`a333f0723df107aa60b60b8ee2c5ba9f85b25475` through PR191 and source commit
`c3db8a858430506d97c30ebe94f512e1863982a9`. Only the following two paths changed.
The reviewed reconciliation is `230dfe09c3f4451d6f5e6355d506d46386101b5a`.

| Canonical path | Canonical blob | Composed behavior |
| --- | --- | --- |
| `src/carr_library/research/export_file.py` | `c4608af444fbdb7a1b5c6feb32be82e8b19f3af5` | Add expected target/parent/basic-file checks before native metadata or byte reads, then compare native ChangeTime before hashing. Keep the candidate's existing `_inspect_destination`/`_recheck_scope`, cancellation, bounded fingerprints, no-clobber publication and cleanup receipt. |
| `tests/reader/test_export_reconciliation.py` | `d29682c151a0566d298116b8b422e412f8abd55f` | Retain all 16 existing candidate cases and the canonical four actual replacement cases; additionally observe native metadata calls. Both pre-write and post-staging replacement must be rejected without opening unrelated content. |

The canonical main commit is retained as integration ancestry. Source review is
complete; all20 composed regression cases passed on exact391beff3. Earlier
export PASS records alone did not qualify this changed file.

## Final reviewed reading-content and fixture composition

The current source includes the complete12-path Read/Skip chain through
`11f374a489cf0df1860225ed4b372a1aa815dbd6` (core822, UI78e, fixture4f5 and
ownership/deferred-play correction11f). Both independent reviews approved
source identity, owner-only adoption, native Destroy cancellation, saved-WAV
receipt truthfulness, filtered timing and existing passive recovery. Runtime
acceptance is tracked against the composed candidate, not inferred from review.

The two test-only successors are `ae1cb7eb2dc599979253730f5f406bd2957cb1eb`
(nonempty, explicitly reviewed raster recovery plus blank refusal) and
`1450ac101f1f63bde952ba940ac26759e9efe3ff` (observe the actual bibliography
receipt export entrypoint). The former passed50 installed cases on exact7a;
the latter requires its actual Windows probe. Existing strict gates retain all
old suites and add the two narration suites plus relevant reading/source/session
preservation. A focused exact-head Windows job exercises the extended existing
reader keyboard/restart probe; no substitute GUI implementation is introduced.

## Concurrent reviewed source retained after e8d

The shared branch advanced without replacing e8d:3e4ce53a adds explicit cache
isolation and non-executable local-model clarifications;fb2d8976 centralizes strict
JSON decoding for typed plans, profiles and existing jobs/controls;5604f19d reuses
the production MiniLM retrieval evaluator, bounded synthetic UK/EN/SK fixture and
existing installed-firewall gate. Their exact five/four/four-path commits were
independently reviewed. No competing store, plan executor, cache owner or search
implementation was introduced. Full-tree comparison confirms unrelated candidate
files were preserved. The evaluator reports NOT_DECIDED quality and does not
establish independent held-out gold or broad language qualification.

The new core/provider/cache/schema tests are included in current strict source
gates. The actual corrected reading component passes227 cases on3e4. Earlier
e09 installed native evidence confirms20 template and40 bibliography-interchange
checks;fb2 bibliography passes1154 Windows source cases and69 installed native
checks. Their exact records do not substitute for the current candidate gates.

Reviewed follow-ups03ab and1704 change only observed acceptance fixtures: a real
isolated standard-library ASR peer avoids repeatedly inspecting unrelated installed
packages, and the native bookmark driver verifies returned editor focus once before
focusing the next command. All original assertions, runtime inspections and gate
budgets remain. New execution is still required for these exact corrections.

## Canonical PR196 optional transcription-cache reconciliation

Canonical main advances froma333f072 to
`a44fac04072389caa40e0d6e799b13797712f797` through PR196. The exact
four-path decision is the reviewed flat-adapter component
`30479967d36ca3b4440c19470d7e32fe6207c87e`. The main commit is retained
as integration ancestry; no old package-shaped transcription service replaces
the current workspace-bound service.

| Canonical changed path | Main blob | Current reconciliation |
| --- | --- | --- |
| `docs/PRIMARY_STATE_BACKUP.md` | `5f45fcaeae604dbcce8f9977e26babdfee71862b` | Adapt descriptions to actual current source/media limits, passive configuration and explicit unsupported recognition-cache inclusion. Preserve the established incremental-backup contract separately. |
| `src/carr_library/backup_sidecars.py` | `e77dca0b919687d9a8e10c1ba86c817ec01662ca` | Reject cache entries contradicting false/absent inclusion before payload reads. Default capture omits only the exact owned cache directories after native directory safety checks. Explicit inclusion cannot admit an untyped ASR cache. |
| `src/carr_library/backup_transcription.py` | `5a82ac28c21dd11eed47f1d809c522a74a611d74` | Add narrow classifiers for current flat job `logs/cache` and `logs/cache_v5`. Existing accepted source, receipts, V5 result/manifests, per-kind limits, collection verification and trust revocation remain unchanged. |
| `tests/storage/test_transcription_backup.py` | `730c2b0c4f784957fd41c579f69708a1ba936fcc` | Retain the earlier intentional deletion of this incompatible package-shaped suite. Port its128 inclusion/layout/manifest cases into current `test_backup_transcription.py`, plus actual default-cache omission/restore, forged manifest no-read and wrong-kind native directory cases. Keep all15 original test bodies. |

The final test file includes the independently reviewed03ab isolated-peer
fixture once. Expected suite cardinality is161, including real filesystem and
synthetic V5 worker cases; this number is not execution evidence. A true
`include_generated` flag still refuses optional ASR cache payloads because
this flat producer has no typed cache admission schema. Default backup retains
required inputs and accepted outputs; another producer's worker-history cache
layout is not silently treated as compatible. New execution is pending on the
composed candidate.

The test-only successor `617965a1796f9794af483b758a7858cb435eb359`
retains all161 scenarios and guards both Path.open and the actual os.open sidecar
reader during the new cache-omission/refusal checks. Those guards end before
legitimate source-directory cleanup. Independent production and test reviews
approve the final source; no new runtime result is inferred from that review.

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

## Reviewed completion updates after 175bbb7

The next composition starts from `cdcde47065bb3909bd93ca2a26985f5c0305449b`.
Its isolated Linux loopback acceptance correction is retained exactly; ordinary
source checks still deny network access. Canonical main
`e5c64a5245583f8f8b404a0f042f6ca438c24d7f` merges PR183 and changes only
the two Save All paths, whose blobs exactly match the reviewed component below.
Main ancestry is retained. No other canonical source is substituted.

These are implementation and source-review decisions. Fresh execution of the
assembled successor is required; historical passes in the table below remain
bound to their recorded commits.

| Component | Final reviewed component | Integration decision |
| --- | --- | --- |
| Portable Office archive intake | `10e73e54979a37d3e42f467e0c6b0cb64415579e` | Share ordinary Windows filename checks between retained-byte and streaming archive admission; keep size, cancellation and source-identity boundaries. Both new actual ZIP/reader suites are selected. |
| Durable production replay | `5be418c3247c9914cf4c89b21dfecdf833710d42` | Bound output names to full job identity; retain existing destinations, reject unowned orphan publications and verify every expected artifact before replay. Bind optional operation digest to the submitted revision and options. Restore explicitly rebinds pending inputs/output authority. |
| Academic acquisition publication | `07262f28b394906b283aeb119cb70c35e83653dc` | Recheck exact duplicates and save their reference membership in one transaction. Retain durable new imports if later reference work needs repair. Preserve existing streaming import, DOI/ISBN equivalence and cancellation. |
| Native unheld lock race | `25e30b1aff0368edcb42edc2820e46c4f2032bd9` | Tolerate only the precise disappearing or zero-link regular leaf before ownership; strict parents, held descriptors, native identity and hardlink refusal remain. |
| Save All identity | `d0bd5ee2f14bcfd0e36df45532911276815557a0` | Attribute replies to the original registration, preserve confirmed durable writes after late Stop and focus the first still-live failed draft. Reuse the exact canonical PR183 blobs. |
| Saved Studio output options | `39e07bc72f250669de41aae4d197a20566246ac5` | Restore saved format/stems; changed options explicitly create a new job from the saved revision, preserving newer drafts and old receipts. Keep real restored-input approval before cue reads. All24 prepared controller/ProjectStore cases are selected; this is not acoustic-render evidence. |
| Pending and manifest cancellation | `d26017319cb75e2361b39db6d71d7c14cc550d9d` | Bound total directory traversal, reject unexpected entries as encountered and propagate cancellation before staging/publication. Preserve streaming source/hash/restore limits. Prefix legacy state paths before their kind-specific depth check. Retain all161 earlier transcription-backup cases and add24 controls. |
| Recorded Python runtime | `16a0b5d7366f1be14459e0ba18872f797361a0ba` | Seal raw METADATA/RECORD, additional distributions and executable bytecode without importing selected packages. Preserve the seven-package worker digest dialect. Extended outer inventories participate in profile identity and permanent stale-result rejection; historical seals remain passive restore data. |
| Generic Python voice helper | `80b04a312ba28bac4a80991d1e57cbf1c969074d` | Add a pinned script/hash pair with fixed isolated invocation. The child compiles one bounded hash-verified byte snapshot, closing the host-verification/path-reopen race. Preserve bare executable helpers, owned cancellation and passive backup. All39 prepared helper cases are selected. |
| Local translation workflow | `f5cf3fbc20e1d77aef48067e877001959417d862` | Add the pinned LibreTranslate wire mapping to the existing profile/Gateway/service/batch/UI/CLI and typed backup. Require explicit loopback profile, zero external cost, separate selected-data approval and directed-language review. Preserve remote approvals, current source/profile rechecks, decoded receipt/ledger binding and no automatic uncertain retry. |
| Exact loopback acceptance | `8c9b75b325dad41f94b7bdd46f38f8a94cc9ca99` | Extend only the closed selection to the exact local-planning and local-translation suites, in order and with one worker. Keep Linux non-root loopback-only namespace, ordinary offline socket denial, JUnit/skip policy, deadlines and process cleanup byte-identical. |
| Native cursor precondition | `08345a93158d419387468476acccdf14f6bc181f` | After the new Ctrl+A policy flow, use real End to re-establish the intended collapsed cursor before Save/Close. Check live and durable cursor, retaining the original restart assertion. The observed175 native failure remains recorded until a fresh corrected probe passes. |

The two independent additions to `test_v3_state_backup.py` retain the exact
original test prefix and both append blocks. Helper sidecar validation is applied
after the pending-backup component, preserving its cancellation and traversal
changes. New source and installed selectors are additive; native origin checks,
strict JUnit failure/skip policy, network isolation, process cleanup and existing
time budgets are unchanged. In particular, the V3 installed-recovery step still
has its ten-minute deadline.

### Exact earlier execution newly recorded

| Tested commit and gate | Actual outcome | Record |
| --- | --- | --- |
| 175bbb7 quick source | 2114 passed, 22 failed, zero skipped; all failures were the real loopback suite selected into the offline runner. The cdc namespace correction requires its own execution. | [Quick source](acceptance/20261127-175bbb-expanded-reader.json) |
| 175bbb7 reading source | 239 passed, zero failed/skipped,12 strict suites. Native keyboard/Read-Skip result is separate. | [Reading source](acceptance/20261127-175bbb-reading-content.json) |
| 175bbb7 OCR | 133 passed, zero failed/skipped,8 strict suites. | [OCR](acceptance/20261127-175bbb-automatic-ocr.json) |
| 175bbb7 workflow templates source | 182 strict cases plus12 identity/parser cases passed, zero failed/skipped. | [Template source](acceptance/20261127-175bbb-templates-source.json) |
| 175bbb7 bibliography | 1154 Windows source cases and69 installed native checks passed. Controlled file choices are not native-picker or human NVDA evidence. | [Bibliography](acceptance/20261127-175bbb-bibliography.json) |
| 175bbb7 academic reconciliation | 2894 passed,64 failed, zero skipped; only the legacy transcription-backup depth matrix failed. The reviewed d260 prefix correction requires new execution. | [Reconciliation](acceptance/20261127-175bbb-reconciled.json) |
| 5604f1 portable lifetime | 262 filesystem source cases,26 native source cases and26 installed native cases passed. Nine real executable-launcher checks use a synthetic child, not an actual Python installation. | [Portable lifetime](acceptance/20261127-5604f1-portable-lifetime.json) |

Private runtime/model quality, human Windows11/NVDA acceptance, per-feature
qualification for all135 languages, real large-corpus qualification and signed
distribution are not established by these records. Explicitly unsupported legacy
input formats and optional ASR-cache inclusion remain product limitations; this
composition does not relabel them as passing qualifications.

### Local workflow acceptance selection

The local backend and UI tests are included in the strict quick, V3 source and
Windows source/installed selections. Actual HTTP/CLI cases run in the dedicated
loopback namespace and the existing explicit transport/installed protocol gates.
The new native local-translation probe accompanies the existing Advanced v3 probe;
its real wx focus/keyboard/worker and derivative checks use synthetic REST
responses and do not claim a live LibreTranslate model, linguistic quality or
whole-machine offline egress. Existing Google v2/v3, batch, source, deadline,
resolution and typed-backup selections remain intact.

The complete175 production job additionally passed2064 strict source cases in109
suites and865 explicit provider/trust cases (one Windows-only test deselected,
still retained in Windows gates), then completed the official EPUB and local DAISY
schema controls. See [the exact production record](acceptance/20261127-175bbb-production.json).
The installed175 reader reached the post-restart cursor assertion and failed;
see [the failed native record](acceptance/20261127-175bbb-native-reading.json).
Neither result is silently reassigned to the assembled successor.

Installed fixture loading is also reconciled in the test-only four-file component
`af325639547eefc3ee115d6f2c2dd8d0db1dbd9f`. Its acyclic source/v3/batch/CLI/UI
fixture imports load exact sibling files using private module names and verify
cached file identity. All test and fixture bodies are retained byte-for-byte;
no test directory, checkout root or production source is added to Python's import
path. The new local UI fixture uses the same loader. Installed collection and
execution still require the assembled Windows gates.

The immediate parent `cdcde470` subsequently completed its corrected quick gate:
2134 ordinary offline source cases plus25 actual isolated-loopback source cases,
all passing with zero failures/skips. Its bibliography gate separately passed1154
Windows source and69 installed native checks. See the [parent quick record](acceptance/20261127-cdcde4-expanded-reader.json)
and [parent bibliography record](acceptance/20261127-cdcde4-bibliography.json).
These records confirm the existing namespace infrastructure and parent component;
they do not qualify the newly added translation suite or the assembled successor.


## Shared producer callers and actual b418 execution

The next composition closes concrete failures observed on exact commit
`b418baf6cd5c4a00cdd9c7337efba053bd3b870b`, tree
`fdec0918f0bb64127435fe8002be8a34f8bc1ae3`. It contains two product call-site
corrections and seven test-file corrections/extensions. No workflow selection,
network boundary, source/installed-origin check or time budget changes.

| Reviewed component | Resulting behavior |
| --- | --- |
| Publication ownership — `10804eb1ac92fe9704ae840bf7997435371aafee` | Publication uses the same exact owner receipt as Studio rendering. New receipts retain their operation seal; valid legacy receipts remain legacy. Missing/changed/extra ownership fields, changed options and a late owner change still refuse publication before acceptance. The shared source check covers EPUB, DAISY and measured PCM. All prior publication assertions remain, with nine additional real-rendered-source cases. |
| Book preview — `11ced77a28d3ef6862da2af308eaafa458743077` | Fresh preview uses the exact full-job-ID destination helper used by submission. Existing and legacy job destinations remain unchanged, and restored jobs still require explicit relocation. Preview/prepare equality and UI approval membership stay enforced. Backup fixtures first prove rejection of a stale operation seal, then construct individually valid changed jobs to exercise the separate immutable Book-plan relation. |
| Isolated ASR protocol fixtures — `e7fa3c2fa0cbd7ef28cd5b27bcbf702738a0f148` | The synthetic standard-library workers use real no-pip, no-system-site virtual environments and retain the selected lexical interpreter through reconfiguration. All227 original assertion lines remain. The explicit legacy resolved-host-interpreter control remains. The 600-second suite and12-minute ASR job budgets are unchanged. Application CLI execution still uses the installed application interpreter. |
| Studio test ownership and representation — `0f5a95be8d23e7f7b0e86edfb1e102f9e8de69a4` | The busy-controls test executes the actual direct closure in `show_studio`, avoiding another dialog's same-named function. Restored projects are compared as complete JSON receipts, preserving all fields, approval-before-read, original receipt bytes, pending state and worker assertions. |

Each component has a full-content readback and an independent source review.
Corrected execution is still required; the earlier results below are not assigned
to the successor merely because its source review passed.

### Exact b418 results

| Gate | Actual result | Evidence |
| --- | --- | --- |
| Reading | 244 strict source cases and55 installed native checks passed. Both native policy/restart rounds completed, including the formerly failing cursor assertion. | [Reading](acceptance/20261127-b418ba-reading-content.json) |
| Bibliography/import | 1252 strict Windows source cases and69 installed native checks passed; exact wheel and uploaded evidence hashes are retained. | [Bibliography](acceptance/20261127-b418ba-bibliography.json) |
| Automatic OCR | 133 source cases passed with zero failures/skips. | [OCR](acceptance/20261127-b418ba-automatic-ocr.json) |
| Workflow templates source | 182 strict cases and12 identity/parser checks passed; installed native result is separate. | [Templates](acceptance/20261127-b418ba-templates-source.json) |
| Compiled native launcher | The same nine native checks passed for pinned Zig and MSVC, using a synthetic child. Actual Python installation, signing and human NVDA remain unqualified. | [Launchers](acceptance/20261127-b418ba-native-launchers.json) |
| Quick composition | 2315 passing and40 failing cases in101 completed reports; one additional ASR bridge suite exceeded600seconds. The new local translation component passed100 protocol,9 UI and12 real isolated-loopback/CLI cases. | [Quick diagnostic](acceptance/20261127-b418ba-expanded-reader-diagnostic.json) |
| Academic production | 1977 passing and103 failing cases in109 suites. Publication/Book caller drift and three ASR child-start waits are recorded explicitly. | [Production diagnostic](acceptance/20261127-b418ba-production-diagnostic.json) |
| Reconciled source | 3201 passing and6 failing cases in95 suites. All185 transcription-backup cases passed in7.49seconds; the six failures belong to the repaired Book/Studio fixture preconditions. | [Reconciliation diagnostic](acceptance/20261127-b418ba-reconciled-diagnostic.json) |

The preceding cdc Windows ASR run exceeded600seconds in both supplied-worker
suites and then reached its12-minute job cancellation. Three completed suites
passed32 cases; backup and later gates had not run. See the [exact incomplete
record](acceptance/20261127-cdcde4-asr-windows-timeout.json). It provides no Windows
backup timing or corrected-worker acceptance claim.

The new local translation results exercise synthetic protocol responses and the
existing approval, source/profile, receipt, batch and export boundaries. They do
not establish live-server/model quality, native translation accessibility, or
human linguistic acceptance. All previously stated private-runtime, corpus,
Windows11/NVDA, language and distribution qualification limits remain.


## Exact09c production follow-through and full Book naming review

The actual109-suite production job on `09c312c4106d9abd8248bdd5fa23b9056326998b`
(tree `cb26477a18c642b6ca4d6687a348a0c9fe185ee6`) completed2091 passing cases,
one failure and zero skips. The39 publication cases,37 Book core,17 Book batch,
44 DAISY,27 measured-audio and11 unchanged actual-child transcription-admission
cases passed. See the [exact production diagnostic](acceptance/20261127-09c312-production-diagnostic.json).
The downstream official publication validators were not reached after the source
failure; their earlier results are not assigned to this run.

The remaining existing Book UI case expected a full sanitized name inside a
directory label now deliberately limited to18 characters plus the full operation
ID digest. Reviewed component `ac5db5470606c66f1a96f25f38c4058e124284c8` makes
the full bounded template and each actual expanded part title explicit beside
the unchanged exact destination. It uses the same pure title helper as the
production plan. Preview does not persist a plan, create files or start audio.
The existing worker/handler test now follows an unnotified field change through
fresh review and explicit Prepare, verifying exact titles and new paths against
that review, absence from the old review and no generated output. The same19-case
suite and all workflow selectors remain in force. This correction has full blob
readback and independent source review; its execution is not inferred from09c.

The finite-source review was also rechecked against the current finalization and
detailed V3 authority. No additional confirmed mandatory public-source adapter
or workflow omission was identified beyond the concrete acceptance corrections.
Unsupported format/cache features stay unsupported, optional features stay
optional, and private/real engine, language, corpus, human accessibility and
signing/distribution qualification remain independent. The [readiness record](V3_RELEASE_READINESS.md)
now links the latest exact evidence rather than presenting older checkpoints as
current acceptance.

The same09c subsequently completed the full quick composition with2420 passing
cases in100 offline and2 isolated-loopback suites, zero failures/skips and no
missing reports. All65 bridge cases completed in77.50seconds within the unchanged
600-second limit. Its separate reading-source job passed244 cases in12 suites.
See the [exact separate checkpoints](acceptance/20261127-09c312-source-checkpoints.json).
These source passes resolve the earlier fixture timeout and Studio quick failures;
they do not replace the separate one-failure production result, installed/native
checks or execution of the later full-name presentation correction.


## Current-head CI scheduling

The finite source audit and concrete Book corrections are complete for testing.
The remaining installed/native gates must run on the current assembled head.
Four existing workflows had a special exception that disabled cancellation only
for the integration branch: Windows desktop, V3, supplied transcription and
portable lifetime. At the recorded18:16UTC observation, older Windows/lifetime
jobs were still queued and the old cdc V3 job was running, while the current0b2
runs remained pending in the same workflow/branch groups.

These four workflows now use the same `cancel-in-progress: true` policy they
already applied to other branches. Their exact concurrency groups, jobs, runner
images, steps, source/installed selectors, time budgets, required tests, origins,
artifact handling and repository permissions are unchanged. All application,
script and test files are byte-identical to0b2. This is validation scheduling;
there is no deployment or release transaction in these workflows.

The [official GitHub concurrency contract](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency)
allows a newer run to cancel an older run in the same group. It does not guarantee
immediate runner allocation. Completed evidence already recorded retains its
exact SHA and outcome. Interrupted old jobs remain cancelled/incomplete; they do
not become passing acceptance. The final head still requires the full unchanged
source, installed, native and recovery gates. Independent review confirmed that
this correction changes orchestration only and does not alter the fifteen worker
automations, their cadence or enabled state, or repository protections.


## Exact 8b6 execution and typed Windows SBOM correction

The exact `8b6a9ac5dc1cb1fe53f433f6326cb4702b759fee` source composition
passed all three large gates: 2420 cases in 102 quick suites, 2092 in 109
production suites and 3207 in 95 reconciled suites, each with zero failures or
skips. These sets overlap. The full Book naming review, publication ownership,
Studio output and backup-plan corrections therefore have actual passing source
evidence. Separate publication validators and the completed installed Windows
reading, template, bibliography, keyboard, lifetime and launcher checks are
bound to their exact jobs in the [8b6 checkpoint](acceptance/20261127-8b6a9a-completed-gates-and-diagnostics.json).

The full integrated Windows job stopped before broad source gates at its runtime
inventory assertion. All dependency installation and pip check succeeded. The
semantic lock has 57 Python libraries; its CycloneDX document correctly has those
57 libraries plus the reviewed py3langid model file. The base profile similarly
has 22 libraries plus that file. Treating every component as a Python dependency
caused the assertion failure; there was no lock version mismatch.

Reviewed component `b314f8118e7e146d393a107edc7669426ed1fd7f` introduces one
stdlib typed-inventory helper used by the bundle builder and both existing
workflow consumers. Library equality remains exact. Only the existing declared
model record is admitted, including its version, hash, size, license and
provenance. Unknown, missing, duplicate or changed components refuse. Installed
model bytes and the selected wheel plus its internal model bytes are bounded
and hashed without importing model code. Copy-time wheel checks and sealing of
the original validated SBOM bytes remain. All 20 former tests and 49 assertion
lines are preserved, with 15 added real-file/profile/negative test methods.
Locks, SBOM source files, the reviewed model manifest, production application,
test selectors and time budgets are unchanged. Root and independent source
review approved exact read-back blobs; execution is required on the successor.

The separate 8b6 Windows ASR gate reached its unchanged 12-minute job deadline
after six complete reports: 331 passed, zero failures and zero skips. This
includes all 195 new runtime RECORD cases. UI and CLI suites were active at
cancellation, eight further selected suites and downstream native checks were
not run. No partial or missing report is promoted to PASS. The reviewed follow-through is recorded below; its runtime cost must be measured
by the next assembled run.


### ASR fixture ownership follow-through

The complete 8b6 Linux ASR gate passed all 721 cases in 16 suites with no
failures or skips. It also measured 217.12 seconds for the UI suite and 204.08
seconds for CLI. Windows completed the same changed runtime RECORD cases but
reached its job deadline. These are suite timings, not per-test attribution.

Component `79712e1e8665a2f4034bc44f6ab40e49b204e61c` selects actual private
no-pip/no-system-site Python environments for the standard-library synthetic
UI, CLI and native peers. The real installed application, CLI entrypoint and
wx host remain unchanged. Every original assertion line (156 UI, 49 CLI and
66 native) and each existing timeout is retained; new assertions require the
selected lexical executable, actual pyvenv configuration and absent core-engine
packages. The native driver still uses real inspection and trust controls for
both protocols. Production runtime review and worker code are unchanged.

Component `8c6c859f7b7f413500f2fa0051bcc6089408188f` changes only the legacy
resolved-interpreter test setup. Its copied executable remains a real resolved
selection, and its small genuine installed distribution has METADATA, hashed
RECORD and a marker module imported by the actual child. Exact path and nonempty
three-file inventory assertions are added. All 148 former assertion lines and
the stripped-record refusal, explicit rereview and successful-child sequence
remain. The application-host CLI launches are unchanged. Both components have
exact full readback and independent source review. They make no performance or
execution claim before the successor runs. The 600-second suite, 12-minute ASR
job and 120-second native probe budgets are unchanged.


### Actual V3 Windows byte-fixture diagnostic before publication

The first 8b6 V3 run completed before the successor branch update. Its 28 earlier
strict suites passed 463 cases with zero failures/skips, covering signed-client
source boundaries, helper/voice controls and citation/DOCX regeneration. Actual
native signed-client and Hunspell checks also passed within their stated scope.
The provider step then completed 287 passes and one failure; installed recovery
and all later stages were not reached. Its exact artifact and log are retained
in the 8b6 checkpoint. The intermediate 2ea assembly was never published.

The sole failure was in the real local-translation CLI fixture: default text
writing changed its LF input to Windows CRLF before the application read it.
The application correctly exported those exact selected bytes, while the test
expected the original LF Python string. The actual CLI reads source bytes and
the ReadingDocument exporter writes retained bytes; production normalization
would have broken the required preservation contract.

Reviewed component `ae678f847158edbdac4242a04d47cc9fa58e8641` writes explicit
UTF-8 bytes and executes the same existing case for both LF and CRLF. All 48
assertion lines in the file and all timing bounds remain, including the two
strict source/export byte comparisons, local approval, exact server call count
and offline export after profile removal. The loopback suite grows from 12 to
13 cases. Full Windows installed acceptance requires both exact `[lf]` and
`[crlf]` testcase IDs. The V3 consumer already normalizes parametrized names,
and the exact two-suite isolated-loopback selector stays unchanged. Root and
independent source review approved the corrected bytes; successor execution
is still required.

Coupled component `4f56e29c5c46d35ae85c529ee29b288f0bd6c9ef` preserves both
the reviewed 2ea assembly and ae678 correction as parents. Its sole workflow
change replaces the old unparametrized installed-test requirement with both
exact newline variants. Reversing that one replacement reproduces the complete
reviewed Windows workflow byte for byte, including both typed SBOM validations.


## Reviewed 64a execution and subsequent exact corrections

The [14c predecessor checkpoint](acceptance/20261127-14c823-completed-gates-and-diagnostics.json)
and [64a checkpoint](acceptance/20261127-64aadb-completed-gates-and-diagnostics.json)
retain their exact source identities, completed gates and explicit failures.
All three broad 64a source selections passed, as did the independent reading,
template, bibliography, OCR, keyboard, portable-lifetime and compiled-launcher
jobs. The V3 installed-recovery selection passed 809 cases and closed all five
previous pure-model import-fixture failures. These overlapping selections are
not a count of unique platform tests and are not transferred to a successor.

### Actual diagnostic evidence

The saved integrated-Windows phase artifacts were recovered by a separate
read-only workflow bound to exact artifact ID, source run/head, byte length
and archive SHA256. The reader executes no application, extracts no archive
and prints only bounded public diagnostic text. Root verified 59 of 60 text
members in each readback. The local-transport.xml log member lost 19 characters
through log transport and is excluded from byte-exact evidence. The three
native failure logs match their independently recorded member hashes.
The diagnostic workflows are excluded from this product composition.

Actual 64a metadata failure is the assertion that child destruction preserves
the live owner. The corrected watchdog now exposes the original exception
and unwinds at its 120-second limit. Source review found that the old native
driver could select an earlier closed metadata dialog while deletion was
pending. The live-modal correction still requires its own native execution;
the trace does not itself identify which wrapper the old selector chose.

The isolated ASR diagnostic on406170b49822b138e3764cae63130c244707ad2d
passed 14 real profile/label regressions and completed the functional native
scenario. Close returned true without a veto. The actual destruction event
ID equalled the frame's captured native ID and the original wrapper was
already dead, but Python object identity differed. That trace establishes a
probe-observer defect; it does not support changing production Close or
claiming that ordinary wx Yield cannot process deferred deletion.

The V3 canonical phase produced 100 of 139 final suite reports before its
12-minute timeout: 2245 passes and three failures. Those failures cover source
bytes changed after the first hash, a test-owned open SQLite observer and
recording bytes changed after verification. The later always-run progress
selection completed 274 passes and six fixture failures. No 600-second
per-suite hang is established by this trace. The revised scheduling retains
all selectors and every timing/failure boundary.

The corrected isolated component `e6e7ee59189330896f13a1414c65e77ad40d3c05`
then completed [run46068567199/job140739732047](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46068567199/job/140739732047)
successfully at 21:19:16 UTC. All 14 profile and 49 dialog cases passed with zero
failures/skips; native ASR reported 15 passing checks and installed incremental
backup reported 31, including four exact installed origins. Both the matching
native destruction event and dead-wrapper assertion passed. Root and two
independent reviewers read the complete log. This component evidence retains
its own source identity and does not qualify the later full assembled tree.

### Reviewed correction components

| Exact source component | Problem | Resulting behavior |
| --- | --- | --- |
| `543d74ed7cb0546e09b0a5a22002f58917a579f4` | ASR folder-relative labels | Carry the exact scanner file labels through the existing validated submit path; retain source copying, hash, cancellation and publication rules. |
| `fe7527ac261e08e8f0f98f7bbdf7f343c0359174 / c9761d1905b3c2f5c26eeaf9f086be63053cb795` | Actual native close observation | Preserve normal Close and its 120-second/5-second limits; require the matching captured native frame ID and an independently dead original wrapper. Keep all 70 original assertions and add five real observer controls. |
| `49ef322e98464f3abd7441ae1cedc4fa2181c674` | Import Center layout | Use a scrollable content body with fixed Cancel/Close actions and actual small-window/focus/overflow checks; retain worker and import behavior. |
| `bef60a58c0565d3595cdd1646b4d861d137e300a` | Native Book review contract | Compare actual prepared canonical destinations while keeping explicit volume 55, selected-parent, destination, focus and no-output-before-Run checks. |
| `17cfe7ed832284ed6c7dbf612022db0c156ed43c` | Current metadata modal | Select exactly one live modal with the exact parent/title and open controller; reject stale, closing, child and ambiguous candidates. |
| `5195d00187960428bf90d8d292f7430e46057c17` | Streaming source integrity and fixture lifetime | Verify a new FileSnapshot with a genuine second bounded read; retain existing file identity and cancellation checks. Close the test's SQLite observer before deleting its original journal. |
| `03b36e65d86031657499ea75aedec6371a919e5b` | Recording segment integrity | Copy each bounded segment into a same-volume private temporary file, verify exact captured size/hash and PCM parameters, then assemble from that same descriptor. Retain cancellation, reserve and range-hash checks. |
| `7f2a07746793c78f20ed06eca844b48de4a5ce85` | Durable progress and reader fixtures | Cancel only after the actual durable import-job binding so the retained source can resume after original deletion; initialize the reader's existing owned-listening collection. |
| `ea6b81294e698d9e54ff3cb8e85c62b8dc7e02f3` | V3 scheduling | Run the same 139 canonical selectors in the same order with three existing isolated workers instead of two. Keep 12-minute phase, 600-second per-suite, 45-minute job limits and strict failure/skip handling. |

Every changed source body has root and independent review. The final source
composition is based on 64a and retains the earlier canonical ancestry;
unchanged files are checked by complete recursive tree comparison. The ASR
observer is transplanted as c9761d19 onto the reviewed fe7527 source parent,
so the isolated diagnostic workflow and its ancestry are not imported.
Readback and execution of the assembled successor are recorded in PR152.
Source approval by itself is never an execution result.

The current qualification limits remain explicit: synthetic ASR/translation
protocols do not qualify private acoustic or language models; language
inventory is not per-feature language quality; bounded fixtures are not
large-corpus capacity; hosted Windows native controls are not human
Windows11/NVDA or device/listening acceptance; checksums are not publisher
signing or distribution approval.

## Reviewed 9f execution and native-lifetime completion correction

The exact [9f checkpoint](acceptance/20261127-9f2a83-completed-gates-and-diagnostics.json) binds the completed gates to commit
`9f2a838aa4914451bd7a3643361ff66c19268674` and tree
`6fcfb60cdc6dd3ab01055da1600fda45661befae`. Fifteen jobs completed successfully.
Quick/production/reconciled selections passed 2432/2113/3218 cases in
102/109/95 suites respectively; those overlapping selections are not summed.
The actual ASR Windows source/native checks, Import Center and all 137 metadata
checks closed their earlier observed failures. Reading, templates, bibliography,
keyboard, lifetime, OCR and both compiled launchers retain their exact results.

V3's canonical phase completed 103 of 139 final suite reports, with 2304 passes
and zero failures in those completed reports, before the 12-minute phase limit.
Thirty-six reports were absent. The 14 progress suites completed all 280 cases
without a failure or skip, closing the earlier fixture failures. Earlier strict,
provider and installed-recovery gates completed 463, 289 and 809 passes within
their respective scopes. Skipped later native/broad stages remain unqualified.

The integrated Windows source phase remained in progress in both normal run
and latest-jobs APIs at 22:34 UTC, after starting at 21:45:23 UTC. Its configured
step limit is 35 minutes; the whole job limit is 120 minutes. The saved earlier
phase artifact has no source-phase results. No responsible suite, runner teardown
cause or completed coverage is inferred from elapsed time alone.

The exact 9f phase artifact was read through a separate bounded read-only reader:
66 of 67 text members matched their declared SHA-256 and lengths; one changed XML
member is excluded. Recording completed 55 functional checks but its old observer
missed native destruction because the event used a different Python wrapper.
The separately corrected 925 diagnostic completed all 57 native recording checks
and 25 recording plus 29 Book UI source cases. Both the captured native event ID
and independently dead original wrapper passed.

The instrumented Book scenario did pass canonical destination/volume review and
receive explicit Run approval. At the 90-second guard it was still rendering
book 2 of 2. Its worker stack showed repeated durable cancellation, retained-file
accounting and guarded publication. The focused CLI suite completed 11 passes and
two 90-second child timeouts. Batch emitted ten completed passing call records,
then a Windows access violation while its next case was active; it produced no
final JUnit report. The fatal event coincided with the sixth periodic diagnostic
C-stack timer. That coincidence and the observed Python stack do not prove a
product defect or a diagnostic cause. The next diagnostic uses bounded normal
Python frame sampling through an owned output descriptor, leaving the actual
strict runner and all deadlines unchanged.

### Exact reviewed successor components

| Source component | Concrete correction | Required execution boundary |
| --- | --- | --- |
| `c217183cacdf89bc870fbc6670ecec99dc753854` | Capture recording owner ID before binding; observe the real matching native event and separately dead original wrapper. Retain worker-closure checks. | 25 source controls and 57 native checks passed on the exact isolated 925 composition; ordinary successor remains separate. |
| `ed4d967a259a326e46d1cacdc87d5819af84a163` over `1ad45c6483447805685c0979861a7c260bd24e56` | Correct V3 frame/bootstrap observers and retain bounded Book stage/approval/live-status diagnostics. | All 29 Book UI controls passed on 925; actual Book completion still failed its original guard. |
| `1f4d53dddc25b582026a36d4cd56b330e4ddeebd` | Apply captured-owner plus dead-wrapper observation to player, desktop and operations native probes; keep original assertions and deadlines. | Source approval only at this checkpoint; actual probes must run on the successor. |
| `edb85a98afe00f4efaaae66188de7697182e9252` | Recognize genuine owner destruction for reading narration, academic editor/library and federated search; revoke late-result authority and preserve committed data. | Existing assertions remain, with real held/queued-work controls; successor execution required. |
| `f03ac373ab2eba5d329e42e820b161fabaf42123` | Use only the verified current Destroy-event proxy for native hotkey/unbind operations; attempt every owned cleanup, preserve errors and resources for retry. | Adds actual direct-Destroy native checks for timer, tray and reacquisition of the same global key by another window. |
| `5adc38203317456a511d3e8f6eb6c0b14b97868d` | Begin the ordinary disk-accounting quiet interval after successful traversal. | Seven actual-file/durable-control cases retain all forced accounting, fresh stop, source-integrity and publication boundaries. Timing benefit requires actual execution. |
| `499d6eeeddad4d3de6114fe6bf3a5efcc94f00a0` | Preserve all 139 canonical suites in independent required 70/69 partitions and move the exact broad command to its own required job. | Eighteen manifest/partition refusal controls; strict 600-second suites, 12-minute canonical phases and 45-minute jobs remain. The remaining native job body is exact. |
| `549b83fac3a7adb617c2f662332798e282715874` | Clarify implemented ASR review/resume, raster limits and private-model/signing requirements in the Ukrainian startup guide. | Documentation only; no new capability or qualification claim. |

The reviewed ordinary source union is
`4ba611e1396d024c7047ff0b6f790067a61877ab`, tree
`6d2a193c696bbcfcdc4096a2a45ae71cc72b6a9b`. Root and an independent reviewer
read all 24 final changed bodies and compared all 1344 file identities/modes:
21 modified, three added, 1320 unrelated 9f blobs retained, no deletions or mode
changes. The f03 desktop probe/test bodies supersede the earlier versions.
The full component ancestry returns to 9f without importing diagnostic
`925658a4`, `db0a84f5` or any diagnostic workflow.

At this historical evidence snapshot the exact cadence diagnostic
`9f5e9e214e9a0df8c599069222e225c21f964564`,
[run46077443673](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46077443673),
is queued. Source approval and isolated results are not the ordinary assembled
head's acceptance. The complete successor must pass its own required gates;
the current execution outcome remains in PR152. All previously stated private
asset, model/language quality, corpus capacity, human Windows11/NVDA,
physical-device/listening and signed-distribution qualifications remain separate.

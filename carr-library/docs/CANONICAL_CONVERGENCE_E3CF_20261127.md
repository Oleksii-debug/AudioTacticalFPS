# Semantic convergence at canonical e3cf74

## Frozen authorities and meaning

This is a source-linked integration ledger, not a release certificate. It extends
[the e69 reconciliation](CANONICAL_E69_RECONCILIATION.md), the finite V3 gap audit
and the large-source/corpus plan. It does not change the promised scope.

| Authority | Exact revision |
| --- | --- |
| Fresh canonical branch read | e3cf74c243d4ef930b526b5648ca9003c0900057; tree 5fc67a0d9a0ab18fb0662bb01e027d29d83af72f |
| Current composed platform used for comparison | db15b173e5d5bec5919b7072a57ec248c7b96c06; tree 894092b07db0aeb01ea15a419096e28fccb111c9 |
| Current finalization specification | UNIVERSAL_SCOPE_V3_FINALIZATION.md blob db8adbdc483a03c262885928c7ed214ca70bbaa7; unchanged between these trees |
| Detailed V3 scope | UNIVERSAL_SCOPE_V3_AUDIO_AGENT.md blob d0b2761dd22b33233b311d4800230fb47f5d3d8b |
| Fresh PR152 donor | dc477add6db2fc0348637dd460d03b76d8e1c75f; tree 098a1dbbe6ea60995145db3835d1dceb5eaa5027 |
| Fresh PR173 donor | ed74d168c81a21e68bbe1673a729181dfef96ee4; tree db2354f293cfeec693513f8ca25bbaa1df096214 |

All four recursive Git trees were complete, with truncated=false. The associated
[JSON inventory](acceptance/canonical-e3cf-source-disposition.json) records exact
blobs and the current owner for every differing canonical product path. An
inventory entry is not an independent equivalence finding or passing test.

The canonical e69→e3cf range has six commits but only two final changed paths:
research/export_file.py and tests/reader/test_export_reconciliation.py. The
remaining canonical feature/data mapping from the earlier ledger is unchanged.
The current export path lacked the new two-line requested-path/private-root
comparison before reading a replacement target. Candidate
e88132b701bcd0fd064d7404da380335d4100841 ports that exact canonical correction
and its four tests without replacing the current native publisher.

PR152 has 47 commits and 99 changed paths since the reviewed196a donor.
PR173 has nine commits and 115 changed paths since ec513. These are open
development donors, not substitutes for the accepted source tree or its evidence.

## Four required workflows and optional donor choices

| Behavior | Actual requirement | Current finding and narrow integration |
| --- | --- | --- |
| Measured-duration book splitting and headless book operations | V3-04 explicitly includes size/duration splitting, reviewed boundaries/names and CLI/headless controlled jobs. | Book candidate a9e71a61b0355f95c47e5410c6f60590449c8cf3 adapts ten text/landmark modes and ordered managed batches. It still needs measured post-render PCM ranges and a thin current-service CLI. Reuse donor studio/book_audio.py review_duration/publish_duration, but adapt through current audio/publishing.py and exact current completion receipts. No new synthesis should be required to split accepted PCM. |
| Named reusable typed workflows | V3-20 explicitly requires reusable named workflow templates, batch presets and allowlisted macros. | Saved approved jobs and scheduler tickets do not provide named save/load/update/delete/rebind. Maintenance confirmed715/695/f738 tool/scheduler work does not yet close this gap. Adapt donor assistant/templates.py as inert draft data to the current Plan/JobStore/native guards; clear old scope and approval on load, then require current Preview→Run. |
| Local translation capability | Finalization§6/§9 and V3-20 retain approved optional local translation engines beside official Google. | Current providers.py supports Googlev2/v3, OpenAI-compatible and ElevenLabs; it does not expose the donor LibreTranslate protocol. Add a selected local endpoint adapter to the same gateway, language-pair cache, approval/ledger and TranslationService. LibreTranslate itself is a candidate, not mandated; no local model/server or quality qualification follows from a client adapter. |
| Durable playback queue and resume | V3-05 requires queue/playlists and accurate resume. V3-06 explicitly includes queue in Ctrl+S/Save All, autosave and portable reading/audio recovery. | Current WavePlayer has bounded in-memory playlists, seek/repeat/sleep and independent prepared controls. It lacks a typed retained playlist/cursor/selection producer and Save All/reopen review. Adapt donor speech/playlist.py into current audio/player.py ownership and typed backup. The donor's exact5s cadence, labels and standalone layout are optional design choices. Never restore autoplay, a live device handle or old external path authority. |

The requirement rows above identify code work that does not require private
models, paid accounts, physical devices or redistribution approval. Their actual
runtime/model/device qualification remains separate.

## Current single-owner replacements and retained behavior

All product paths below are relative to src/carr_library. A changed module name
does not mean its behavior is absent.

| Donor/canonical area | Current authority and preservation decision | State at this checkpoint |
| --- | --- | --- |
| Catalog, immutable sources and source reading | Existing application.py, storage/revisions.py, catalog and reader/reading_desk.py. ReadingDesk blob0acd7f25 is byte-identical to canonical. Keep V5 import_publication ownership and current exact retained original/page checks. | Integrated existing owner; new capacity query optimization remains a separate reviewed candidate. |
| Search/federation | search/pagination.py, search/federated.py, current application/CLI and native Search Center. Preserve current cursor query/filter/root/revision binding, sparse empty pages and successful-session publication. | Current staged source/library/result continuation already addresses donor search/paging behavior; no second search cursor service. |
| Large scan/intake | ingest/scan_pages.py, batches.py, worker.py, retained_assets.py and pending_backup.py. Saved successor batches retain scanned source identity and restore makes old cursors inert. | Current5003-file continuation and explicit streamed UTF-8 profile retained. Multi-GB TXT/PDF work is an additional pending profile, not an unconditional removal of caps. |
| Research/export | Existing evidence/catalog/academic handlers and research/export_file.py retain captured output approvals, source checks, cancellation and completed-write receipts. | Existing0759 plus exact e88132 correction. Do not replace current native metadata/rename behavior with an older donor writer. |
| Bibliography/Word | citations/csl.py, bibliography_workbench.py and documents.py retain pinned QuickJS-ng, selected custom CSL, editable DOCX references and regeneration. | Keep current extra workflows; a donor branch without documents.py must not delete them. |
| Voice/narration | audio/engines.py, runtime_settings.py, reader/narration.py, live_session.py, source-bound drafts, pronunciation worker, spelling, appearance and normalization. | Reuse current source/voice boundaries; no duplicate speech/runtime.py stack or automatic engine activation from old settings. |
| Reader pacing/listening | Current reader live/recorded sessions, audio/playback_processing.py and existing WavePlayer. Exact source ranges remain separate from estimated time-stretched position. | Current derivative ownership, source revalidation and cleanup handshake retained; physical listening remains separate evidence. |
| Native playback | audio/playback.py and player.py, bounded WinMM/RF64 buffers, explicit device identities, acknowledged Stop/close, native process fallback. | Preserve current native ABI/cleanup fixes. Add durable queue metadata without a second player. |
| Studio and DSP | audio/models.py, production.py, clips/editing/mastering and ui/audio_studio.py; one current typed compiler/render owner. | Keep current clips, source-preserving edits, track/master controls, receipt-bound plans, RF64 and process-family lifetime. Donor schema copies must not remove these. |
| Capture | Current recording/capture types/process/backup and ui/recording_dialog.py. | Pause/resume/monitor ownership and copied managed assets remain. Private hardware qualification is not supplied by synthetic fixtures. |
| Publication | audio/publishing.py plus current EPUB3/DAISY inspectors, media-overlay reader and typed publication state. | Preserve independent EPUBCheck/DAISY workflow, selected-source approval and offline completed output reading. Measured duration splitting is a finite follow-up on this owner. |
| Book source and batches | reader/book_source.py, audio/book_plan/book_batch/book_store/book_audio/book_export and native book dialog in a9e71. | Source-reviewed candidate, NOT_RUN. Current managed projects/render receipts remain the only execution store. Final child adds restored accepted-audio export without live runtime discovery. |
| Media conversion | audio/media_conversion.py, media_types.py, media_worker.py and current native workspace. | Already supports ordered retained batches, explicit video stream, codecs/depth/tags, resume, offline verification, output export and typed backup. Donor AudioRuntime/conversion_batch is not needed as another runtime. Its direct ordered-effects/trim convenience must be assessed against existing Studio clip/effect actions, not silently declared a new mandatory service. |
| Shared resources/Tasks | operations broker/models/admission/facade, existing native Tasks. | Retain actual current ASR/render/import/translation/media/recording stores and recorded-versus-verified statuses. No fictional render pause controller or archived live lease. |
| Progress/Save All | Current ProgressHub, ui/operation_progress.py, media quiet hold and ui/save_all.py. | Preserve observer failure isolation and completion-aware close. New queue/template participants should join this coordinator, not duplicate it. |
| Assistant/local planning | Existing assistant Plan/JobStore/service/llm through root ProviderGateway, scoped review/approval and durable receipts. | Current local model proposal path retained.715/695/f738 typed-tools/scheduler is a source candidate; named templates remain an explicit follow-up. |
| Translation | providers.py, assistant/translation.py/translation_state.py and current UI. | Googlev2/v3 model/glossary/credential/ledger binding retained. Root structured translation751746 has source/core review; UI submit must remain on existing worker. Local LibreTranslate is not yet present. |
| Supplied ASR | transcription_service/state/receipt/backup/cli, selected manifests, supplied worker boundary. | Current relative owned-input jobs and hardened receipt remain authoritative. No alternative transcription package/service or private worker publication. |
| Incremental backups | incremental_backup.py and current Vault transport over current complete backup. | Adapted ec513 behavior is integrated: full authenticated independent inventories/chunk reuse, explicit predecessor token and truthful postcommit attention. PR152's separate chained layer format is optional and incompatible; do not replace this owner or silently claim it can open those archives. |
| Native atomic persistence | audio/jobs.py shared-delete reader and native Windows rename, existing native file metadata and writer locks. | Preserve actual current Win32/process cleanup fixes and PID/thread-owned guard borrowing. No extra durable_state stack. |
| Packaging/trust | Current FULL FFmpeg resolver/pins, reviewed dependency lock, installed-origin gates, TUF update/managed lease/standard installer and neutral entitlement contracts. | Preserve current package identity and rights separation. Source hashes/CI are not Authenticode, paid entitlement activation or distribution clearance. |

## Data ownership and migration

| Historical data | Treatment through the current platform | Never transferred automatically |
| --- | --- | --- |
| Canonical common-v4/legacy backup | Explicit manifest-version dispatch, bounded exact original bytes/provenance, sealed DB validation and typed current producers. | Arbitrary neighboring files or stronger byte budgets derived only from a manifest claim. |
| Canonical state/speech sessions and reading histories | Existing legacy_reading/narration import retains original envelope/history and selected revision with DB-bound source anchor. | Old voice paths, active execution, fabricated new source permission. |
| Canonical state/studio project schema1/2 | Explicit import-as-new through current legacy_project, retained original JSON and copied verified PCM assets. Unsupported meaning is refused rather than lost. | External asset access, old runtime/model/cache authority, existing output overwrite. |
| Canonical state/transcription | Current typed inert recovery archive preserves validated input/output/edit/history data. Explicit selected subset can prepare a new current job with a freshly reviewed runtime. | Old running/completed flags, trusted-profile declarations, absolute source/model paths or old completion identity. |
| Current transcription-jobs | Relative immutable job definition and current/historical completion validation survive restore. | No old absolute paths are followed; restored completed outputs remain readable without weights. |
| Canonical alternate translation/assistant/provider profiles | Historical bytes remain inert under explicit legacy producers. Current flat translation and JobStore schemas are not populated by generic copying. | Credential secrets, provider scope, ledger-root binding and execution approval. |
| Current book plan/batch | a9e71 adds typed immutable plans/batches, exact original/DB relationships and ordinary managed projects to full backup. | Output parent/approval, engine/model paths or fresh inference claims. Donor schema1 BookPlan is not automatically the current schema2 Role contract. |
| Donor new playlist/template records | Not yet registered by current typed producers. Before a final migration claim, add an explicit validated import/preservation route or report unsupported schema. | Autoplay, native handles, live timers, stored scope or execution approval. |
| Machine-local engines and secrets | Current runtime_settings registry remains machine-local and excluded from active portable recovery; inert legacy provenance stays distinct. | Executables, private models, vault credentials and redistribution rights. |
| Alternate PR152 incremental layer archive | Separate interchange format; current Vault repository and ordinary full backups remain authoritative. | Do not treat a foreign layer descriptor as a current complete snapshot. |

Inert retention is preservation, not active compatibility. A historical job may
remain readable without being resumable. A model path in a verified backup is
still not authorization to execute it.

## Evidence, finite limits and next composition

The latest portable/native results must come from the exact composed tree and
installed artifact. Donor396/1660 results and earlier current suite counts are
historical evidence only. New a9e71 book, e88132 export, structured translation,
scheduler and capacity changes prepared during the outage remain NOT_RUN until
their own CI completes.

The capacity lane55ad9a83 adds an actual retained-book harness targeting1k/10k/50k
books and1M segments, with supervised import samples, paging, restart and backup
measurements. That is source under review, not a measured capacity claim. The
known asset.resolve call fixture correction must be composed before its gate.
Multi-GB sources have an explicit profile lane; remaining converter formats,
private model/language tests, actual device/NVDA work, signed installer evidence
and redistribution decisions retain their own status.

For the final two-parent reconciliation:

1. Freeze the current reviewed tree and fresh canonical commit. Re-read both
   heads before publication; this ledger does not approve unrelated later code.
2. Compose exact e88132, the accepted current feature candidates and each
   compatible correction. Retain existing research, reader, audio, QuickJS-ng,
   provider, resource, process and backup authorities.
3. Resolve package exports, native menu registration, source version/SBOM and
   the union of strict source/installed/native selectors against that tree.
   A source-only donor test is not silently skipped to claim success.
4. Record any unported required row as active code work. Do not label the
   platform source-complete over the four required gaps above.
5. Only then create the actual two-parent commit. Verify its complete tree and
   targeted semantic tests; ancestry alone and an "ours" tree are not evidence
   that the canonical behavior or data survived.

# Resonance V3 candidate readiness

Resonance / «Резонанс», by Sceneria Innovative Technologies, retains the existing native Python/wx desktop, SQLite library, exact-source evidence, audio services and installed entrypoints. The current product authority is [the finalization specification](UNIVERSAL_SCOPE_V3_FINALIZATION.md) with [the detailed V3 scope](UNIVERSAL_SCOPE_V3_AUDIO_AGENT.md). This is a source and acceptance record, not a replacement specification or a distribution approval.

## Current source and execution checkpoint

The selected completion branch is `carr/canonical-completion-2aa-20261127`, reviewed
through [PR152](https://github.com/Oleksii-debug/AudioTacticalFPS/pull/152). Canonical
changes through `a333f0723df107aa60b60b8ee2c5ba9f85b25475` are reconciled in
[the convergence ledger](CANONICAL_CONVERGENCE_20261127.md).

The latest completed integrated checkpoint recorded below is **391beff3**, tree
**908accd8e5f306138b80443d51e30381cbcbf1a1**. It precedes the final Read/Skip
component and the two narrow fixture corrections now composed in this branch.
Each result belongs to its exact tree; overlapping selections are not a count
of unique tests.

| Exact391 checkpoint | Result | Evidence and scope |
| --- | --- | --- |
| Expanded reader, audio, recovery, source and assistant composition | **1912 passed; 1 failed; 0 skipped; 85 suites** | [Record](acceptance/20261127-391bef-expanded-reader.json). The sole failure used a blank PNG in a positive commit-recovery fixture. Production correctly refused the empty source. Corrected ae1 preserves real child commit/recovery and adds an explicit blank refusal; its separate installed50-case run passed. |
| Automatic OCR and raster composition | **133 passed; 0 failed; 0 skipped; 8 suites** | [Record](acceptance/20261127-391bef-automatic-ocr.json). Actual pinned detector examples and English Tesseract PDF/PNG/JPEG/TIFF; this does not qualify every language or human Windows/NVDA. |
| Production, audio, reading and assistant regressions | **1955 passed; 0 failed; 0 skipped; 105 suites** | [Record](acceptance/20261127-391bef-production.json). Followed by **772 passed, 1 deliberately deselected Windows-only case**, zero failures. Actual synthetic audio, loopback/provider boundaries and durable output workflows. |
| EPUB and DAISY publication validation | **PASS within their stated scopes** | [Same record](acceptance/20261127-391bef-production.json). EPUBCheck5.3.0 validates three generated EPUB3.3 publications with zero errors/warnings and rejects the deliberately malformed control. Three DAISY cases pass local XHTML/SMIL DTD validation and malformed-XHTML rejection. Full DAISY rule conformance, reader interoperability, human NVDA and WCAG conformance remain unclaimed. |
| Reconciled import, research, backup and recovery | **2768 passed; 1 failed; 0 skipped; 90 suites** | [Record](acceptance/20261127-391bef-reconciled.json). The same blank-PNG fixture is the sole failure. All28 previously failing f249 reconciliation cases pass in this composition. |
| Typed workflow templates and export preservation | **180 passed; 0 failed; 0 skipped; 9 suites**, plus **12 portable identity cases and20 actual native checks** | [Record](acceptance/20261127-391bef-templates.json). Real installed wxMSW keyboard dispatch, empty fresh values, separate Preview, project identity/source reset and no-job execution. Manual NVDA and acoustic quality remain separate. |

The corrected legacy raster component independently passed **50 cases, zero
failures/skips**, from an installed package outside the checkout on exact
`7a2174520b19dc96271e4a62990c3dd83c3ca4c2`, tree
`1930586566f90d596865021760d7b100adf8cacb`.
Its [record](acceptance/20261127-7a2174-legacy-raster.json) retains actual child
publication/recovery, explicit page-checksum review and blank-source refusal.
The composed branch still has its own gate.

### Historical evidence and remaining native execution

The earlier b024 records retain [1761-case composition](acceptance/20261127-b0245f-expanded-reader.json),
[133-case OCR](acceptance/20261127-b0245f-automatic-ocr.json),
[356-case Linux transcription](acceptance/20261127-b0245f-asr-linux.json),
[255 source and71 actual Windows keyboard/draft checks](acceptance/20261127-b0245f-windows-keyboard.json),
and the [bibliography fixture failures](acceptance/20261127-b0245f-bibliography.json).
The reviewed template/export [8a component](acceptance/20261127-8a1a5d-templates-export.json)
also passed its180 strict and12 portable identity cases. These older records
do not qualify later changed bytes.

The actual b024 full Windows job stopped at the bibliography interchange probe:
its observer still overrode `export_to`, while the real dialog now uses
receipt-bearing `export_result`. Component1450ac10 corrects only that observer
and adds two real-controller/writer regressions; real keyboard, confirmation,
competing-save refusal and content assertions remain. Its updated native probe
must pass on the composed source.

Earlier failures remain in [962fd8](acceptance/20261127-962fd8-expanded-reader.json),
[c8cbc5](acceptance/20261127-c8cbc5-expanded-reader.json),
[dc477 source](acceptance/20261127-dc477-expanded-reader.json),
[dc477 production](acceptance/20261127-dc477-production.json),
[dc477 reconciliation](acceptance/20261127-dc477-reconciled.json)
and [55a](acceptance/20261127-55a-expanded-reader.json).
All12 failures in962/c8 passed after correction in b024; the f249 transport
fixture and all28 reconciliation fixtures subsequently pass on391.

The historical [dc477 Windows lifetime record](acceptance/20261127-dc477-windows-lifetime.json)
contains262 filesystem,26 native-source and26 installed passing cases.
The f249 installed V3 recovery selection reached its10-minute step deadline;
the current gate retains that budget and adds bounded per-case durations and
thread diagnostics. A partial or timed-out run is never a pass. Full native
and installed-bundle gates remain required on the final candidate.

The authoring executor became unavailable during this session. Later source
was preserved through immutable Git objects and expected-head updates.
Execution claims come from returned completed CI jobs; no later local
execution or unavailable private engine is claimed.

## Integrated workflows

| Requirement area | Current integrated behavior | Acceptance boundary |
| --- | --- | --- |
| Library, sources and research | Scoped original assets, catalog/import queues, exact quotations, historical source metadata, notes/evidence, academic references and source review; bounded search windows through the existing services | Main Search, Search Center and CLI now expose source/result/library continuation and a configurable cooperative allowance. Source cursors recheck current catalog/revision authorization; ranking is local to each window. The actual501-edition/multi-segment core and federated gates passed on dc477; current native-navigation and composed changes require fresh execution. See [paging contract](SEARCH_PAGING.md). |
| Larger original files | TXT/PDF up to128 MiB use bounded verified streams through the same original store, isolated worker, source attestors and complete/pending backups; other in-memory formats retain64 MiB | Componentsdfaf06bf/9500a505 and their exact continuation union are source reviewed and integrated. Actual80 MiB meaningful TXT and128 MiB PDF, child memory, cancellation after commit and backup/restore gates passed on962fd8/c8cbc5 (11 intake and 6 workflow cases). Componentca93d68a extends exact streaming originals to private native-view copies with cancellation, no-clobber and preserved editor changes. Its six large-source and27 source-view cases passed in b024. Original TXT segments remain bounded at4 MiB, with no invented pagination. See [profile and fixtures](LARGE_SOURCE_INTAKE.md). |
| Large folder intake | Existing Import Center and guarded CLI now review ordinary saved pages, explicitly Continue and explicitly Run each page; source changes and restored external cursors require fresh review | The actual5007-file/restart/child-death, retained backup/restore and admission-deadline cases passed in b024's23 continuation cases, together with all three CLI cases. Native keyboard and later legacy-request changes require their own runs. No total-tree5000 limit or automatic import is inferred from one page. See [continuation contract](IMPORT_SCAN_CONTINUATION.md). |
| Reader | Reversible reading copies, original comparison, Undo/Redo, Unicode navigation, search/replace, pronunciation, spelling, presentation, voice preferences, several recoverable tabs, passive exact-text bookmarks, paragraph split/join and explicit Read/Skip categories with exact narration preview | Reader/mark/tab/focus/lifetime composition passed on ad679. Native and manual acceptance is tied to its own build. Missing selected voices remain missing, without silent replacement. |
| Paced reading and listening | Explicit visual Start/Pause, clearly labelled approximate audio follow, independent speed/pitch listening derivatives, seek and resume using measured source-time mapping | Actual FFmpeg listening and source-identity checks passed ad679. No exact engine word timings are inferred from duration. Originals remain unchanged. |
| Standalone playback | Ordered reviewed PCM playlists on the existing WavePlayer, one UI owner/timer, sleep and A–B controls, per-instance volume/mute/balance through owned WinMM handles | Source component c5c57241 is integrated. File review and next-item admission remain explicit and bounded; native cleanup retains buffers until acknowledgement. Source/native gates are pending on this exact candidate. Queue component0c2bce30 adds passive Ctrl+S, Save All and autosave through the same SaveCoordinator, content-addressed retained media and typed complete backups. Reopen and restore require explicit fresh review and never start audio. All20 portable recovery cases passed on dc477; current native installed recovery requires its own run. |
| Book Audio | Existing chapter/batch narration, role/pronunciation settings, landmarks/subtitles, new exports and measured target-duration review using actual rendered frames | Actual measured duration, CLI, publication/backup and native-handler tests passed ad679. Long synthetic fixtures do not establish long neural-model quality. |
| Studio, capture and effects | Existing clips/timeline, explicit range edits, multi-track/role production, effects/presets, measured loudness/peaks, microphone pause/resume and monitoring; ordered audio conversion batches in native Studio and CLI | Batch component2f80fc5d reuses AudioRuntime, shared resource admission and per-file durable publication receipts. Crash recovery admits only the exact owned pending alias. Actual core19/UI12/CLI6 gates passed on55a with real FFmpeg. The integrated candidate and native installed controls still require their own gates; physical listening and private engines keep separate qualification. |
| Recovery | Current full v4 snapshot producer, explicit legacy adapters, pending input recovery, typed reading/studio/ASR state, original/evidence bindings, Save All and scoped draft recovery | Byte/schema/source validation remains mandatory. Restored external runtime trust is revoked before publishing the new root. |
| Incremental backups | Changed-file layers over canonical full snapshots, explicit checksum-selected parent chains, verify/create/restore in Library menu and CLI, bounded private materialization | 22 actual filesystem/core cases and eight queued action cases passed ad679. The follow-up budget check now precedes any full-base payload verification, including final chain recheck; both new bound-order regressions passed on55a (24 core cases total). Eight incremental layers maximum; a layer requires its bases. Creation still stages a full canonical snapshot. See [workflow and limits](INCREMENTAL_BACKUPS.md). |
| Supplied transcription | Existing receipts-v1 and V5 worker protocols, retained input batches, exact environment/model review, named profiles, durable pause/cancel/interruption review, selected exports, separate editing, native workstation and CLI | Integration source is 6a9b5d96/65a2c287 on the existing flat service. Synthetic worker execution proves protocol/application behavior only. New profile/restore/control gates and actual native workflows must pass on the delivery candidate. See [current runtime](transcription_runtime.md). |
| Shared operations | One cooperative CPU/RAM/disk/device broker around existing jobs; exact workspace views, durable supported controls, admission held through process/device cleanup | Reservations are declared allowances, not measured model capacity. An accepted pause/cancel request is distinct from acknowledged worker stop. No second queue is introduced. |
| Translation | Basic v2 and Advanced v3 plaintext derivatives, protected OAuth references, exact provider/model/glossary binding, per-request approval/cost ledger and chunk receipts; ordered multi-document review, preparation, resume and provenance exports in native UI/CLI | Components b131e0c7/28c3ecf3 reuse existing TranslationService/JobStore and typed backups. Each child keeps its own approval, source binding and receipt; uncertain charges never receive automatic retry. The28 batch,18 CLI,9 native-handler and11 backup cases passed in b024; actual native installed controls retain their separate gate. Synthetic transport does not establish a live account, billing rate, translation quality or local model. |
| Assistant | Guided commands and explicit local-provider plan proposals, review before submission, validated scoped tools, durable plan/checkpoints; exact reader ranges, authenticated evidence search/read/export, configured voice inventory, measured audio inspection/effects and conversion/render adapters | Typed component4ec1d436 uses the existing Plan/JobStore and native/audio/library services. The43 typed-tool cases and21 assistant cases passed in b024, including actual source and FFmpeg paths. A typed plan or synthetic local response is not a qualified local LLM. Componentf138ac70 adds the exact live-reader status/Pause/Stop bridge through the native event queue and existing player; unknown prior dispatch is not retried. Assistant search now uses the canonical source-page continuation. All18 portable player-bridge and11 assistant paging cases passed in b024; native event-loop and installed gates remain separate. Componentd3fb6e4e preserves producer path/size/SHA receipts through conversion, evidence export, Studio and transcription; acceptance uses a bounded stable reader and never adopts substituted bytes. Real FFmpeg/export replacement regressions passed in b024. The later canonical PR191 export guard retains all old cases and adds four no-read replacement cases; all20 composed cases passed on391. |
| Academic/document integration | Current supported TXT/PDF/JPEG/PNG/TIFF/DOCX/EPUB/text-adapter formats, strict local BibTeX/RIS/CSL interchange, seven pinned CSL styles through QuickJS-ng, editable DOCX citation provenance, explicit lawful metadata acquisition and portable sync boundary | Unsupported adapters are visible. No implicit quotation approval, live Word add-in, external publishing or arbitrary provider support is inferred. |
| Accessibility and delivery | Native labelled controls, semantic command registry, configurable keys, explicit progress feedback, private runtime/launcher, update/entitlement verification boundaries and standard-installation lifecycle code | Automated Windows evidence is useful but distinct from human Windows11/NVDA. A checksum or passing source test is not Authenticode, publisher trust or distribution clearance. |
| Languages | Canonical 135-entry language inventory and capability/qualification views | OCR, ASR, translation, UI, voice and model coverage are independent. The inventory is not a claim of 135 qualified speech languages. |

## Reviewed finite source additions and their execution gates

The updated specification remains the authority. Source implementation and execution are recorded separately.

| Requirement | Current component | Remaining work |
| --- | --- | --- |
| Larger TXT/PDF and continued import scanning | Streamingdfaf06bf/9500a505 and scan040ea858/45ec0ca9/c25c2c6d are composed with existing source/search/restore semantics | Fresh actual source and installed tests; current source review is complete |
| Typed assistant and current-player bridge | Components4ec1d436/f138ac70/d3fb6e4e are composed with one Plan/JobStore and canonical player/services | Fresh replacement-at-publication, actual player/UI event-loop and installed regression gates |
| V3-20 named workflows and batch presets | Componentbe707b10 adds the passive typed catalogue, native save/load/delete, fresh Preview/Run and the existing semantic keyboard registry; spoken-equivalent names cannot create ambiguous choices | Components78d1291/b6059349/8a1a5d2 now add explicit typed parameters and bounded per-item overrides, project capture with fresh IDs and cleared source claims, one semantic review command per template, native authoring and a capture/preview CLI. All91 template/planning/command cases passed in the391 composition;20 actual installed native checks also passed there. The final composed source retains its own gate. See [named workflows](ASSISTANT_TEMPLATES.md). |
| V3-01 image intake and automatic OCR language selection | Rastera9a75e89 and automatic-language componenta5ab8240 are composed on the existing OCR pipeline. Manual remains default; the pinned offline detector classifies without restricting its language inventory, then selects from at most 8 reviewed installed models. Up to 12 bounded paragraph alternatives keep independent raw text, TSV, pixel origins and uncertainty; no concatenated or approved quotation is fabricated | The 34 portable raster cases passed on c8cbc5. The 133-case b0245fe composed OCR job passed actual detector English/Ukrainian/Slovak samples, English Tesseract PDF/PNG/JPEG/TIFF, passive preset/restore and manual-override checks. Native Windows keyboard and multilingual OCR quality remain separate. See [automatic OCR](AUTOMATIC_OCR_LANGUAGES.md). |
| V3-04 audio metadata and cover art | Component427e810f with reviewed cleanup78c7c1bc and fixture correctioncca1e0ac adds MP3/FLAC/M4A/M4B tag and local cover review through the current AudioRuntime. It preserves encoded audio packets and measured chapters in a separate no-clobber output with exact publication receipts. Passive saved choices/artwork participate in typed backup; restoration requires fresh explicit source/output review | All129 cases in seven metadata/cleanup/publication/hash/passive-backup/UI suites passed in b024, together with existing process/DSP/conversion selections. Successor73516f52 binds saved-artwork export to the exact pre-picker review and adds two record-replacement regressions; all131 metadata cases passed on391. Native/installed probes retain their own gate. See [metadata and artwork](AUDIO_METADATA_COVER.md). |
| Legacy import defaults | Componente6e0461f preserves historical seven-field OCR configuration bytes while recognizing absent automatic-language selection as strict False only at the prepared-request and fresh committed-reply boundaries. New child requests retain an old batch's validated wire; runtime hashes, receipts and passive backup comparisons remain exact |24 actual-child, unprepared/prepared, deleted-original, lost-reply, full-backup and refusal cases plus26 supervisor cases passed on the installed exact7a component. All source/installed selections include the corrected ae1 fixtures; the final composition requires its own run. |
| V3-02 reviewed Read/Skip categories | Reviewed component11f374a4 adds bounded explicit footnote/table/math/code/heading/other regions, category Read/Skip and whole/selection/cursor preview through existing ReadingDocument/Reading Tools. It preserves originals/history, exact source mapping, optional v4 passive recovery and old wire formats | Two independent reviews approved exact-policy adoption, native Destroy cleanup, pre-publication and deferred-play checks, truthful completed-WAV receipts and zero invented filtered timing. New core/UI/real-worker/PCM cases and the extended actual keyboard/restart probe are composed; fresh execution is NOT_RUN until the candidate gates finish. No automatic semantic classification is claimed. See [reading tools](READING_TOOLS.md). |

The actual malformed-package RECORD and runtime-drift ASR cases passed in2fb Linux source gates. The actual export regressions for mismatched approval before destination opening now all pass in dc477. Historical evidence is never copied to a new tree.

A source-complete claim requires closing the finite rows and rerunning the assembled candidate. External qualification and distribution prerequisites below remain independent.

## Explicit capability limits

The requested format inventory remains in [document compatibility](DOCUMENT_FORMATS_V3.md).
AZW/AZW3, CHM, DjVu, legacy DOC/PPT/XLS, LIT, MOBI, PDB/PRC, RTF, TCR and WPD
remain unsupported/pending converter paths; no calibre/LibreOffice/Pandoc runtime
is silently installed or invoked. The detailed V3 inventory explicitly permits
supported, limited, pending and unsupported classifications. This is a declared
product limitation, not a missing Windows test, and it must not be advertised
as implemented all-format support. Optional BRF, live Office add-ins, third-party
audio plugins and encrypted/scheduled backup have their own declared scopes.

## Qualification and delivery gates

The following require evidence or inputs beyond source completion:

- Exact authorized private cloning/dordiai runtime and reference recordings, source/model/voice licenses, multilingual similarity/intelligibility and supported hardware.
- Exact supplied ASR SDK/model inference, recognition quality and long-input resource measurements. Diarization and extra alignment capabilities require separate explicit support.
- Each selected neural music, local translation or local LLM runtime/model with its rights and measured capacity. Procedural audio or synthetic protocol responses do not qualify these models.
- Human Windows11/NVDA, real keyboard/device recovery and listening review on the exact installed build.
- Publisher signing identity, signed installer/repair/uninstall, production update/entitlement trust and approved release/distribution obligations.
- Actual1k/10k/50k corpus and large-media measurements with throughput, latency, memory/disk, restart and isolation. Initial targets are not present capacity claims.
- Optional online accounts, external provider permissions and billing. No paid call or secret access is implied by implementing a provider adapter.

The default remains local/offline operation with access to user-owned data. This technical work does not activate paid services, select prices, publish private voices/books/models or waive third-party notices and corresponding-source obligations.

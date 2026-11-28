# Resonance V3 implementation and delivery status

Resonance by Sceneria Innovative Technologies combines the research library,
reading tools and audio workflows. This document separates source integration,
executed tests and real device/model acceptance. A complete signed release is
not established by passing synthetic-engine tests. Package names, launcher
filenames and data locations keep their existing compatibility contracts.

## Current canonical convergence

The source composition reconciles canonical
`e5c64a5245583f8f8b404a0f042f6ca438c24d7f`, the earlier PR143 feature input
`198a488b72c73b57e7e8bad87d69c3915bfd5e31`, and the reviewed integrated
reader/conversion/effects/recovery, retake, dictionary, schema3 clip, independent
playback, verified-download continuation, typed local tools, streamed import and backup,
complete-scope search, explicit automatic OCR, local translation and reviewed playback queues.
It follows the unchanged current
`UNIVERSAL_SCOPE_V3_FINALIZATION.md` and retains catalog V5, original bytes,
source lineage, import publication receipts and both exact saved ASR codecs.
The committed files are a source composition. Execution verdicts belong to the
Actions run and installed artifacts for this exact commit; they are not
predeclared by this document.

The convergence uses one current backup writer, one bounded citation executor,
one transcription service with explicit historical/current codecs, one command manager and the shared progress
contract. Legacy formats are decoded explicitly. Their presence never grants
permission to execute restored engines, change user shortcut profiles or
accept source/receipt identities inferred from matching text.

Complete-scope reads use the existing current-edition and page keys without
changing catalog V5, source ordering or current-source authorization. Within one
public search-page call, streamed original proofs may be shared for the selected
segments. Complete original hashes, current path identities and the catalog
epoch are checked before publishing that page or its cursors. Later calls and
quotation copy obtain fresh authority. The bulk ASCII path retains the same
Unicode normalization, source offsets and bounded cancellation checks.

## Completed public implementation additions

The current source retains these completed implementation groups:

| Requirement | Current route | Verification boundary |
| --- | --- | --- |
| V3-03 / V3-05 independent listening | Native reader/Studio settings for tempo, pitch, volume, mute and balance reuse the existing player, worker and admission owner. Tempo/pitch prepare bounded private PCM; gain/mute/balance affect only queued PCM for the selected output. | Source audio, synthesis settings and other players stay unchanged. Existing MCI is the default; explicit levels use an owned WinMM handle. Exact queue bounds and device limitations are documented in [independent playback](INDEPENDENT_PLAYBACK.md). |
| V3-09 typed local tools | The existing Assistant now has 21 allowlisted tool names, including current-source/evidence selection, voice/effect discovery, inspected serial audio conversion and current-reader status/Pause/Stop. | Preview and explicit Run use the same durable executor and exact artifact receipts. Completed side effects are verified before recovery; a late journal warning cannot invent rollback or authorize replay. The existing reader bridge never creates a second player. See [local tools](ASSISTANT_LOCAL_TOOLS.md). |
| V3-18 Pause / Resume / Cancel | The existing signed updater retains a bounded inert prefix, refreshes TUF metadata on Resume and verifies the full signed length and hashes before making an installable package. | Actual local/TLS Range cases and native keyboard/progress/lifetime probes are included. Opening/restoring a checkpoint neither resumes nor installs. See [verified continuation](VERIFIED_UPDATE_RESUME.md). |


The capacity and workflow completion also reuses the existing services:

| Addition | Current route | Verification boundary |
| --- | --- | --- |
| Streamed sources and durable import pages | Explicit TXT/PDF resource profiles, finite folder windows, saved scan continuations and independent per-book commits in one current import slice | Every source, request, checkpoint and actual publication receipt retains its own identity. Resume is explicit; each completed book survives a later failure. See [Import Center](IMPORT_CENTER.md). |
| Automatic OCR and static images | Pinned first-pass language classification, explicit selected language pool, bounded region analysis and existing Tesseract/PDFium quarantine; checked PNG/JPEG/TIFF import | The selected OCR models and exact native runtime remain visible. Automatic language selection never approves quotation text. See [automatic OCR](AUTOMATIC_OCR_UK.md). |
| Complete selected search scope | Bounded result and source cursors, complete-scope lexical/BM25 ranking and batched real MiniLM scoring | Current scope, retained originals and exact quotation-copy checks apply on every continuation. The real model regression covers 607 sources beyond the former 500-source window. See [search paging](SEARCH_PAGING_AND_RESOURCES.md). |
| Explicit local translation | A user-selected numeric loopback LibreTranslate-compatible service through the existing provider gateway, scoped approval, ledger, single-document and batch jobs | No server or model is installed or started implicitly. Actual HTTP/TLS cancellation and native controls are qualified separately from language/model quality. See [local translation](LOCAL_TRANSLATION_UK.md). |
| Reviewed playback queue | Passive saved queue and skip intervals in the current Studio/player/worker ownership model | Output and entries need current review. Restore never starts playback; Stop, Pause, replacement and native destruction revoke pending advancement. See [playback queue](PLAYBACK_QUEUE.md). |
| Full and incremental recovery | The same common format-4 snapshot/restore checker, explicit resource controls and a caller-selected chain of exact parent manifest hashes | Eight-layer maximum, bounded private reconstruction, unchanged historical readers and create-only final publication. Restored jobs and settings remain passive. See [backup and recovery](BACKUP_RESOURCES_AND_RECOVERY.md). |

The same composition retains canonical Save All behavior, exact ASR runtime-file
inventory, both saved transcript codecs, and source-preserving no-clobber exports.
Office conversion uses the reviewed short native workspace. The XML reader
validates the observed empty static geometry and, only in direct ODS master-page
style slots, the four recorded inactive Calc sheet/page templates. It reports
their omission without turning cached fields into source text. Other hidden
text, body lookalikes, unsupported drawing formulae and unknown attributes remain
explicit refusals. Spreadsheet formula cells retain the existing omission
warning; their evaluated values are not presented as source text.
The native ASR page stops and detaches its timer before an owned worker can outlive
the deleted window.

Assistant state reuses the shared descriptor opener and atomic replacement
helper, so native Windows polling can retain a valid old snapshot while the worker replaces
its checkpoint. The 64 MiB Assistant budget, source/schema checks and bounded
reads remain explicit. A retained process owner now finishes its registered
callback drain before publishing completion; a competing operation cannot
observe success before the retained lock-release callback has returned.

Native EPUB acceptance has two explicit modes. Its default executes WavePlayer
against actual Windows output. The controlled-output mode uses the existing
disclosed PCM fixture while executing actual publication/import, prepared-WAV
ownership, native keys, source revocation and cleanup. It reports physical output,
WavePlayer and listening as NOT_RUN; it is never an automatic fallback after a
physical failure. Actual device inventory is preserved independently.

Actual hosted Windows evidence at `0c3068672884058d3479144431369120e68ca4ee`,
[run46041427289](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46041427289),
reported zero WinMM output devices and refusal of all four tested output formats;
the physical EPUB attempt failed with MCI326. The same run passed all 108 source
cases and the native retained-timer direct/parent deletion checks. CI therefore
selects the explicit controlled EPUB mode while keeping that failed physical
attempt and the real-device requirement visible.

## Implemented components and acceptance boundaries

| Area | Implemented behavior | Evidence and limits |
| --- | --- | --- |
| Library and OCR | Scoped original bytes, exact quotations, academic references, managed Fast/Best OCR profiles, explicit automatic language/region selection and static-image import; larger TXT/PDF sources use reviewed streaming budgets | Real source/installed OCR and image regressions plus actual native Windows controls; classifier inventory and supported model availability do not establish every language's OCR quality |
| Reading and voices | Source-preserving reading derivatives, portable edition/revision/asset lineage, explicit detached-draft recovery, native pronunciation review and bounded author-observed BXD/DIC/REX text-rule import, recorded EPUB/SMIL playback and the existing SAPI5 COM worker | Historical formats and original bytes remain intact. Recorded overlays use exact fragment ranges, isolated bounded parsing and shared admission before snapshot writes, extraction and conversion. Native lifetime/keyboard gates require exact-head execution; physical listening, reader interoperability and human NVDA remain separate |
| Session playback controls | Explicit Windows output selection and opt-in tray/global keys for one current live reader; no hidden clipboard watcher or background daemon | Actual device inventory and OS key registration are native gate subjects. Session selections are temporary, owner-bound and revoked on native destruction; physical routing/keypresses/listening remain separate |
| dordiai-voices bridge | Explicit connection to a separately installed reviewed local runtime; bounded JSONL discovery/PCM, persistent worker, content-bound cache fingerprints and cancellable inventory | Public adapter only: no private worker/model bytes are copied. Synthetic actual-child tests cover changed assets, cancellation, cleanup failure and retained ownership. An installed owner runtime, authorized reference material, hardware capacity and acoustic quality still need independent qualification |
| Audio studio | Existing roles/scenes/narration/DSP, explicit microphone capture controls, optional selected-output dry monitoring, retained PCM and retake receipts; schema3 sample-range editing, crossfades, markers, loop and dry/wet preview, and typed effects on clip/role/scene/master buses | Schema1/2 wire fingerprints remain unchanged. Retakes and rendered joins create new retained assets. Real FFmpeg range/resampling, old-cache rejection, lifecycle and backup tests are required; exact installed native gates exercise keyboard/loop/ownership. Physical microphone quality and measured monitoring latency remain separate. See [clip editing](STUDIO_CLIP_EDITING.md) |
| Ordered effects and presets | Typed supported offline effect chains, reordering, bypass, bounded undo/redo and named local presets; exact preset files included in complete backup | Installed FFmpeg capabilities and numeric limits are rechecked at execution. This extends the existing conversion path and does not establish live microphone effect monitoring |
| Synchronized EPUB and DAISY publication | One Studio publisher, native format selector and CLI export completed schema1 narration and schema2/3 clip productions as EPUB3 media overlays or a DAISY2.02 NCC/XHTML/SMIL/MP3 file set; DAISY requires explicit publisher metadata | Both formats retain the same resource, destination, atomic publication and prepared-receipt recovery authority. Exact-head tests include real process death, backup/restore and native controls. Pinned EPUBCheck and offline DAISY DTD checks are required; DTD validation does not certify all DAISY rules, hardware readers, word alignment or Braille |
| Shared operations | Native Tasks and resources view and CLI over the existing import, Studio, ASR and assistant stores; cooperating CPU/RAM/disk/device reservations, shared CUDA exclusion and cancellable admission | Canonical ASR, actual isolated imports, process ownership and controller regressions are required by the exact portable/native/installed gates; disposable metadata cleanup cannot reverse a verified completed output; hardware capacity and hard-crash orphan management remain separate |
| Research search and acquisition | Existing isolated document adapters, advanced query builder, complete selected lexical/BM25/semantic scope with bounded continuation and cache; opt-in DOI/ISBN metadata and source acquisition | Actual 607-source MiniLM and 365 native search checks are component evidence. Exact final corpus gates measure scale separately. Provider statements never approve quotations or establish rights; live endpoint and full-format fidelity remain separate |
| Documents and citations | Bounded BibTeX/RIS/CSL-JSON import, selected-field metadata review, isolated conversion, seven current CSL styles and historical editable DOCX regeneration through one bounded QuickJS executor; original style/processor provenance retained | Actual parser, local processor, cancellation/rollback, generated-document and overwrite-confirmation regressions; no live Word add-in |
| Workflow templates | Typed assistant, Studio-project and retained-batch templates; explicit parameters and per-item overrides, reviewed expansion, import/export and passive backup | One existing execution host and current domain services. New use rechecks scope, inputs, voices and output permission; restoring or opening a template executes nothing. Native keyboard/lifetime acceptance is independent of model quality. See [templates](WORKFLOW_TEMPLATES.md) |
| Selected legacy reading | Explicit checked LibreOffice and Calibre routes, plus isolated MHTML/TXTZ/readable ZIP text | Fixed supported families, retained originals and the current no-clobber derivative/provenance pair. Dedicated actual six-family Office and eight-family Calibre gates are required; no visual fidelity, hidden OCR, decryption or automatic converter selection. See [legacy](LEGACY_DOCUMENT_READING.md) and [Calibre](CALIBRE_READING.md) |
| Retained document batches | Existing conversion workers and reading copies, exact stream splitting, portable filename previews, explicit review/approval, no-clobber text/provenance and Studio output; UI and CLI share one service | Completed artifacts are reverified on resume; restored destinations require fresh review. Estimated duration is not measured speech timing, and imported prose is never executed as a Studio script |
| Recovery | One common format-4 writer with explicit resource budgets for database, streamed originals, settings, pending imports, reading drafts, projects and typed retained Studio/ASR inputs; strict historical readers 1–5 and explicit-chain incremental snapshots | Full and incremental paths share the same complete snapshot verifier and create-only restore. Current typed manifests, retained input checks and exact parent hashes govern recovery. Restored engines, scan authority, provider approvals and playback remain inert |
| Exports | Selected-file approval binds file identity and bytes; new files publish without replacement; completed exports retain success if only temporary cleanup fails | Actual competing creates, equal-size content edits, moved destinations, cancellation and bounded-read tests; existing files above the 32 MiB overwrite-check limit require a new filename |
| Supplied transcription | Existing Whisper worker protocol, strict source preparation, reviewed local runtime/model and complete recorded-distribution inventories, isolated owned processes, captured export/edit identities and atomic TXT/SRT/VTT/JSON receipts; native file/folder jobs, pause/cancel/resume and transcript editing, plus CLI | Actual-child regressions distinguish the unchanged native deadline from later synchronous validation: timely native exits remain timely; living roots and descendants are stopped at the original limit. Every final cancellation and content check remains required. Synthetic recognition does not establish real model quality, throughput or human NVDA |
| Keyboard and progress | One command manager and contextual keymaps, searchable command palette, one shared progress contract with compatibility facades, measured or indeterminate native indicators and optional selected local-voice notices | The palette preserves explicit saved shortcut conflicts and stable command identities. Audio playback/capture quiets notifications. Cold voice discovery is cancellable and stays off the UI thread; failed worker cleanup retains ownership for retry. Exact-head native probes are required, and manual NVDA remains NOT_RUN |
| Language inventory | Exact canonical 135 entries, source audit and searchable capability matrix | Resource inventory is separate from acoustic qualification; no claim of 135 working voices |
| Assistant and providers | Existing typed scoped executor and deterministic parser plus an explicit local compatible-LLM proposal path, profile selection, plan preview and separate Run action in UI/CLI | Capability, profile identity, scope, budget and plan validation surround the real gateway. Edited goals discard pending proposals. Synthetic loopback HTTP and native controls are tested before the runtime firewall; actual model inference and paid provider quality remain unqualified |
| Translation | Official Google Basic v2/Advanced v3 and explicit numeric-loopback local translation; current source ranges, actual directional inventory, scoped approvals, decoded-text receipts, export and ordered reviewed batches through one gateway/ledger | Actual local HTTP/TLS, cancellation, Unicode and native controls use disclosed protocol fixtures. Completed derivatives remain available offline. Actual selected model/API quality, IAM, glossary coverage, formatted-document fidelity and automatic token renewal remain separate |
| Signed maintenance | Explicit owner-source HTTPS/TUF check and download, separate reviewed activation, protected-anchor-bound portable and standard update/recovery, native maintenance status and current owned launch entry | Default installation invents no publisher authority. Synthetic signed fixtures and actual setup/health/repair/uninstall gates are separate from production signing, hosting and credentials. Successive updates retain an independently bound generation history and existing research data. See [maintenance](SIGNED_UPDATE_MAINTENANCE.md) |
| Delivery preparation | Native ResearchLibrary.exe, exact-source sealed bundles and the existing Inno Setup wrapper: per-user Start-menu launch, versioned repair generations and normal uninstall preserving unknown/data files | Required Windows gates bind build/source/executable identities and actual install, failing bootstrap, repair with an old interpreter alive, uninstall and final bundle seal. This is an unsigned development installer; exact-head native acceptance, publisher signing and distribution clearance are separate |
| Portable snapshots | Explicit snapshot publication, inspected transfer codes, optional password encryption and restore into a new folder over the complete typed backup producers | AES-GCM, scrypt and HKDF use the pinned cryptography library. Actual encrypted roundtrips, competing writers, process interruption, changed destinations and committed-cleanup outcomes are covered by source tests. Windows DPAPI/native controls and the installed artifact need exact-head qualification |
| Public trust client | Versioned Ed25519 entitlement verification, signed-file renewal, configurable idle refresh, protected credential references, replay anchors and local TUF update verification | Public clients and synthetic policy fixtures are integrated; production authority, endpoints, trust roots, account/billing service and signing material remain separate owner configuration. Managed installations continue to use their approved installer path; an update is not activated merely by checking status |

| Reader appearance and spelling | Explicit font, colors, high contrast, 50–400% zoom/reset and bounded saved preferences; code-only spelling engine with explicit local dictionaries, suggestion preview and undo | Current source regressions passed at96294e; native lifetime/installed composition still needs exact-candidate acceptance. Dictionaries are not downloaded or bundled implicitly |
| Audio metadata and cover | Native review and bounded metadata/cover editing through the existing audio tools and destination publication authority | Common and installed/native probes are required in the current feature integration and full Windows bundle gates |

## Current required gate inventory

| Gate | Required evidence | Separate limitation |
| --- | --- | --- |
| Portable source integrity | All selected existing and new suites, zero failures/skips, actual declared OCR/audio dependencies and real POSIX races | Synthetic recognition/model peers do not establish inference quality |
| Native Windows V3 | Installed source identity, citation/backup/import/ASR tests, actual native controls and verified runtime egress denial | Posted input and native controls do not replace physical keyboard, listening or NVDA |
| Full Windows bundle | Exact wheel/tree, sealed install, native probes, source and installed suites, EXE restart, repair/uninstall and final seal | Unsigned development installer; distribution and production trust require separate clearance |
| Native launchers | Both MSVC and pinned Zig for this exact committed application source | Compiler execution does not prove publisher signing |
| Bibliography | Installed import/export, seven retained styles, original/adapted runtime receipts and historical DOCX regeneration | No live Word add-in or external provider acceptance |
| Selected legacy converters | Actual six-family Office and eight-family Calibre conversion with selected executable identities, private worker ownership, cancellation and unchanged source bytes | Authored fixtures establish their exact supported text routes; visual fidelity and third-party protected material are outside these probes |
| Current feature integration | Every explicit current source and installed selector, platform-specific ownership race and native workflow probe in the committed Windows/V3 workflows; installed module origins and complete package-byte inventories before and after execution | Focused component evidence supports diagnosis; the full installed bundle and source matrix remain required for the exact integrated candidate |
| Actual library capacity | Exact PR-head checkout and installed-byte proof for Linux and Windows, each with 1k/10k/50k books × 40 segments and genuine 65 MiB/129 MiB/2 GiB text sources; real crash/resume, catalog, Unicode quotation, scoped search and ranking measurements; full backup, verification and fresh restore on both largest tiers | Small fixture contracts and individual completed tiers do not qualify another size or a later composition. Runtime, individual-process memory and disk observations are recorded per actual tier; simultaneous process-family peaks and unmeasured device metrics stay explicit |

The Actions checks attached to [PR143](https://github.com/Oleksii-debug/AudioTacticalFPS/pull/143/checks)
and their exact-commit artifacts provide the current verdict. A failed,
unfinished or skipped required job is not a PASS. The focused component review
runs are diagnostic evidence and do not replace the complete matrix.

The explicit Windows source deadline is 900 seconds only for
`tests/transcription/test_admission_boundaries.py`. The
[measured process run](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46044998689)
completed all 476 installed cases, including all 41 admission cases, while its
source selection reached 39 of those 41 before the old 600-second aggregate
guard. The same source run exposed the retained callback completion race.
The default deadline for every other selected suite stays 600 seconds; actual
operation, cancellation and native cleanup limits are unchanged. The runner
still rejects skips, expected failures, empty results and incomplete process
cleanup. Its explicit-selector routing and real child deadlines have their own
regressions.

The Windows source aggregate remains 60 minutes. The V3 installed aggregate is
105 minutes, its native aggregate 65 minutes and its job 240 minutes. The main
Windows bundle aggregate is 160 minutes within a 270-minute job. The predecessor
96cf8a V3 run completed 8,416 installed tests in 4,208.93 seconds (70m09s) before
its later native palette failure; the expanded selectors and retained installation,
native, repair and sealing work need their own finite aggregate allowance.
These changes preserve the default 600-second suite guard, explicit 900-second
admission guard and all individual operation, cancellation and native cleanup
deadlines. A timeout remains a failed or incomplete result, never a PASS.

## Qualified component evidence

These are completed component runs at their recorded commits. They do not
transfer a PASS to a later composition. The final PR checks, exact installed
inventory and corpus artifacts remain the integration authority.

| Exact component revision | Executed scope and result | Evidence boundary |
| --- | --- | --- |
| 043f494eed00e6d08fc90f98786720c44e08613a | Automatic OCR/image component: Windows 194 source + 194 installed + 194 semantic-environment cases; Linux 214 + 214 + 214; native Windows 11 source and 15 installed checks; zero failed/skipped cases. [Run 46081652369](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46081652369). | Exact pinned classifier and both NumPy environments, real OCR dependencies and complete package binding; all-language image/recognition quality is separate. |
| 06c70bde0d7e388984880557aab6494b0377a3ed | Complete-scope search common regressions: Windows 905 source + 905 installed, Linux 907 + 907, zero failed/skipped cases. [Run 46082271279](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46082271279). | Source scope, cursors, retained quotation authority and platform races; later real-model/native component is separate. |
| 3be09c364abae9c43e6fe24b34ba129e5664fde5 | Real MiniLM search component: Windows 241 source + 241 installed cases, including the actual 607-source regression; 365 native checks. [Run 46084207789](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46084207789). | The selected pinned model executes; this does not establish 50k-book throughput or every language's retrieval quality. |
| 76f776177c4fe9a8efdb74a6c2e1ab2c44e9b155 | Local translation: each OS passed 556 source + 556 installed common cases and 21 source + 21 installed actual HTTP/TLS cases; Windows native checks passed 115 in each context. [Run 46086508668](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46086508668). | Real socket/TLS, withheld headers/handshake, body cancellation, Unicode and owner lifetime; local protocol fixtures provide no linguistic quality claim. |
| 90ea2ede28fad196a7ecaf8e31c72d0b1ea546be | Playback queue: Windows 575 source + 575 installed, Linux 585 + 585, and 154 native checks; zero failed/skipped cases. [Run 46089506019](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46089506019). | The unchanged native probe now passes genuine nested-modal teardown, late callbacks and committed-result preservation. Hosted runner output inventory is zero; physical output and NVDA remain NOT_RUN. |
| 9dc56d6cd1db2d050be244c5eb81fd795bfeefef | Runtime corrections: each OS passed 140 source + 140 installed cases, zero failed/skipped cases. [Run 46085152514](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46085152514). | Bounded Windows job enumeration and durable idle-refresh backoff with explicit manual/due paths; the final full installer remains a separate gate. |
| bc195d800d9ffc5846cef37fac81e94e3e0a69b0 | Import, catalog and retrieval contracts: Windows 641 source + 641 installed cases; Linux 648 + 648, zero failures/errors/skips/xfails. [Run 46094685065](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46094685065). | Actual receipt-aware publication, fresh session accounting, POSIX directory changes and Windows scratch disappearance regressions; 393 package files and 36 module origins remain exact before and after each context. |
| Same bc195 revision | Actual 1,000 books × 40 segments completed on both OS, including real first-commit process interruption, explicit resume, every catalog/source row, complete literal traversal, exact Unicode copies, 21-library navigation and first/reopened BM25. [Run 46094685110](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46094685110). | Measured import/search seconds: Linux 92.936/102.586; Windows 193.363/178.964. These unprofiled measurements qualify this component and size only; projections are not larger-tier results. |
| 1d85bae4c35bd7fedf385b3eeafc9519d770b0c5 | Full-backup scale adapter: each OS passed 17 source + 17 installed actual cases, zero failures/errors/skips/xfails. [Run 46095579146](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46095579146). | Real public import, common4 create/verify/fresh restore and full catalog/raw/original equality, plus resource refusal, tamper, cancellation and extra-restored-data controls. Exact 405-file inventory, 45 module origins and three scripts; actual 50k/2GiB runs remain separate. |
| 734962a62f4efa329f04d8c2f69ae20057ca6114 | Explicit search-memory admission and backup benchmark contracts: 62 source + 62 isolated installed cases on each OS, zero failures/errors/skips/xfails. [Run 46099085535](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46099085535). | The saved, admitted and reported working allowance is bound to the actual profile. It is separate from process RSS and does not establish a large-corpus throughput result. |
| 25518b4c49c8fa55c3ad4f45a178403e733609dd | All six uninstrumented component scenarios completed: 1,000 books × 40 segments and exact 65 MiB / 129 MiB text sources on Linux and Windows. Real interruption/resume, every catalog row, full source traversal, 50 scoped queries, quotations, 21-library navigation and first/reopened complete ranking passed. [Run 46101042884](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46101042884). | These exact component texts precede the scoped-SQL changes and differ from the final matrix's non-round byte lengths. Their PASS does not qualify the later composition, 50k books or 2 GiB. Large-source working allowance was 1 GiB; actual individual-process peaks are recorded separately. |
| 3bd0453809dbade69dbc6131079bdb5e79dc733a | Actual public 100-book and 1,000-book corpora passed read-only old/new SQL row and order comparisons, foreign-scope refusal, exact replacement-query plans and bounded SQLite VM observations. [Run 46101997783](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46101997783). | This diagnoses the existing scans and the proposed keyed queries. The patched production source still receives its own functional, installed and full-capacity verdicts; diagnostic timings are not release throughput measurements. |
| fb90e7797b90339561d45982e256a6f601b4489f | Follow-up actual 100-book and 1,000-book corpora passed all 24 read-only SQL pairs per corpus, including the exact derived bounded-fetch and current-original query forms, foreign scope and empty-ID cases. [Run 46105648861](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46105648861). | Nonempty fetches use at most 100 page keys and up to 500 selected edition alternatives. The current-original scalar took 57 VM steps at both sizes. The wide 500-edition case is bounded but costlier than its old form at 1,000 books; no universal speedup or final runtime PASS is claimed. |
| 6832acef500a7a2c6a869778a6c958cba0a64d34 | Search/source/storage component: 1,049 source + 1,049 isolated installed cases on each OS, 52 suites, 20 named origins and the same 406-file package inventory in all four contexts; zero failures/errors/skips/xfails. [Run 46103083136](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46103083136). | Includes the streamed original proofs, final multi-source stamp check, Unicode mapper and the first three scoped SQL corrections. Later bounded-fetch changes receive their own exact-source gates. |
| 60f01a5630bdc4127cfc60bdf26474d527ba1744 | Actual 10,000 books / 400,000 segments completed on both OS after the first three scoped SQL corrections. Genuine first-commit interruption, same-queue resume, complete catalog/source verification, 4,000 literal windows, 50 scoped queries, exact copies, 21-library navigation and both complete-scope ranking calls passed. [Run 46103586773](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46103586773). | Measured import/retrieval seconds: Linux 1,868.085 / 668.823; Windows 2,015.626 / 558.904. All ten chronological import slices and their overlapping child times are retained. These exact-head results do not qualify 50k, the later bounded-fetch changes or the final twelve-tier matrix. |
| 6d30844bc79f951fc3387a29b2d753370c8950bb | Linux completed the actual 536,871,085-byte / 250-segment source, full interruption/resume, catalog and retrieval protocol. Import took 59.231 seconds and retrieval 1,321.129 seconds. [Run 46104136822](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46104136822). | The same-head Windows job failed its selected temporary-output quota during the first import: zero complete, one failed. No Windows resume/catalog/search result exists for this run. The cause and later correction require their own observed evidence; this is not a 2 GiB or final-release PASS. |
| 13a61cd6a0236de20d891552937369770179f414 | Installed native Windows import/backup scenario: 966 checks, plus 30 independently attempted command-palette checks. The real flow completed three imports, full backup, explicit checksum/base-chain incremental creation, verification and fresh restore with original Unicode quotations. [Run 46104158148](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46104158148). | Exact 404-file / 48-origin inventories and three script/helper hashes matched before and after both probes. Folder-picker answers and one post-snapshot hold are controlled; physical/NVDA and native folder-picker acceptance remain separate. The seven common menu-binding cases are selected by the final common gates. |

The earlier integrated 96cf8a1435187844393b5389001d07a587c417a8
runs remain failed: Windows source exposed refresh, firewall-recovery and job
enumeration defects, and both installed native paths hit the palette's wx text
type error. The corrected palette, exact owned firewall recovery and the
component-qualified runtime fixes are included in the current source; only the
new complete integration run can qualify that union.

## Executed predecessor evidence

| Exact revision | Scope | Result |
| --- | --- | --- |
| `96294e97498f2133bf93f02ce72f84b6e709c72b` | Portable source integrity, actual declared dependencies | 5001 passed plus 14 subtests; zero failures and skips; [run45969316754](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45969316754) |
| Same96294e revision | MSVC and pinned Zig launchers executed on native Windows | Both jobs passed; [run45969316748](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45969316748) |
| Same96294e revision | Full Windows integrated desktop and installed bundle | Reader/bookmark and DAISY URI acceptance failed; integrated source exposed the isolated fixture import and temporary-report cleanup failures before exceeding its 20-minute bound. No complete Windows PASS is claimed; [run45969316892](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45969316892) |
| `d94e8800261f74c238fc8a4ff6684f84f8040b00` | Real-file export publication, approval, cancellation, full-content hash and native Windows ChangeTime | 138 passed on Linux and 138 passed on Windows, zero failures/skips; [run45974691968](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45974691968) |
| `42eb288fcebb39e36eb374f338b2fb159ccd5a77` | Pinned citation adaptation, unchanged seven-style oracles, original/adapted sorting and historical-receipt regeneration on Windows | 64 passed, zero failures/skips; [run45988886603](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45988886603). This component result does not qualify the full composition. |
| `9e7cb2ac5a58a7cf9ff3bedb6270b86300eb345d` | Earlier complete portable source selection | 7011 passed and 65 failed, with 17 passed subtests; [run45984540473](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45984540473). Its fixture/observer corrections are included in the subsequent composition; this failed run remains failed. |
| `7530a3632c2d57f76baee5ebf17de818a8f7c9ab` | Bounded dictionary import, existing review/handler/worker boundaries and 49 captured author-tool observations | Source 159 and isolated installed 159 passed, zero failures/skips; [run46028769389](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46028769389). The extended native dialog and final union require their own gates. |
| `4087df8df6da95543f5cd2bf41182c4cba151047` | Schema3 clips, real FFmpeg rendering, current installed source and native keyboard/lifetime controls | Linux and Windows each passed 292 source and 292 isolated installed cases, zero failures/skips; Windows also passed 236 native checks. [run46041819555](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46041819555). Preview transport was explicitly synthetic; later independent playback and the final composition require their own gates. |
| `1d001b53bc8c318deb3f47fb37971a1a9f79b96e` | Native ASR window/timer ownership, shared Tasks executor, controlled EPUB reading and pronunciation review | 108 source cases, 56 platform checks, 52 controlled EPUB checks and 136 pronunciation checks passed on Windows; [run46047174783](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46047174783). Actual SAPI generated measured non-silent PCM. Output-device inventory remained empty; physical playback, human NVDA and recognition-model inference remain NOT_RUN. |
| `4b951b59299bcec893ed39d559bf150e609c9349` | Independent listening controls, current source/installed modules, real FFmpeg and native Windows control/PCM/lifetime checks | Linux passed 451 source and 451 installed cases; Windows passed 449 source and 449 installed cases plus 101 native checks, all with zero failures/skips. [run46050382435](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46050382435). Native transport is explicitly controlled; actual inventory still reported no output device, so physical output and human NVDA remain NOT_RUN. |
| `47b5f544ad3e53d7cea29ef99e47d0a6a39e3c16` | Verified signed-download Pause/Resume/Cancel, actual TLS and native Windows lifetime controls | Each OS passed 119 source and 119 installed cases; Windows also passed 39 native checks, with zero failures/skips. [run46052086116](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46052086116). Signing authority is synthetic; actual installation remains its own gate and physical keyboard/NVDA remain NOT_RUN. |
| `50951f2bc416d4856942529587cddf2ddb29667f` | Retained process ownership, ASR admission, exports and exact-suite CI deadlines | Each OS passed 479 source and 479 installed cases plus 34 separate CI-runner tests, with zero failures/errors/skips; Windows also passed the actual profiled ASR case. [run46054991722](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46054991722). The source callback-lock regression and all 41 admission cases passed. Hosted timings do not establish recognition throughput, and the final composition requires its own gates. |
| `0070a28ccf68250633dca30d226bdef2476af3a9` | Full portable source selection | 9410 passed, 24 failed and 42 subtests; [run46057432268](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46057432268). The failures comprise 18 recording cascade cases, one POSIX playback case, two dictionary import cases and three Calibre child-owner cases. This full run remains failed. |
| `409cc2af6f03e6f8b64ffec315c719b6e70da535` | Focused recording ownership and ordinary child-import corrections | Linux passed 162 source and 162 isolated installed cases; Windows passed 159 source and 159 installed cases, with zero failures/skips/expected failures; [run46061911299](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46061911299). All four runs verified the same complete 382-file Python inventory and 13 selected module origins before and after execution. |
| `0070a28ccf68250633dca30d226bdef2476af3a9` | Main Windows bundle and full source selection | Real standard install, repair, uninstall and failed-bootstrap preservation passed. The signed-update probe then failed Python parsing at line333 with `SyntaxError: positional argument follows keyword argument`; it and the downstream bundle gates did not execute. The source step completed 394 of 450 suites, reporting 10,144 JUnit passes with zero failed/skipped cases, before its 40-minute limit; 56 suites lacked completed verdicts. Admission only collected at 38m54s. This run is failed and incomplete; [run46057432160](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46057432160). |
| Same0070 revision | Windows V3 installed, broad and later strict selections | The installed step collected 8414 cases and recorded 6779 PASS outcomes, zero observed failures/errors/skips, before its 50-minute timeout; all 41 admission cases passed, but the gate is incomplete. The broad step recorded 4734 passed, 27 failed and 13 skipped. Progress step12 passed 278 cases in 10 suites; publication step14 passed 404 in 13. Shared step15 was cancelled on normal corrected-head supersession after 595 passes in 30 completed suites and remains incomplete. Native steps9–11 were NOT_RUN. These overlapping selections do not establish a full V3 PASS; [run46057432268](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46057432268). |
| `4c25f0be2adf093a5f2ceaaa800cdb19707d9bd0` | Syntax-only qualification of the corrected update probe and selected Python sources | Python3.11.18 compiled all 469 files under application source, scripts and packaging with zero syntax failures; [run46065991216](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46065991216). No probe import, application/native execution or bytecode write occurred. The temporary syntax workflow is not included in the product composition. |
| Current committed composition | All new seams together, source and installed origins, fresh native bundle | Require the gate inventory above for this exact commit. Predecessor counts cannot be reassigned. |

The recording cascade began when a reservation callback waited for public
completion from inside the callback drain itself. Its release now binds the exact
native-confirmed current owner; public `close()` still requires owner quiescence,
and a superseded callback cannot release a newer capture. The playback fixture
now checks silent paused seeking and starts a replacement child only on explicit
Resume. Dictionary and Calibre fixtures preserve ordinary child imports while
requiring the selected parent package or the current interpreter's byte-identical
installation, including the complete relative Python-file inventory. The focused
run qualifies these corrections in both import contexts; the next coherent PR
head still requires the complete source, installed and native gate matrix.

The signed-update probe correction (`6db4ab25`) restores the missing
`sha256=` configuration field without changing its installer/update assertions.
The main workflow correction (`05141c6e`) compiles the exact probe bytes before
bundle assembly. Syntax qualification does not replace actual signed-update
execution. The Windows V3 broad failures comprise 18 recording cases plus one
Clip case blocked by the same retained capture, five POSIX-only cases collected
on Windows, and three child-import identity cases. POSIX routing now excludes
the five POSIX files from that Windows command while retaining every one in the
unchanged explicit Linux selection; no required Linux test is removed. The
13 reported broad-stage skips remain recorded, with individual attribution still
pending. The corrected coherent head requires its own complete runtime verdict.

Office execution at `0070a28ccf68250633dca30d226bdef2476af3a9`
([run46057429553](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46057429553))
passed all nine source and installed native cases on Windows. After an initial
TLS provisioning failure, Linux's second attempt passed all 25 installed cases,
including all nine native cases, but its source-native selection retained one
DOC failure: the isolated worker reported a regular-local-file rejection
without identifying the rejected object or its type. This result remains failed.

The separate diagnostic revision `4311014d949be4f832c5d62e247a9a0c7c3c5925`
([run46059804621](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46059804621))
passed the same source/installed native selections and 12 independent real DOC
conversions plus two original-stat controls. The refusal did not reproduce.
No native rejected type or causal fix is claimed, no file/link guard was relaxed,
and the diagnostic instrumentation is not part of the product composition.
The next coherent PR commit still requires all nine source and installed native
cases on both operating systems; these diagnostic passes cannot replace that gate.

The earlier export attempt `c27b8b8` passed Linux 138 but failed 38 Windows
cases because its real-descriptor observer used the unavailable
`os.O_ACCMODE` constant. The d94e correction uses the exposed write-mode bits;
the actual production reads and assertions remain in place. Six additional
canonical bibliography CLI/workbench cases are restored for the complete
candidate and require its own run.

Each CI evidence artifact identifies the exact source commit and dependency
closure. Installed tests resolve actual product modules from the installed
environment and compare their bytes with the sealed application wheel. The
Windows workflow preserves native controls, source-opening events, fresh EXE
bootstrap, normal shutdown, restart, standard installation/repair/uninstall and
the final unchanged-bundle seal check. Failed or skipped steps are not evidence
of success. Human NVDA, listening and physical devices remain separate.

The focused correction commit `1dd1c6b3611aa21c5463d251257542b7eeeece36`
has now passed its Windows export, real descendant cleanup, isolated EPUB batch
and Reader constructor regressions, followed by the actual installed full-source
reader/bookmark native probe. Its Linux run and the complete canonical
composition remain separate gates. The earlier V3 installed recovery timed out
at 70% of its 25-minute run without a recorded test failure; its incomplete job
was cancelled by the 75-minute job bound. Expanded bounds preserve every required
suite and verdict rather than treating unfinished work as passed.

## Backup and import authority

The current writer emits common format 4. Supported historical formats 1–5 readers
are strict, read-only decoders with their original envelope and typed-producer
rules. Literal prior-writer fixtures verify compatibility independently of the
new writer. A backup includes only declared typed state and retained inputs,
with size/hash, path, source and publication checks. Optional explicit budgets
bound streamed original copying, SQLite snapshot progress, journal fields,
metadata memory, staging and cooperative time without rewriting archive authority.
Incremental copies first use that same complete snapshot path, select only
changed retained bytes, and bind an explicit ordered chain of exact parent
manifest hashes. Verification reconstructs a private complete snapshot and runs
the unchanged full checker before create-only restore. No archive grants a new
resource budget, reactivates a provider approval or starts playback.
Unreadable inventory
subtrees fail visibly; they are not treated as absent. Generated audio is an
explicit choice, and external exports need their own copies.

Catalog schema 5 is independent of the archive format. It keeps the canonical
immutable `import_publication` receipt layout. The incompatible pre-release
`publication_receipt` dialect is rejected without rewriting the database.
A worker freezes its launch identity before resource admission and rechecks the
retained input before publishing. Recovery requires the actual matching
request/attempt receipt. A similar existing revision or a timing watermark
does not prove completion.

Restore creates a new folder and never resumes work automatically. Old
path-bound attempt hints are discarded; explicit resume validates the source
and obtains a new attempt. Reader projects preserve their supported source
dialects, history and detached drafts without inventing quotation authority.
Saved bookmarks keep their exact library/edition/revision/asset/text identities.

## Publication, cancellation and resource ownership

All research exports use captured destination approval, a bounded full-content
fingerprint and fresh scope checks before publication. Existing files over
32 MiB, or files whose inspection exceeds the checking budget, require a new
name. These are replacement-inspection bounds, not limits on new export sizes
or guarantees about filesystem latency. Text and binary publication retain
actual no-clobber and native ChangeTime checks.

Studio and transcription preserve verified committed output when later cleanup
or a directory durability operation reports an error. They do not turn corrupt,
missing or foreign equal-byte replacements into successful output. Cancellation
before publication remains effective. An operation that has already committed
reports its saved result accurately.

Shared admission accounts for cooperating CPU, memory, disk and exclusive
devices. Studio also checks retained logical bytes, destination capacity and
incremental inventory bounds. These declarations do not claim system-wide hard
RAM limits. A child, playback device or provider that has not confirmed closure
retains its owner, files, lock and lease for retry. Progress observer failures
cannot reverse business completion or replace an output location with a
misleading operation failure.

## Audio and citation packaging

The main transferable bundle is audio-free. `SetupLocalAudio.cmd` first shows a
plan and prepares the pinned public FFmpeg inputs only after explicit
confirmation, into a new folder outside the application. The prepared runtime
is not a redistribution clearance. CI attaches it to a separate fresh bundle
copy for real runtime verification under the existing egress rules; the
original transferable bundle remains independently checked.

Current bibliography and historical document regeneration use the same
installed QuickJS executor with CPU, memory, stack and input/output bounds.
Historical processor, style and locale bytes remain exact for document
regeneration; no stored URL or path is followed. New documents also record the
separately pinned three-edit sort-macro runtime adaptation, its upstream revision,
hashes and notice. Historical receipts remain accepted by their exact grammar;
a new regeneration records the current adaptation explicitly. The primary bundle
does not need Node. Exact source, dependency wheels, full notices and the
version-aligned base and semantic SBOMs remain part of the delivery closure. Checksum integrity
does not establish a publisher signature.

Measured M4B chapters and LRC use actual verified output timing. Synchronized
EPUB3 and DAISY2.02 publication share the existing typed Studio receipts and
atomic publisher. Pinned EPUBCheck and offline DAISY DTD tests provide structural
evidence. They do not certify every reader, word alignment, Braille or acoustic
quality. Long 1/3/10-hour streaming fixtures do not establish equally long
uninterrupted neural inference.

## Qualification and owner dependencies

The following cannot be labelled DONE by passing the current core tests:

- The required exact-commit installer/uninstall/repair gate must pass in the
  attached run. A signed ResearchLibrary.exe/installer, owner-configured publisher
  trust and distribution review for the selected profile remain additional
  conditions even after that automated gate passes.
- Owner provisioning and qualification of the actual private entitlement/update
  authority, production trust, endpoints and protected credentials. The public
  account-optional/perpetual, signed refresh/offline renewal and signed update
  clients are integrated; their synthetic policies and actual native update gates
  do not establish a deployed production service or cross-version migration.
- Actual owner-supplied cloning runtime, authorized reference recordings,
  protected voice profiles and independent multilingual inference/listening.
- Physical microphone/output monitoring latency and listening, verified
  legacy/SAPI4 combinations and live reader interoperability. Source integration
  of dry monitoring, reviewed workflow templates and selected legacy converters
  is distinct from those device/runtime qualifications. Supported dictionary grammar and per-format limits remain explicit in the
  finite reading checklist and their dedicated format guides.
- Actual installed dordiai-voices and MusicGen/ACE-Step model inference, the
  explicitly limited scenarios in the [finite reading checklist](READING_WORKFLOW_CHECKLIST.md),
  chaptered-audio device/interoperability qualification and optional Braille workflows.
- Independent qualification of every required capability for the canonical 135
  entries; registry/resource counts never establish language quality.
- Human Windows 11/NVDA and listening review. Automated Windows Server native
  execution is useful evidence, but does not substitute for that review.

## Supplied transcription and private components

The supplied Whisper Transcribe V4.3 worker has a reviewed source preparation
and an exercised synthetic process/protocol boundary. The public
`transcription.service` coordinator dispatches the canonical profile/UUID and
historical hash64 jobs by exact paths and typed records. Its historical adapter
delegates to that one current service, isolated process and retained lease owner.
Native file/folder selection, saved jobs, pause/cancel/resume, separate transcript
editing and typed recovery remain part of the public implementation. Real
recognition quality, throughput and a verified owner runtime/model installation
require their own evidence. Diarization and word alignment are not inferred
from ordinary segment output.

The public dordiai bridge remains an adapter to an explicitly selected local
installation. Restored declarations stay inert, including legacy declarations.
No private worker/model bytes, recordings, credentials or voice profiles are
copied into this repository. Public entitlement/update clients likewise require
owner-configured authority, endpoints, trust roots and signing material before
production use.

## Guides and project tracking

Use [the Ukrainian quick start](START_V3_UK.txt),
[document boundaries](DOCUMENT_FORMATS_V3.md),
[academic search](ACADEMIC_SEARCH.md),
[bibliography](V3_BIBLIOGRAPHY.md),
[historical DOCX provenance](academic_integration.md),
[keyboard profiles](KEYBOARD_SHORTCUTS.md),
[reading workflow checklist](READING_WORKFLOW_CHECKLIST.md) and
[transcription source review](TRANSCRIPTION_INTEGRATION_REVIEW.md),
[retained document batches](DOCUMENT_BATCHES.md), [effect presets](ORDERED_AUDIO_EFFECTS.md),
[playback outputs](OUTPUT_DEVICES.md), [session keys/tray](READ12_SESSION_CONTROLS.md),
[workflow templates](WORKFLOW_TEMPLATES.md), [ordered translation batches](TRANSLATION_BATCH_UK.md),
[selected legacy documents](LEGACY_DOCUMENT_READING.md), [Calibre reading](CALIBRE_READING.md)
and [signed maintenance](SIGNED_UPDATE_MAINTENANCE.md),
[independent listening](INDEPENDENT_PLAYBACK.md), [typed local tools](ASSISTANT_LOCAL_TOOLS.md)
and [verified download continuation](VERIFIED_UPDATE_RESUME.md),
[streamed import and continuation](IMPORT_CENTER.md), [automatic OCR](AUTOMATIC_OCR_UK.md),
[complete-scope search](SEARCH_PAGING_AND_RESOURCES.md), [local translation](LOCAL_TRANSLATION_UK.md),
[playback queues](PLAYBACK_QUEUE.md), [backup resource controls](BACKUP_RESOURCES_AND_RECOVERY.md)
and [actual library scale](LARGE_LIBRARY_SCALE.md).
The exact published source and checks are tracked in
[PR143](https://github.com/Oleksii-debug/AudioTacticalFPS/pull/143).

Every deployment profile keeps access to user-owned data. This implementation
does not publish private content, acquire paid services, sign an application
with an invented identity or authorize external distribution. See the actual
candidate's reports before treating any source-reviewed addition as executed
or qualified.

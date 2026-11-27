# Finite V3 implementation and qualification audit

## Source and evidence boundary

This audit was reconciled on 2026-11-27 against published source
`8911b2ae232b4786677890c40560f4393a0202a9` and the source-only integration
`fa79b549d5637801f02db0dbced61dda38ee4495`. The latter adds the managed
playback queue, publication cleanup receipts and a complete-library
backup/restore phase to the capacity harness. Follow-up `910a80ff` seals
owned queue staging bytes; `8697ce93` corrects the historical ASR source
dispatch and export expectations described below. These source corrections
are **NOT_RUN** until their exact composed source passes its required jobs.

The authorities are [the current finalization scope](UNIVERSAL_SCOPE_V3_FINALIZATION.md)
and [the audio scope](UNIVERSAL_SCOPE_V3_AUDIO_AGENT.md). The initial
`7e830912` audit and older “remaining work” paragraphs are historical
observations. They must not override implemented scheduling, named templates,
book batches, structured translation, selected Calibre adapters or the
executed retained-corpus results recorded here.

Source implementation, an executed synthetic contract, native execution,
model/language quality, manual accessibility and distribution clearance are
independent dimensions. A later source commit does not inherit a previous
installed binary's passing result. A queued workflow is not execution evidence.

## Finite closure work using the current implementation

| Scope | Current implementation | Concrete remaining work or acceptance |
|---|---|---|
| V3-01: document inputs | The direct readers, selected Pandoc/raster adapters, five selected LibreOffice suffixes and all eight selected Calibre suffixes below are implemented. They use the existing owned process, resource, review and provenance contracts. | Execute each selected runtime against real unprotected fixtures on the declared Windows profile. The selected development Calc host failed its XLS memory boundary; this does not establish that XLS is globally unsupported. Adapter presence and a synthetic child test do not qualify an actual converter. |
| V3-02: text preparation | Source-bound editing/history, optional Hunspell-compatible spelling review, scoped pronunciation and reversible finite Ukrainian/English speech preparation are implemented. Number/date/unit/abbreviation/math/symbol and Markdown read/skip transformations use Preview, Apply and Undo. | Execute the current spelling/normalization gates and qualify each supported locale/rule set. Other locales retain verbatim text. Learned punctuation/stress/prosody restoration and unfamiliar foreign names need appropriate model and language evidence; finite substitutions are not that evidence. |
| V3-03: reading and presentation | Exact-source live reading, font/colour/zoom, exact-range pacing and independent playback speed/pitch/volume/balance are implemented in the existing readers/player. Native live, recorded and Studio controls passed on earlier exact sources. | Repeat current installed/native ownership and source-revocation tests, then manual keyboard/NVDA and physical-device checks. SAPI4 remains explicitly unavailable in the current engine registry until a separately validated 32-bit helper/runtime combination exists. |
| V3-04/05: audio production | Current Studio has role/master chain edit, reorder, bypass, removal, reusable presets and master dry/wet; clip copy/cut/paste, split, join and different-source consolidation; segmented recording with pause/resume and dry-input monitoring; ten-format media conversion and explicit video-stream selection. | Run the exact composed source/installed/native suites, including new queue stage ownership and post-publication cleanup faults. Verify real recording devices, latency, monitoring feedback and listening quality separately from synthetic FFmpeg sources. Pre-master stems and exact processed-master chapter slices remain distinct. |
| V3-04/05: books and publication | Ordered multi-source book batches, reviewable naming/presets/splits, current-service CLI, metadata and measured PCM-frame publication are implemented. Measured parts use exact frames and retain approximate whole-line text scope where word alignment is unavailable. | Current Book native construction and immutable Effect fixture corrections need their next exact run. Verify batch cancellation/resume, retained-source restore/export and the selected voice/runtime. No model-generated speech quality or forced word alignment is inferred from a frame map. |
| V3-06: recovery and automation | Complete typed snapshots, retained pending sources, current Vault incremental snapshots, explicit foreground scheduling and inert named workflow templates exist. `fa79` adds the managed Studio queue and its typed backup through the same owners. | Qualify the current full/incremental backup, queue restore and large-corpus complete cycle. Finish the separately reviewed legacy queue import integration and its project-ZIP exact-set checks. Restored jobs/templates/queues do not inherit execution approval, devices or automatic playback. |
| V3-07: large libraries and sources | Search/federation and folder scanning have bounded continuation. Explicit large TXT/PDF profiles, typed pending recovery and a larger selected backup capacity profile are implemented. Actual 1k, 10k and 50k retained-book runs passed at `431ac087`. | Execute the newer complete-backup cycle, declared large-source fixtures and exact current-head regressions. Read the bounded measurement JSON before reporting P50/P95 or RAM/disk values. Fixture construction is not public import throughput; million-segment traversal is not semantic model quality. |
| V3-09: assistant operations | Seven additional typed tools now cover reader pages, notebook evidence, conversion, voice listing, audio planning/effect proposals and PCM inspection. Current explicit approval, source binding, journal and shared broker remain authoritative. Named templates rebind through a fresh Preview; scheduled jobs need an explicitly armed foreground window. | Do not describe inspection/proposals as arbitrary control of a live device or UI. Current `player.inspect` does not start playback; `audio.plan` does not render. Reverify completed scheduled jobs without replay, qualify actual selected local-model planning and measure engine-session costs before claiming large-book throughput. |
| V3-20: translation and ASR | Structured document/chapter/range/batch translation is integrated with official Google Basic v2/Advanced v3 and an explicit local LibreTranslate transport. Current supplied-worker ASR has preparation, managed jobs, receipts, cancel/resume, CLI/export and inert canonical recovery. | Execute the exact current provider/CLI/native/backup gates. Real account-authorized provider calls, the actual selected local translator/model and actual supplied Whisper inference need their own evidence. Legacy ASR bytes must remain recoverable even when their extension is not proof of decodable PCM. |
| V3-20: synchronized publishing | Current EPUB3/DAISY producers, synchronized reading and exact-publication resume are implemented. Independent EPUBCheck and DAISY DTD checks actually passed at `819d6bd9`. | Repeat them for the release source and verify real reader interoperability, manual navigation and accessibility. DAISY DTD validity is not full DAISY rule conformance; EPUBCheck is not WCAG/NVDA acceptance. |

Implementation references: [large sources](LARGE_SOURCE_PROFILES.md),
[book batches](BOOK_AUDIO_BATCHES.md),
[measured publication/CLI](MEASURED_BOOK_PUBLICATION_AND_CLI.md),
[typed tools](TYPED_LOCAL_ASSISTANT_TOOLS.md),
[foreground scheduling](LOCAL_ASSISTANT_SCHEDULING.md),
[named templates](LOCAL_ASSISTANT_WORKFLOW_TEMPLATES.md),
[structured translation](STRUCTURED_TRANSLATION.md),
[local translation](LOCAL_TRANSLATION_ADAPTER.md),
[media conversion](MEDIA_CONVERSION.md),
[managed queue](MANAGED_PLAYBACK_QUEUE.md),
[spelling](SPELLING_REVIEW.md) and
[speech preparation](SPEECH_PREPARATION.md).

## Named input format inventory

The current direct registry supports TXT, PDF, DOCX, EPUB, HTML/HTM/XHTML,
Markdown, EML, FB2/FB3, ODT/ODP/ODS, PPTX and XLSX. Explicitly selected Pandoc
adds RTF; explicit OCR configuration adds supported raster/TIFF files.
The authoritative suffix dispatch is
[`conversion/capabilities.py`](../src/carr_library/conversion/capabilities.py).

| Legacy suffix | Selected adapter now present | Qualification boundary |
|---|---|---|
| `.doc`, `.ppt` | Fixed LibreOffice import filters and the existing bounded owned conversion path. | Real selected Linux fixtures passed; current composed/native Windows acceptance remains required. |
| `.xls` | Selected fixed Calc filter, current isolated profile and unchanged resource limits. | The selected LibreOfficeDev 26.8 host failed within its 3 GiB boundary. A successful supported-runtime fixture is still needed; no global unsupported-format claim. |
| `.wpd` | Selected WordPerfect header/filter adapter. | A real selected Linux fixture passed; historical variants and current Windows runtime require their own cases. |
| `.pdb` | Explicit PalmDoc TEXt/REAd header/filter adapter. | A real selected Linux fixture passed. Other Palm database types are not silently treated as text. |
| `.azw`, `.azw3`, `.lit`, `.mobi`, `.tcr` | Selected self-contained `ebook-convert` through the integrated Calibre adapter. | Actual runtime/filter and real unprotected files need selected-runtime acceptance. No DRM/password bypass. |
| `.prc` | The same selected Calibre adapter, restricted to Mobipocket/PalmDoc input. | An arbitrary Palm database is not admitted as a book. Real supported fixtures and runtime remain required. |
| `.djvu` | The selected Calibre adapter reads an existing text layer. | Image-only DjVu is not silently OCRed. Real text-layer fixtures and selected-runtime execution remain required. |
| `.chm` | The selected Calibre adapter with the same fixed plaintext output and staged inventory bounds. | Real CHM fixtures and runtime qualification remain required; no HTML application, recipe or plugin execution is authorized. |

The eight Calibre suffixes are therefore **implemented adapter paths**, not
absent-tool work: `.azw .azw3 .chm .djvu .lit .mobi .prc .tcr`.
[`conversion/calibre.py`](../src/carr_library/conversion/calibre.py) captures
the chosen launcher/frozen core/Python-library identity and retains current
native readers until the existing supervisor settles the whole process family.
Its private profile/config/cache/temp and fixed command do not establish a
complete operating-system sandbox or a seal of every system library.

The earlier observed LibreOfficeDev build was `26.8.0.0.alpha0`,
revision `2c87e51eeaa2b413ff4ae097b2705eea1995d8e5`.
Its filter inventory and four successful format fixtures are host-specific
evidence. The adapter's 176 portable plus six selected-host cases do not
qualify XLS or a different Windows installation. Current selected Calibre
controls and process ownership passed 59 and three cases respectively in the
819 Windows run; those cases do not claim eight real conversion executions.

The 17-wheel core and 53-wheel semantic locks remain unchanged by spelling.
Hunspell-compatible review uses a separately selected exact repacked code-only
Spylls component before bundle sealing; dictionaries remain explicit inputs.
It is not evidence that a native system library or every language dictionary
was installed or tested. Reuse and redistribution decisions remain separate:
[reuse ledger](REUSE_DECISIONS_V2.md),
[extractor decisions](DOCUMENT_EXTRACTOR_REUSE.md).

## Implemented large-source and automation limits

Defaults remain bounded. The ordinary source limit is 64 MiB and the earlier
streamed UTF-8 profile is 128 MiB. The explicit large TXT profile admits up to
4 GiB with at most 65,536 source fragments of 65,536 Unicode characters.
The explicit file-backed PDF profile admits up to 4 GiB while retaining the
1,000-physical-page limit, 16 MiB per page checkpoint and 256 MiB aggregate
checkpoint bound. The larger worker deadline is explicitly 7,200 seconds,
not an unbounded change to every import.

The current pypdf/PDFium, page OCR, original-byte reader, import journal and
catalog publisher remain the implementation. The large-source acceptance
fixture includes a PDF with a large unused stream; it tests retained original
size, not thousands of OCR pages or arbitrary complex-PDF quality. Optional
2 GiB fixtures and peak child memory/VRAM still require recorded execution.

The explicit `large-source-v1` backup profile uses complete manifest v6 and
pending component v4. It declares 64 GiB catalog, 32 GiB journal, 128 GiB
originals, 64 GiB pending bytes, a 64 MiB manifest, 100,000 originals and
1,048,576 aggregate checkpoints. Ordinary 512 MiB database/journal and 8 GiB
original budgets, and independent typed sidecar limits, remain unchanged.
A raised allowance is not a capacity result.
See [the source/profile contract](LARGE_SOURCE_PROFILES.md).

The assistant's current additions are exactly `reader.page`,
`evidence.search`, `document.convert`, `voice.list`, `audio.plan`,
`audio.effects` and `player.inspect`. They use current typed services;
a saved plan cannot supply arbitrary executables, new providers or devices.
Read-only reader pages do not gain quotation authority. Evidence IDs remain
bound to their saved notebook entry and source. Effects produce a reviewed
source-preserving draft, not an implicit playback action.

Foreground scheduling accepts finite saved jobs and UTC due slots, requires
explicit arming in the current window and runs one job at a time. It is not an
OS daemon or an automatically recurring task. Closing/revoking the scope
disarms it. Named templates preserve permitted steps as inert data and require
current folder/library selection, fresh Preview and approval after load/import/
restore. The source scopes are documented in
[scheduling](LOCAL_ASSISTANT_SCHEDULING.md) and
[templates](LOCAL_ASSISTANT_WORKFLOW_TEMPLATES.md).

## Actual retained-corpus evidence

The three runs below use source
`431ac08712b4c31c70a6253711481c02d092de94`, Linux CPython 3.11.18,
actual retained originals, catalog editions and SQLite source segments.
All completed logs were read; the 50k terminal PASS appeared at
2026-11-27T14:12:02.446Z.

| Retained books | Actual source segments | Exact query hits traversed | Page requests including terminal probe | Actual job / verdict |
|---:|---:|---:|---:|---|
| 1,000 | 20,000 | 1,000 | 41 | [140527316366](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46004721023/job/140527316366) — PASS |
| 10,000 | 200,000 | 10,000 | 401 | [140527316279](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46004721023/job/140527316279) — PASS |
| 50,000 | 1,000,000 | 50,000 | 2,001 | [140527316349](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46004721023/job/140527316349) — PASS |

The stand includes exact full-result traversal, source Copy/ReadingDesk/open
checks, a resumed cursor, 12 public supervised import samples and actual owned
worker cancellation/recovery. The 50k fixture setup took 262.983 seconds using
batched internal fixture registration. That number is **not** public import
throughput. Fresh startup is a new isolated process; OS disk cache was not
evicted. The Python socket guard is not an operating-system/native-child
egress qualification.

Bounded report/log artifacts, without corpus bytes:

- [1k report archive](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46004721023/artifacts/15203590104),
  SHA-256 `cbbfe203caedebdffdfa07f64ee52c851cef25102799d9740804857240f063ee`.
- [10k report archive](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46004721023/artifacts/15202911859),
  SHA-256 `3ccf30b802f60498880ae66cca1bff9f1595b88a5e0c23b63dbb418b84629218`.
- [50k report archive](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46004721023/artifacts/15205360923),
  SHA-256 `cdf9cbd79e02d5539eb3a9e541cdeaefa9e448193a91304732609acabd84a66b`.

This audit has not read the report JSON's P50/P95, peak RSS or sampled disk
values and does not invent them. Native startup, peak child RAM/VRAM,
transient disk high-water mark, a customer library, semantic inference and
retrieval quality are separate measurements.
[The harness contract](LIBRARY_CAPACITY_MEASUREMENT.md) describes the recorded
workload. The [retained-capacity receipt](acceptance/RETAINED_CAPACITY_431_20261127.md)
keeps these measurements tied to their source; the job links above identify
the terminal results.

The `fa79` composition adds `--complete-backup`: full typed snapshot,
manifest verification, retirement of only the harness's source library,
independent restore, every-edition identity digest and original/source-tail
checks before fresh startup. **None of the three 431 runs contained this new
phase.** Its source and tests are present; its 1k/10k/50k execution remains
NOT_RUN until the new exact reports pass.

[`semantic-cache-scale.json`](evidence/semantic-cache-scale.json) still
describes only synthetic 384-dimensional vector cache entries.
[`long-audio-rf64.json`](evidence/long-audio-rf64.json) separately proves a
real ten-hour 48 kHz RF64 stream of 6,912,000,080 bytes from repeated procedural
material. Neither substitutes for corpus import or full-length original
neural audio.

## Observed native results and failures to close

| Exact source / evidence | Observed result | What it does not establish |
|---|---|---|
| `55725629`, [managed installation run 45985890211](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45985890211) | Actual corrected A665 → B557, full-wrapper rollback, Repair, Uninstall, registry/vault and library-data checks passed; 251 strict cases in eight suites, zero skips. | Later packages still need the same exact-source run. Production signing, public endpoints, rights and NVDA are separate. |
| `c68d950a`, [audio job 140496095412](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45996200703/job/140496095412) | Native live/recorded/Studio controls and appearance/pacing passed. The subsequent strict step passed 34 of 37 suites. | Native passes do not make the failed whole workflow green. Pacing long test IDs and other concrete failures received later corrections. |
| `f8d5f856`, [audio job 140512807125](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46001149163/job/140512807125) | Earlier native SAPI/FFmpeg/effects/recording/reader/appearance/speech-preparation controls passed; strict step passed 42 of 43 suites. | The last existing-file fixture needed explicit replacement; a later broad step was not reached. |
| `819d6bd9`, [audio job 140526455657](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46005184387/job/140526455657) | 55 of 60 strict suites passed. Native scheduling and structured translation passed. Four Book suites used an invalid mutable Effect fixture; a real Book field-label construction collision and completed-schedule Resume inconsistency were identified. | Current Book and `8911b2ae` schedule corrections need their new exact run. Counts overlap other suites and must not be summed. [Failure record](acceptance/AUDIO_819_ACTUAL_FAILURES.md). |
| `819d6bd9`, [main Windows job 140526456685](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46005184320/job/140526456685) | The early ASR step passed six of eight suites: four legacy recovery cases hit an inappropriate managed-WAV header gate; one export case expected the other valid missing-format error. | `8697ce93` keeps archived originals opaque while retaining their typed inner hashes, with current-managed-WAV and forged-outer-manifest controls. New tests are NOT_RUN. |
| `819d6bd9`, [trusted-client run 46005184306](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46005184306) | Both OS jobs passed ten security suites and failed the desktop fixture suite solely on two omitted book-menu installer bindings. | The combined UI was not accepted. `fa79` corrects both inventories and requires the actual semantic book command. |
| `16207681` / `819d6bd9`, [sync job 140534499641](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46007562069/job/140534499641) | 393 passed, nine failed: one stale publisher callback fixture, six isolated crash-child package-origin mismatches, two book-menu fixture omissions. | `fa79` preserves real origin and publication assertions while correcting the fixtures; it requires separate source and exact installed recovery. Execution of the correction remains pending. |

Independent publication acceptance also actually passed at `819d6bd9`,
[job 140526455530](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46005184352/job/140526455530).
The exact installed boundary suite passed 13 cases without skips. Actual
EPUBCheck 5.3.0 accepted three current-produced EPUB3.3 fixtures with zero
fatal/error/warning/usage messages and rejected the deliberately invalid
archive. Local DAISY DTD checks accepted the three current-produced cases and
rejected invalid XHTML. The reports explicitly leave
`full_daisy_rule_conformance`, external-reader interoperability and NVDA
NOT_RUN, and do not claim WCAG conformance. These fixtures use synthetic PCM
through actual current production/FFmpeg, not model-generated speech quality.

The `fa79` publication receipt change preserves an accepted output through
ordinary late cleanup errors, keeps a pending native owner's original
exception/owner, and propagates interpreter control signals with the committed
receipt attached. The queue correction seals actual staging bytes before
native identity checks and never deletes a foreign substituted stage.
Both need their authored fault tests executed on the composed source.

## V3-01 through V3-22 qualification map

| Requirement | Implemented boundary and remaining evidence |
|---|---|
| V3-01 | Named selected converters above and current OCR/model manager. Mixed scripts, RTL/vertical text, layout fidelity and region-level second passes need per-scenario evidence. |
| V3-02 | Immutable originals, reversible editing/normalization and scoped dictionaries. Only explicitly implemented locales/rules are supported; learned restoration and pronunciation quality remain separate. |
| V3-03 | Current live/recorded reading, appearance, pacing and independent playback controls. Current native/manual acceptance and explicitly qualified SAPI4 compatibility remain open. |
| V3-04 | Current speech/render recovery, ordered book batches, naming/presets/metadata and measured splits. Qualify actual selected voices, exact current batch workflow and long-form listening. |
| V3-05 | Effects, master chain, clips/consolidation, segmented recording/monitoring, media conversion and queue implemented. Physical devices, live latency/quality and current native regressions remain. |
| V3-06 | Full/incremental typed snapshots, current queue, pending imports, foreground schedule and inert templates. Execute current complete-corpus restore and legacy-queue migration; no archived executable/device authority. |
| V3-07 | Continuations and explicit large TXT/PDF profiles; actual 1k/10k/50k retained corpora passed on431. New backup phase, release-source performance and declared hardware metrics remain. |
| V3-08 | Academic sources, evidence, CSL and editable Word/regeneration flows integrated. Exact current source-grounded export, receipt and quotation tests remain the evidence. |
| V3-09 | Guided/local typed planning, expanded tools, scopes, approval, broker and durable execution. Selected local-model inference and measured multi-request engine session behavior are not established by registry or synthetic HTTP tests. |
| V3-10 | Semantic command registry, native controls, keyboard and presentation preferences. Manual NVDA workflows and per-language UI localization remain separate. |
| V3-11 | Current provider gateway, multiple explicit profiles, vault, budgets, Google Basic/Advanced and local transport. Actual account-authorized outcomes, quotas/terms and selected-runtime identity require current evidence. |
| V3-12 | Procedural/MIDI/FluidSynth and selected neural music adapters remain distinct. Actual MusicGen/ACE-Step assets, supported hardware, inference and quality still require qualification. |
| V3-13 | Typed production plans and the current complete render executor produce real artifacts; proposals alone do not. The requested approved multi-role/music/SFX scenario, recovery and changed-scene regeneration require selected-engine and listening evidence. |
| V3-14 | Ten-hour RF64 streaming evidence exists for repeated procedural material. Multi-hour original neural generation and representative model-segment continuity are not thereby established. |
| V3-15 | The exact 135-entry catalog is an inventory, not feature coverage. Record each OCR/detection/spelling/restoration/pronunciation/TTS/STT/translation/UI capability with its exact engine, pack and language evidence. |
| V3-16 | Current bootstrap, source inventory, owned processes, trust/credentials and malformed-data guards. Exact final installed/Windows regression, production signing and residual-risk review remain necessary. |
| V3-17 | Rights/attribution/private licensing are independent of execution. Full FFmpeg redistribution remains denied until its exact clearance; private packaging does not waive obligations. Recovery remains independent of license state. |
| V3-18 | Local signed-update/standard managed installer mechanisms exist; actual A→B rollback/Repair/Uninstall and original-data backup/restore passed at819. Later final packages must repeat them. Production endpoints/keys and manual accessibility are external gates. |
| V3-19 | Shared cancellable progress, quiet/audio suppression and persisted voice/preferences exist. Exact native event tests, measured units and manual NVDA listening remain; synthetic probes do not qualify physical output. |
| V3-20 | Supplied ASR adoption, structured/local translation, tasks/command palette and synchronized publishing/reading are integrated. Actual supplied inference, external readers, full DAISY rules, optional Braille and ASR-based acoustic QA remain separately scoped evidence/work. |
| V3-21 | Existing owner engine/voice archives are known inputs for review. Consent-bound reference handling, admitted exact runtime/model, multilingual similarity/intelligibility and actual Windows/hardware cloning are not established by an adapter. No replacement training stack is authorized. |
| V3-22 | Neutral entitlement/delivery, trust and refresh contracts do not establish deployed private billing/account authority. Real owner credentials, endpoints, signing and rights remain external dependencies. |

Canonical source/data disposition is tracked separately in
[the e3cf convergence record](CANONICAL_CONVERGENCE_E3CF_20261127.md)
and its [135-path mapping](acceptance/canonical-e3cf-source-disposition.json).
Preserving old bytes is not live-runtime compatibility; explicit supported
imports use the current stores. Do not merge a second Studio, speech or provider
implementation merely to make a file-count comparison disappear.

A release claim requires the exact final source/package identities and completed
required gates. Keep every known code defect, unsupported scenario and unexecuted
qualification visible; do not replace either a real PASS or an honest NOT_RUN
with a broader claim.

## Later exact contract corrections

The first Linux e43 V3 contract run exposed the pypdf integer-subtype read issue
and an installed checker which incorrectly required unshipped bootstrap probes.
The current corrections retain the bounded read limit and require exact declared
Python inventory, including rejection of missing, extra and changed installed
code. [Observed contracts and managed update](acceptance/CONTRACTS_AND_UPDATE_OBSERVED_20261127.md)
records the exact runs and source boundaries. New corrections remain NOT_RUN.

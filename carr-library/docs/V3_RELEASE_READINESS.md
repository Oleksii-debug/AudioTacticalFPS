# Resonance — current integration and release evidence

Product: **Resonance / Резонанс**, version **0.2.0.dev2**.

This is the current integration record for the 27 November technical project.
The complete platform is **NOT_QUALIFIED**. The implementation and automated
qualification are being reconciled against the updated scope; intended
capabilities are retained even when their actual engine, hardware, manual or
distribution acceptance still needs input.

Start with [START_UK.txt](START_UK.txt) for the actual installation paths,
menus and keyboard commands. The [technical specification](UNIVERSAL_SCOPE_V3_FINALIZATION.md),
[detailed audio/agent scope](UNIVERSAL_SCOPE_V3_AUDIO_AGENT.md), retained
[V2 library contracts](UNIVERSAL_SCOPE_V2.md) and [acceptance gates](acceptance.md)
remain authoritative. The
[machine-readable matrix](validation/RESONANCE_REQUIREMENT_STATUS_20261127.json)
keeps their exact source blobs and separate evidence dimensions.

## How to read the evidence

Source review, an adapter's presence, integrated behavior, actual execution,
quality qualification and distribution clearance are different states.
The final candidate's PR and immutable CI receipts must identify the exact
source/tree, wheel, dependency/runtime packs, commands and outcomes. This
document neither adds overlapping test counts nor carries PASS to a later
source tree.

The reviewed full regression oracle currently declares **5,683 protected source
cases and 5,355 protected installed cases**. The focused selection declares 909
per mode after CHM and owner-voice recovery integration. These are selected populations, not
execution results. The finite existing native families, strict failure/skip
checks, immutable-original guards and complete runtime inventories remain in
place.

## Observed component executions

| Component | Exact source and actual scope | Durable evidence |
| --- | --- | --- |
| Shared recovery/publication/generated audio/ODF | `375f6cc7c62ddf3dce248761ebe8eccdb1cbf9de`: 805 source + 805 installed, zero failure/skip; exact 22 native provider checks and cleanup. Includes generated-media 36 and ODF 55. | [Exact805 proof](validation/SHARED_CONTRACTS_805_WINDOWS_20261127.json) |
| Ordinary CHM admission | `e5453076c9593219e2d851d8f96a7fa62680d219`: 236 source + 236 installed, actual 4 native CHM checks in each mode; 149 Linux cases. The source-bound real publisher case asserts 70/70/0 members, without a separately retained raw per-member digest census. | [Ordinary CHM proof](evidence/chm-ordinary-admission-independent-read-20261127.json) |
| Installed Windows synthetic capacity | `523aa40f9215569a276f0625d09fcc0fa84bb520`:complete 1k/10k/50k edition traversal, including 500 pages at 50k, with cancellation and fresh-process recovery. One shared tiny original is not 50k real books. | [Capacity receipt](evidence/r7-installed-capacity-20261127.json) |
| Native SAPI process/PCM path | Earlier exact current-engine run and independently checked native PCM/process/file-release evidence; later engine changes require their own current gate. | [SAPI execution record](NATIVE_SAPI_OWNED_SHUTDOWN_RESULTS_20261127.md) |
| Supplied ASR host/assistant | Actual isolated synthetic protocol, source/installed and native UI records; private source corrections and model inference remain separately identified. | [ASR integration record](validation/ASSISTANT_TRANSCRIPTION_WINDOWS_20261127.md) |
| Standard maintenance/update | Historical direct-original managed attempt failed; current O→maintenance A→managed B is a separate cohort requiring its exact 4 + 15 checks, installed-library 50 and recovery proof. | [Current standard contract](STANDARD_SIGNED_UPDATES.md), [historical failure](evidence/standard-update-480f-artifact-diagnostic-20261127.json) |
| Selected Office | Exact earlier run read all 7 source and 7 installed cases, but failed complete runtime immutability because 42 Python cache files were added. Its later no-bytecode run exposed a separate transient path failure; the final normal gate remains required. | [Converter qualification](OPTIONAL_CONVERTER_QUALIFICATION.md) |

These rows are not a whole-candidate acceptance verdict. The prior failures
remain visible and are not converted into success by a source-only correction.

## Requirement matrix

| Requirement | Current implemented scope | Remaining qualification or scope |
| --- | --- | --- |
| V3-01 / A — Documents and OCR | Existing isolated import/conversion stack, explicit encodings, managed language/model packs and bounded legacy adapters; exact EML metadata, CHM member recovery and strict ODF page-layout handling. [Current contract](DOCUMENT_CONVERSION.md). | Actual selected-tool runs, precise format/fixture limits and separate language/model acceptance; arbitrary document fidelity and all OCR languages are not inferred. |
| V3-02 / B — Reading preparation | Immutable originals, editable/versioned narration, source mapping, pronunciation, scoped rules and explicit preparation modes through the existing reader. [Current contract](READING_PREPARATION.md). | Language-specific pronunciation/stress quality and actual engine prosody need qualified voices and human review. |
| V3-03 / C — Voices and live reading | One VoiceRegistry with actual native SAPI, explicit reviewed local profiles, the owner-specific worker adapter and the distinct generic helper protocol; live reading and retained playback. [Current contract](LOCAL_VOICES_AND_MUSIC.md). | Exact owner-runtime routes and post-render/process integrity are source-reviewed and awaiting their new regression run; real owner voices, legacy drivers, hardware and acoustic quality remain separately qualified. |
| V3-04 / D — Audiobooks and scenes | Current narration, role/scene projects, batch jobs, chunk identity, durable publication, output formats, chapters, stems and measured duration through the shared renderer. [Current contract](AUDIO_STUDIO_V3.md). | Each actual voice/model/codec pair and long-form listening quality needs its own evidence; synthetic audio duration is not neural-generation throughput. |
| V3-05 / E — Studio and player | Native track/clip/time controls, non-destructive editing, effects chains, local playback, recording/monitor interfaces and exact output-frame/media validation. [Current contract](STUDIO_CLIP_EDITING.md). | Physical microphone/output/monitoring, real-time device behavior and professional listening/NVDA acceptance remain distinct from automated PCM/effect tests. |
| V3-06 / F — Save and recovery | One Save All coordinator, current complete/incremental backups, passive canonical history, retained pending inputs and explicit restore/resume review. [Current contract](COMPLETE_BACKUP.md). | Actual device power-loss/manual desktop recovery and declared per-pack redistribution remain separate; old state is not silently activated. |
| V3-07 / G — Scale | Bounded paging, continuation, streaming originals and current catalog/query optimization; source-bound Windows measurements complete1k/10k/50k synthetic edition traversal. [Current contract](LIBRARY_CAPACITY.md). | The fixture shares one tiny original. Real diverse books, millions of segments, large-file throughput and declared user hardware require additional measured corpora. |
| V3-08 / H — Research and citations | Scoped scientific search/evidence, bibliographic metadata, CSL, exports, acquisition adapters and Word citation boundaries through existing owners. [Current contract](V3_BIBLIOGRAPHY.md). | Actual licensed Word/Zotero/editor interoperability and lawful real-corpus relevance are separately recorded; controlled COM tests do not qualify installed Office. |
| V3-09 / I — Local assistant | Deterministic plans, reviewed local-LLM proposals, typed bounded tools, one durable executor, explicit scope/budgets and reapproval after changes. [Current contract](LOCAL_PLANNING_RECONCILIATION.md). | A real approved local model and multilingual tool-selection/hardware benchmarks are needed for model-planning qualification. |
| V3-10 / J — Native accessibility | Current native wx menus, dialogs, focus handling, command registry, keymap migration, Save and status/progress controls. [Current contract](KEYBOARD_SETTINGS.md). | Automated native controls do not substitute for real Windows 11/NVDA keyboard use or complete Ukrainian/Slovak UI localization. |
| V3-11 / K — Optional providers | Existing profile store, Windows credential references, allowlisted gateway, transfer/cost approval, budgets and durable request/recovery identity. [Current contract](PROVIDER_RECOVERY.md). | Real account/model availability and live paid calls require explicit owner configuration/authorization; no service is enabled by this code. |
| V3-12 / L — Local music | Current selected music adapters, actual MIDI control assertions, licensed-asset/profile boundaries, stems and local generation orchestration. [Current contract](LOCAL_MUSIC_ADAPTERS.md). | Real approved neural weights, hardware, output quality and per-asset rights remain required; MIDI is not neural music. |
| V3-13 / M — Production director | Approved typed script/scene plans call existing voice/music/effect/render/publication services and retain completed output identities. [Current contract](ASSISTANT_WORKFLOW_TEMPLATES.md). | End-to-end neural multi-voice/music quality and creative model selection await actual qualified engines; model text never grants arbitrary execution. |
| V3-14 / N — Long production and profiles | Bounded long-audio assembly, checkpoints, measured frame/duration verification and separate usage/rights profiles. [Current contract](MEASURED_BOOK_DURATION.md). | Hours of original neural generation, sustained GPU/VRAM/load cost and compatible model rights are not established by streaming assembly tests. |
| V3-15 / O — 135-language target | One derived canonical 135-entry catalog and per-feature/per-engine status matrix; no independent replacement catalog. [Current contract](language_qualification.md). | Every relevant voice/model/language pair needs real qualification; a catalog or route entry is not proof of speech, cloning, ASR, translation or OCR quality. |
| V3-16 / P — Security and privacy | Current trust/admission boundaries, immutable assets, bounded filesystem/process handling, credential isolation and reviewed signed-client primitives. [Current contract](TRUSTED_CLIENT.md). | Private service integration, deployment threat review and actual release profile remain separate; no confidential runtime/keys are published. |
| V3-17 / Q — Rights and attribution | Exact current dependency/runtime provenance, component notices and independent model/voice/asset rights records. [Current contract](ABOUT_COMPONENT_NOTICES.md). | Per-profile distribution clearance, owner-approved legal terms, signing and publisher configuration are required before a distributable release. |
| V3-18 / R — Standard install and updates | One ordinary Inno installer and signed managed transaction, protected state, generation leases, rollback/recovery and the distinct original→maintenance→candidate proof. [Current contract](STANDARD_SIGNED_UPDATES.md). | Only exact actual installer/managed/restart/library receipts qualify the cohort. Historical direct-original managed failure remains explicit; production authority is not configured. |
| V3-19 / S — Audible progress | One measured progress contract, native status and explicit optional voice feedback, quiet/media ownership and durable completion boundaries. [Current contract](SHARED_NATIVE_PROGRESS.md). | Real NVDA progress announcements and absence of device/audio contamination require physical/manual evidence. |
| V3-20 / T — Complementary workflows | Supplied ASR host, synchronized publication/reading, translation scopes, typed workflow templates, diagnostics and declared project interchange. [Current contract](SYNCHRONIZED_PUBLICATION_READING.md). | Actual external readers, EPUBCheck/DAISY/Braille/table packs, optional watchers/plugins, tactile output and ASR-based acoustic QA must retain their own implemented/qualified status; no blanket completion is claimed. |
| V3-21 / U — Owner voice cloning | Current owner-specific adapter and reviewed engine/profile boundary reuse the supplied engine interface; private samples/models remain external. [Current contract](LOCAL_ENGINE_PROFILES.md). | The professional consent/reference/profile and multilingual cloning workflow requires the actual owner-engine contract/assets, authorized samples and real intelligibility/similarity/control evidence; not replaced by a registered SAPI voice. |
| V3-22 / V — Entitlements and translation | Neutral external contract plus current signed-client/offline-refresh/recovery boundaries, separated from update activation and official Google/local translation adapters. [Current contract](DELIVERY_INTEGRATION_CONTRACT.md). | Real private authority/account endpoints, policies and authorization; actual selected translation models/providers and independent rights/quality. |
| ASR — Supplied transcription | Reviewed supplied V4.3/V5 worker profiles, real isolated synthetic protocol interoperability, content-bound originals/cache/receipts, TXT/SRT/VTT/JSON, edits, cancellation/resume and recovery. [Current contract](TRANSCRIPTION_INTEGRATION_REVIEW.md). | The supplied source is available and reviewed. Actual installed recognition weights, acoustics/timestamps/language quality, diarization and hardware are separate, not a generic missing-project blocker. |
| G/U — Retained library contracts | Independent libraries/collections, topics/notes, exact raw source evidence, scoped retrieval, export/no-clobber and verified restoration. [Current contract](UNIVERSAL_SCOPE_V2.md). | Real lawful corpus acceptance, semantic relevance, actual NVDA and declared release artifact/rights gates remain independently required. |

## Canonical reconciliation

The merge must retain a finite per-path decision rather than silently discarding
canonical changes. The52 general source paths are recorded in
[the source disposition](validation/CANONICAL_GENERAL_SOURCE_DISPOSITION_20261127.json).
The corresponding canonical missing-test and shared-contract records retain
exact old blobs, actual replacement tests and differences in Python/CLI/state
interfaces. Canonical documentation has an explicit
[17-path disposition](validation/CANONICAL_DOCUMENTATION_DISPOSITION_20261127.json);
obsolete entry points route to current owners and their immutable history.

The owner-specific voice adapter remains distinct from the generic local JSON
helper. Passive backup/history recovery does not activate engines or validate
generated media. Current consumption must establish runtime, input, settings
and media identity again. UI/CLI/assistant keep the same domain owners and
approval boundaries.

## Inputs that automated development cannot substitute

Full acceptance needs the actual authorized owner voice/cloning runtime and
samples; actual ASR, local translation, LLM and neural music models with their
rights and supported hardware; real recordings/listening quality and per-language
corpora; physical device and manual Windows11/NVDA checks; and a separately
approved signed distribution/service profile. These remain requested product
requirements. A passing synthetic protocol, catalog entry or long PCM file
does not complete them.

The supplied transcription source has already advanced beyond a missing-project
state: its reviewed profiles and compatibility work are retained. No private
source, recordings, transcripts, models, credentials or issuer keys are added
to public Git. User-owned data stays readable/exportable/recoverable across
network or entitlement failures.

The [historical readiness snapshot](https://github.com/Oleksii-debug/AudioTacticalFPS/blob/e5c64a5245583f8f8b404a0f042f6ca438c24d7f/carr-library/docs/V3_RELEASE_READINESS.md)
and [earlier integration checkpoints](RESONANCE_INTEGRATION_CHECKPOINT_20261127.md)
remain source-specific history; they do not override the current technical
project or a failed current gate.

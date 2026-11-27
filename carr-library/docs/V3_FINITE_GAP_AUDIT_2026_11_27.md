# Finite V3 implementation and qualification gaps

The initial audit described `7e830912c399fe0c9eaacd19540d86982c3bf548`.
The rows below include the reader/scanner/master composition after `df0f7c1`;
[current executed evidence](V3_INTEGRATION_EVIDENCE_2026_11_27.md) binds its
installed runtime to `2ba600f`.
The authorities are [the V3 finalization scope](UNIVERSAL_SCOPE_V3_FINALIZATION.md)
and [the audio scope](UNIVERSAL_SCOPE_V3_AUDIO_AGENT.md), not the number of
adapters, tests, catalog rows, or merged branches. Later implementation must
update the relevant row with its exact source and executed acceptance evidence.

The canonical `966e5ccf892243ead6942c635af5fbab47e72551` search modules and
`ingest/batch_scan.py` are identical to this candidate. Their pagination cannot
be recovered by importing another existing search stack. The current source
already incorporates compatible canonical reading, Studio, backup, progress,
conversion and publication behavior; preserved historical records do not make
an incompatible runtime executable.

## Priority work that does not require private models or customer credentials

| Priority | Scope | Confirmed remaining behavior | Existing implementation to extend |
|---|---|---|---|
| 1 | V3-07 | Search and folder continuation now have bounded explicit pages and executed source/installed acceptance. Original documents still use the default 64 MiB boundary; the new large UTF-8 profile and corpus-scale measurements are active work. | [`application.py`](../src/carr_library/application.py), [`search/federated.py`](../src/carr_library/search/federated.py), [`ingest/batch_scan.py`](../src/carr_library/ingest/batch_scan.py), [`catalog/intake.py`](../src/carr_library/catalog/intake.py). Keep each operation bounded; add continuations and configurable budgets rather than removing safety checks. Catalog browsing already has keyset pagination. |
| 2 | V3-01 | Thirteen named legacy suffixes have no integrated adapter. Five have an installed local conversion candidate, as detailed below. | [`conversion/capabilities.py`](../src/carr_library/conversion/capabilities.py), existing selected Pandoc/raster adapters and owned process supervisor. |
| 3 | V3-03 | Exact-source live reading is implemented and passed installed synthetic acceptance; actual native wx/SAPI acceptance is pending. Independent playback speed/pitch remain code work. | [`ui/reading_desk.py`](../src/carr_library/ui/reading_desk.py), [`ui/narration_editor.py`](../src/carr_library/ui/narration_editor.py), current SAPI adapters and [`audio/playback.py`](../src/carr_library/audio/playback.py). Existing playlist, A–B repeat, sleep timer, seek, pause and resume are already present. |
| 4 | V3-04/05 | Role/master chain editing, reorder/bypass/presets and master dry/wet are implemented; clip cut/copy/paste/split and compatible adjacent join are implemented. Arbitrary different-asset consolidation, recording pause/resume and monitoring remain code work. | [`ui/audio_effects.py`](../src/carr_library/ui/audio_effects.py), [`ui/audio_clips.py`](../src/carr_library/ui/audio_clips.py), [`audio/models.py`](../src/carr_library/audio/models.py), [`ui/recording_dialog.py`](../src/carr_library/ui/recording_dialog.py). Do not describe a typed filter list as the complete professional editor. |
| 5 | V3-04/05 | General audio batch conversion, video soundtrack extraction, and a durable multi-book audiobook preset/naming/split-review workflow are not completed native actions. | Current FFmpeg ownership, Studio plans, prepared assets and receipt-bound exporters; no additional audio framework is needed. |
| 6 | V3-02 | No Hunspell spellcheck adapter or general number/date/unit/symbol reading normalization. Current normalization deliberately only adjusts spacing. | [`reader/narration.py`](../src/carr_library/reader/narration.py) and current bounded language/voice pronunciation rules. Statistical punctuation, stress and prosody quality need separate model evidence. |
| 7 | V3-20 | EPUB3/DAISY authoring exists; opening those publications for synchronized text/audio reading does not. | [`audio/publishing.py`](../src/carr_library/audio/publishing.py), [`audio/daisy.py`](../src/carr_library/audio/daisy.py), static EPUB reader and current bounded player. Format validation is distinct from listening/navigation interoperability. |
| 8 | V3-20 | Translation operates on selected text, with durable chunks and receipts; no document/chapter/batch selection and structure-preserving derivative workflow is exposed. | [`assistant/translation.py`](../src/carr_library/assistant/translation.py), [`ui/translation_dialog.py`](../src/carr_library/ui/translation_dialog.py), existing document readers. Real paid service acceptance remains credential dependent. |
| 9 | V3-09 | Typed assistant tools do not yet include conversion, reading controls, playback or effects editing. | [`assistant/models.py::TOOLS`](../src/carr_library/assistant/models.py): create/list/catalog/import/import_folder/search/extract/backup/audio.produce. Reuse the same scope, approval and executor contracts. |
| 10 | V3-06 | Complete backups are full snapshots; incremental reuse and scheduled local backups are not implemented. | [`library_backup.py::create_backup`](../src/carr_library/library_backup.py) and current typed producers. Encrypted sync alone is not incremental backup. Scheduling is optional scope; incremental recovery needs its own manifest and corruption tests. |

## Named input format inventory

The current direct registry supports TXT, PDF, DOCX, EPUB, HTML/HTM/XHTML,
Markdown, EML, FB2/FB3, ODT/ODP/ODS, PPTX and XLSX. Explicitly selected Pandoc
adds RTF. Explicit OCR configuration adds supported raster/TIFF files.

| Suffixes still `not_integrated` | Local inspection on 2026-11-27 | Remaining gate |
|---|---|---|
| `.doc`, `.ppt`, `.xls` | Installed LibreOffice registry has MS Word 97, MS PowerPoint 97 and MS Excel 97 IMPORT filters. | Selected-runtime binding, isolated profile, macro/external-resource policy, fixed conversion outputs, actual fixtures and native qualification. |
| `.wpd` | Installed Writer registry has WordPerfect IMPORT filter. | Same adapter and format fixtures; not every historical WordPerfect variation is thereby qualified. |
| `.pdb` | Installed Writer registry has PalmDoc, Plucker and Palm text IMPORT filters. | Explicit supported PDB variants; the extension alone cannot identify an arbitrary Palm database as a book. |
| `.azw`, `.azw3`, `.lit`, `.mobi`, `.prc`, `.tcr` | `calibre` and `ebook-convert` absent. Pandoc does not advertise these input formats. | Reviewed optional converter provisioning and integration; DRM/password bypass remains out of scope. No customer model/key is needed for ordinary unprotected fixtures. |
| `.djvu` | `djvutxt`, `djvused`, `ddjvu` and discovered DjVuLibre shared library absent. | Reviewed DjVu reader or selected-tool adapter and real fixtures. |
| `.chm` | No CHM utility/shared reader found. | Reviewed CHM reader/adapter, archive and external-resource bounds, real fixtures. |

The executable actually present is LibreOfficeDev `26.8.0.0.alpha0`, revision
`2c87e51eeaa2b413ff4ae097b2705eea1995d8e5`. Its host version and filter inventory
were executed/inspected; no new document conversion or Windows qualification
was performed by this audit. Pandoc is `3.1.11.1`; its advertised formats do not
include these thirteen suffixes. Native Hunspell 1.7 and Enchant libraries are
present, but no corresponding Python module was found. These observations
identify reuse candidates, not product availability or redistribution clearance.

The decisions in [REUSE_DECISIONS_V2](REUSE_DECISIONS_V2.md) and
[DOCUMENT_EXTRACTOR_REUSE](DOCUMENT_EXTRACTOR_REUSE.md) favor adapters and
interoperability. Their older candidate statuses must not override newer
executed source, nor justify importing a duplicate Studio or reader stack.

## V3-01 through V3-22 qualification boundary

| Requirement | Current boundary / next evidence |
|---|---|
| V3-01 | Named legacy adapter gaps above. OCR runtime/model-manager coverage does not by itself qualify every mixed-script, RTL, vertical or region-level second-pass workflow. |
| V3-02 | Immutable originals, editable source-bound derivatives, history and bounded scoped dictionaries exist. Spellcheck/general linguistic preparation gaps remain; learned restoration requires actual language evidence. |
| V3-03 | Live reader integration is implemented; native composed acceptance and independent live playback controls remain; SAPI4 needs an isolated helper and qualified Windows/runtime combination. |
| V3-04 | Chapter plans, persistent render recovery, codecs and synchronized exports exist. Multi-book batch/preset/splitting and metadata acceptance require finite workflow evidence. |
| V3-05 | Real FFmpeg DSP, clips and microphone capture exist. Chain editing and compatible split/join are implemented; arbitrary-asset consolidation and recording controls remain code work; native monitoring/quality need hardware acceptance. |
| V3-06 | Typed complete backup, pending sources and recovery exist. Incremental backup remains code work; historical imports must stay inert unless current validators explicitly authorize them. |
| V3-07 | Bounded continuations are implemented. Large-source streaming and full corpus measurement remain active implementation/evidence gaps. Vector-cache rows are not books. |
| V3-08 | Academic sources, CSL and editable Word flows are integrated. Exact current CI and source-grounded end-to-end checks remain the evidence, not unrelated donor test counts. |
| V3-09 | Guided/typed local planning and bounded execution exist; requested reader/conversion/player/effects tools are absent. Actual local model inference requires approved assets. |
| V3-10 | Shared command registry, accessible controls and native probes exist. Manual NVDA workflow acceptance remains separate; no 135-language UI claim is supported. |
| V3-11 | Profiles, explicit network approval, credentials, budgets and translation/LLM transports exist. Actual paid-provider outcomes need selected accounts and owner-authorized calls. |
| V3-12 | Local neural adapters and procedural generation are distinct. Music model assets and actual inference/quality evidence are still required. |
| V3-13 | Typed script-to-audio plans and the current production executor exist. A model-generated proposal is not an accepted finished production; integrated Director actions, inspected outputs and actual selected speech/music engines require end-to-end evidence. |
| V3-14 | The existing ten-hour RF64 proof demonstrates bounded streaming of repeated procedural score. It is not long-form neural generation, speech quality or full production-studio acceptance. |
| V3-15 | The exact 135-language catalog is an inventory. Qualification must be recorded separately for OCR, detection, spelling, restoration, pronunciation, TTS, STT, translation and UI. |
| V3-16 | Source/bootstrap/trust protections have synthetic and native evidence. Final signing, license and rights clearance cannot be inferred from them. |
| V3-17 | Rights, attribution and private licensing are separate from implementation. Full FFmpeg redistribution remains denied pending exact clearance; private packaging is not a workaround. Data recovery must remain independent of license state. |
| V3-18 | Local TUF/update and managed installation mechanisms exist or are under separately recorded review. Actual A→B Windows wrapper/cache/rollback acceptance must be executed against the composed build. Production website endpoints/signing keys require owner inputs; synthetic tests do not imply deployment. |
| V3-19 | Common progress, cancellation and persisted voice/quiet preferences are integrated. Observer behavior and native probes remain exact-version evidence; actual speech/NVDA and every operation's units need explicit acceptance. |
| V3-20 | Supplied ASR source integration, translation, command palette, tasks and publication exports exist. Synchronized reading and document/batch translation gaps above remain code work; optional local engines need assets. |
| V3-21 | Voice cloning requires owner-authorized engine/assets/reference recordings and consent-bound workflows. Adapter registration is not cloning qualification. |
| V3-22 | Neutral entitlement/delivery contracts do not establish private account/billing deployment. Real owner authority, secrets, endpoints and rights remain external gates. |

## Capacity evidence must describe the actual workload

[`semantic-cache-scale.json`](evidence/semantic-cache-scale.json) measures
1,000/10,000/50,000 synthetic 384-dimensional vector cache entries. It explicitly
does not run neural inference, retrieval quality or the native application.
It does not establish performance on 50,000 books, millions of source segments,
or thousands of result pages. End-to-end import, search, source opening, start,
reopen/recovery, memory and disk measurements with P50/P95 are still needed.

[`long-audio-rf64.json`](evidence/long-audio-rf64.json) records a real ten-hour
48 kHz RF64 stream (6,912,000,080 bytes). This is useful bounded disk-streaming
evidence; the repeated procedural source and exact runtime must remain visible.

Do not mark the platform complete while a known code gap remains. Do not mark
an adapter QUALIFIED merely because it is registered or a synthetic boundary
test passes. Use distinct source implemented, executed synthetic, native
executed, model/language qualified, manual accessibility accepted and
distribution-cleared statuses.

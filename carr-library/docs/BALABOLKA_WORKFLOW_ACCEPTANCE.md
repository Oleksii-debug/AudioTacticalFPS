# Requested reading and synthesis workflow acceptance

## Scope and evidence boundary

This is the finite checklist required by [V3-04](UNIVERSAL_SCOPE_V3_AUDIO_AGENT.md)
and [finalization, engineering decision 1](UNIVERSAL_SCOPE_V3_FINALIZATION.md).
It covers the requested familiar reading, text preparation, conversion and
audiobook workflows. It does not promise every present or future feature of
Balabolka, reproduce its interface, or imply affiliation.

Source review: **2026-11-27**, public commit
`8bf411718548809793748ed01f458c79f3692e98`, tree
`f8345c43a294315dedb15f30042a38d12333e5a7` (local equivalent
`b9b6e76b02713a01498792be5174b068a8c7510c`). Later changes require updating the
affected rows, not assuming that their acceptance follows from this snapshot.

Source additions on the same date include retained EPUB recorded-audio reading
and DAISY 2.02 export through the existing Studio service. Their bounded behavior
and exact-byte evidence are documented in [recorded EPUB reading](EPUB_RECORDED_READING.md)
and [DAISY publication](STUDIO_DAISY_PUBLISHING.md). Native and human acceptance
remain separate from these source additions.

| Status | Meaning in this inventory |
| --- | --- |
| **Supported** | The stated, bounded workflow has a production path and relevant source tests. This is not an installed-build or human acceptance result. |
| **Limited** | A useful production subset exists; the missing part or runtime dependency is stated. |
| **Pending** | The requested user workflow is not complete at this source revision. An existing lower-level API is identified where available. |
| **Unsupported** | The current admitted parser or engine cannot perform this operation. The reason and next boundary are explicit; the original requirement remains recorded. |

**Evidence is separate from status.** The tests below are traceable verification
targets, not a claim that this documentation change reran them. At review time,
[Windows run 45929429163](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45929429163)
later completed with failures recorded in [release readiness](V3_RELEASE_READINESS.md).
Native gates must pass on the exact installed build. Human
NVDA, actual listening, language quality, uncommon-document fidelity and rights
qualification require their own records; none is certified by this inventory.

## Public behavioral references

These official pages were read on 2026-11-27. They informed the bounded checklist;
no vendor executable, source, interface asset, voice or model was copied or run
for this review.

- **R1 — [Product description](https://www.cross-plus-a.com/balabolka.htm):** document reading, installed voices, pronunciation rules, clipboard, tray, shortcuts and presentation options.
- **R2 — [FAQ](https://www.cross-plus-a.com/bfaq.htm):** named-bookmark chapter output, splitting, filenames, subtitles, device choice and voice-use qualifications.
- **R3 — [Console utility](https://www.cross-plus-a.com/bconsole.htm):** voice selection, speech controls, local text input, queues and timestamped text.
- **R4 — [Text extraction utility](https://www.cross-plus-a.com/btext.htm):** multiple inputs, combined or split text, encoding and output naming.

The requested scope is fixed by V3-01 through V3-04, not by every option on those
pages. Vendor skins, proprietary dictionary-file compatibility, viseme export,
vendor-specific online services and arbitrary external filter execution are
not newly added requirements here. Application code, installed engines, voice
assets and output-use permissions remain separate rights decisions under the
[current specification](UNIVERSAL_SCOPE_V3_FINALIZATION.md#8-rights-entitlements-and-updates).

## Implementation and verification map

All paths below are relative to `carr-library/`. A module or test listing alone
does not establish real engine quality or Windows accessibility.

| Key | Production path | Focused verification targets |
| --- | --- | --- |
| D | [Document registry](../src/carr_library/extract/document_formats.py), existing isolated import worker, [Import Center batches](../src/carr_library/ingest/batches.py) | [Extended whole-path imports](../tests/e2e/test_extended_document_workflows.py), [import queues](../tests/e2e/test_import_batches.py), [native format controls](../tests/ui/native_document_formats_smoke.py) |
| C | [ConversionService](../src/carr_library/conversion/service.py), [bounded readers](../src/carr_library/conversion/formats.py), [Convert documents UI](../src/carr_library/ui/document_conversion.py) | [Actual extraction, optional engines and integrity failures](../tests/conversion/test_conversion.py) |
| S | [VoiceRegistry](../src/carr_library/speech/runtime.py), [native SAPI worker](../src/carr_library/speech/sapi_native_worker.py), [voice reader](../src/carr_library/ui/speech_dialog.py), [WavePlayer](../src/carr_library/speech/player.py) | [Actual PCM/runtime checks](../tests/speech/test_runtime.py), [native protocol](../tests/speech/test_native_sapi_protocol.py), [installed Windows SAPI/Studio probe](../scripts/windows_v3_smoke.py) |
| R | [ReadingDocument and pronunciation dictionaries](../src/carr_library/speech/reading.py), [source binding](../src/carr_library/speech/source.py), [recovery store](../src/carr_library/speech/session.py) | [Reversible edits, mappings, regex deadline and dictionaries](../tests/speech/test_reading.py), [source lineage](../tests/speech/test_source.py), [restart/backup](../tests/speech/test_session.py) |
| P | [Studio project](../src/carr_library/studio/project.py), [ProductionService](../src/carr_library/studio/render.py), [native Studio](../src/carr_library/ui/studio_dialog.py), [CLI](../src/carr_library/studio/cli.py) | [Measured three-role production, chapters, subtitles and resume](../tests/studio/test_production.py), [production cancellation](../tests/studio/test_production_cancellation_outcomes.py), [controller persistence](../tests/studio/test_studio_controller.py) |
| A | [AudioRuntime](../src/carr_library/audio/dsp.py), [typed clip editor](../src/carr_library/studio/clips.py), [EPUB publisher](../src/carr_library/studio/publishing.py) | [Actual DSP/codecs](../tests/audio/test_dsp.py), [clip timing and backup](../tests/studio/test_clip_editing.py), [EPUB graph and bytes](../tests/studio/test_publishing.py), [native publication](../scripts/windows_publishing_smoke.py) |
| B | [Reading state](../src/carr_library/reader/reading_state.py), [Reading Desk UI](../src/carr_library/ui/reading_desk.py), [application shortcuts](../src/carr_library/ui/keyboard_settings.py) | [Source-scoped bookmarks/resume](../tests/reader/test_reading_state.py), [native Reading Desk](../tests/ui/native_reading_desk_smoke.py), [shortcut validation](../tests/ui/test_keyboard_settings.py) |

## Requested workflow checklist

The references identify the public behavior being compared; the implementation
column states Resonance's own behavior. Source availability does not remove the
native, human or rights gates above.

| ID | Requested workflow | Status and actual boundary | Evidence / public reference |
| --- | --- | --- | --- |
| W01 | Read manual text, a selection, from cursor, book or chapter; navigate source locations | **Limited.** Voice reader has all/selection/from-cursor commands and an authorized book/chapter source path. Reading Desk navigates source segments/pages. Dedicated current-word/sentence/paragraph commands and continuous source-synchronized speech navigation are incomplete. | S, R, B; R1, R3 |
| W02 | Play, pause, resume, stop and seek | **Supported** for the generated local WAV through the current player, including seek in seconds. Windows MCI position and approximate ffplay elapsed position are distinguished. Live streaming of a book while later chunks are synthesized is not established. | S; R1, R3 |
| W03 | Enumerate/reload local voices and select them for reading or roles | **Limited.** VoiceRegistry retains native SAPI5, Flite and optional local providers. The reader now saves exact per-language defaults and favorites, applies a chosen installed ID explicitly, and refuses missing/changed defaults without substitution. Engine declarations do not establish language quality; installation management, SAPI4 and separately qualified 32-bit host compatibility remain open. | S; [voice preference workflow and backup](VOICE_PREFERENCES.md), `tests/integrations/test_dordiai_speech.py`; R1, R3 |
| W04 | Rate, volume, native pitch, punctuation and pauses | **Limited.** UI exposes supported rate/volume; the runtime rejects unsupported pitch/punctuation. Studio line pauses are explicit. Native prosody is provider-specific; full pitch/style/emotion controls and sentence/paragraph pause presets are not universally exposed. Audio tempo/pitch effects are distinct from synthesis controls. | S, P, A; R1, R3 |
| W05 | Open/save/save-as a reading copy; undo/redo, original/diff, whitespace cleanup | **Supported.** One editable derivative retains source SHA and conservative offset mappings. Save detects external modification; no-clobber exports and source-bound recovery are implemented. Editing does not approve quotations or overwrite retained sources. | R, S; R1, R2 |
| W06 | Find/replace including regex; tabs; encoding choice; font/colors/zoom and presentation modes | **Limited:** dedicated literal/regex Find next/previous and reviewed Replace/Replace all now use the existing cancellable worker, exact native selection and one undoable reading edit. ConversionService accepts explicit supported encodings. Tabbed editing, saved font/color/zoom profiles and speed-reading modes remain pending. | R, C; [Find/Replace contract and actual source tests](READING_FIND_REPLACE.md); native/NVDA evidence per build; R1, R2, R4 |
| W07 | Pronunciation substitutions, dictionaries, scope and preview | **Supported in source for the Voice reader and Studio spoken lines; native qualification pending.** The [shared rule dialog](PRONUNCIATION_REVIEW.md) provides whole-copy/selected-text preview, explicit apply/undo, language/voice scopes, saved library dictionaries and JSON import/no-clobber export. Studio adds explicit line-revision Save/Undo through the existing project store, with complete fingerprint approval and preserved original script, clips and source references. Installed native probes are separate gates. Text substitutions do not prove stress/phoneme or voice quality. | R; [reader lineage](../tests/speech/test_pronunciation_review.py), [reader handlers](../tests/speech/test_pronunciation_handlers.py), [Studio history/recovery](../tests/studio/test_pronunciation.py), [Studio handlers](../tests/studio/test_pronunciation_dialog.py), [actual render binding](../tests/studio/test_pronunciation_render.py), [reader native](../scripts/windows_pronunciation_smoke.py), [Studio native](../scripts/windows_studio_pronunciation_smoke.py); R1, R3 |
| W08 | Hunspell spelling review | **Limited:** explicit native library and `.aff`/`.dic` selection, byte-bound review, suggestions, exact selected-text/whole-copy checks and reviewed one-edit apply/undo now use the existing reader. No automatic acquisition or restored executable approval. Actual POSIX C API and synthetic dictionary execution passed; real Windows DLLs, language/dictionary quality and human accessibility remain separate. | [Hunspell contract, exact native report and tests](HUNSPELL_REVIEW.md); R1 |
| W09 | Requested document inputs and readable extraction | **Limited.** Every requested format is enumerated below. Direct library import and conversion to a derivative have different contracts. Extensions alone do not establish support. | D, C; R1, R4 |
| W10 | Multiple files/folders, batch extraction, combined/split text | **Supported for a finite explicitly selected group.** Document batches retain file/folder snapshots, prepare reading copies, review separate or combined outputs, and use durable same-job pause/cancel/resume and verified receipts. Restored jobs require a new destination and approval. One reviewed split/voice/format applies to the group; a distinct preset for each book remains pending. | D, C, P; [document batch contract and native/CLI gates](DOCUMENT_BATCHES.md); R4 |
| W11 | Split-and-convert by chapters/headings, bookmarks, pages, sentence/paragraph, size/duration, blank lines or regex | **Limited.** Shared exact split plans and the native batch form provide whole/paragraph/blank-line/sentence/character/literal/regex/section/page/estimated-duration choices and direct Studio admission. Sentence boundaries are a disclosed heuristic; duration is an explicit word-rate estimate. The API accepts actual chapter/bookmark positions, but the importer does not invent or expose positions absent from its reading copy. | P, S, C; [split semantics and preserved source maps](DOCUMENT_BATCHES.md); R2, R4 |
| W12 | Preview names; title/first-line/header/sequence naming templates | **Limited.** Text exports have editable title/source/heading/first-line/index/part/total/document templates, exact boundary/name preview, portable sanitization and case/NFC collision checks before approval. Each export is no-clobber with a completion sidecar. Studio preview shows its actual job names; arbitrary renaming of canonical chapter/master audio files is not implemented. | P, C; [reviewed names and collisions](DOCUMENT_BATCHES.md); R2, R4 |
| W13 | Persistent audiobook job, progress, pause/resume/retry/cancel and reuse of completed work | **Supported** for an explicitly submitted Studio project. Hashed chunks, durable job state, output re-verification and restart/resume are implemented. An ordered multi-book queue with a distinct preset for each item remains part of W10/W11. | P; R3 |
| W14 | Select audio codec and numeric audio settings; convert/extract/concatenate audio | **Limited.** AudioRuntime checks actual encoder/muxer capabilities for WAV, MP3, FLAC, Ogg Vorbis/Opus, AAC/M4A/M4B, AIFF and WMA. The conversion form exposes sample rate, channels, PCM bit depth and target compressed bitrate alongside trim/DSP controls; actual output settings are tested. Concatenate remains a service operation and no durable audio-conversion batch queue is provided. Video soundtrack extraction is limited to admitted local containers. | A, P; [numeric conversion controls and actual output tests](AUDIO_CONVERSION_CONTROLS.md); R3 |
| W15 | Audiobook metadata, chapters and cover | **Supported in a bounded source profile; native/human qualification pending.** M4B production embeds explicit title and measured scene chapters. Audio Studio additionally reviews eleven portable tags and explicit keep/replace/remove PNG/JPEG cover choices, then saves a new MP3/FLAC/M4A/M4B copy. Actual readback verifies tags, cover bytes, encoded audio packet hash and preserved chapters before no-clobber publication. Project fingerprints and completed receipts stay unchanged. Other containers and third-party artwork/player compatibility are not qualified. | P, A; [actual M4B evidence](AUDIOBOOK_CHAPTERS.md), [metadata/cover controls and integrity tests](AUDIO_METADATA_COVER.md); R2 |
| W16 | Synchronized text, subtitles, EPUB/DAISY and Braille | **Limited.** Studio emits measured line SRT/VTT/LRC and EPUB3 overlays; retained EPUB recorded-audio navigation and bounded DAISY 2.02 NCC/XHTML/SMIL publication are implemented. Word alignment, full DAISY semantics/interoperability and Braille remain separate open requirements. | P, A; [DAISY](STUDIO_DAISY_PUBLISHING.md), [recorded reading](EPUB_RECORDED_READING.md), [M4B/LRC](AUDIOBOOK_CHAPTERS.md); R2, R3 |
| W17 | Named bookmarks and remembered reading position | **Supported** for source-scoped Reading Desk locations, with explicit bookmark import/export. The voice reader separately retains cursor, voice settings and playback position. Bookmarks are not automatically converted into Studio split points or synchronized audio markers. | B, R; R2 |
| W18 | Controlled command-line/headless workflows | **Limited.** Studio and transcription retain their existing service-backed commands. The dedicated `documents` CLI now captures/prepares/previews/approves/runs the same batch service, with UTF-8 JSON, explicit review fingerprint, real Ctrl+C cancellation and backup/restore-aware resume. Live-reader and every dictionary action are not mirrored as CLI commands; console syntax compatibility is not required. | P, C; [document CLI contract](DOCUMENT_BATCHES.md#command-line-review-and-resume), `tests/conversion/test_document_batch_cli.py`, `studio/cli.py`, [transcription](TRANSCRIPTION.md); R3, R4 |
| W19 | Clipboard reading and optional watching | **Limited.** Voice → Read clipboard opens one bounded Unicode snapshot in the existing reader, with a separate recovery slot and unchanged clipboard. Voice selection and speech remain explicit; no watcher or automatic speech is installed. Optional watching remains pending. Verified academic clipboard copying is a separate workflow. | S; [clipboard action and boundaries](CLIPBOARD_READING.md), [dispatch/recovery tests](../tests/speech/test_clipboard_reader.py); R1, R3 |
| W20 | Tray controls and configurable/global shortcuts | **Limited.** Application menu shortcuts support persistence, conflict checks, reset, reviewed schema 1 keymap import, no-clobber export and complete-map undo of the last applied dialog change. Stable identities and local backup use the existing preferences; see `KEYMAP_INTERCHANGE.md`. Source and handler tests cover these boundaries; native/human qualification is separate. Tray controls and system-wide/global shortcuts remain pending. | B; R1, R3 |
| W21 | Playback device selection, adjustable skips, percentage seek, A-B repeat and sleep timer | **Limited**: the existing voice reader has bounded configurable skips, percentage/time seek, explicit A-B repeat and sleep controls through the existing WavePlayer; generation checks revoke old timers and queued commands. Shared controller/reader tests and actual POSIX PCM/ffplay lifecycle tests pass. Native Windows/assistive-technology acceptance and a playback-device selector remain pending. Offline tempo conversion is not live playback speed control. See `PLAYBACK_NAVIGATION.md`. | S, A; R1, R2, R3 |
| W22 | Preserve originals, recover drafts/jobs and export without overwriting | **Supported** within the current retained-state/backup contracts. Reading copies, source bookmarks and managed Studio inputs have recovery tests. External engines, credentials, unlicensed assets and user-selected output directories are not automatically migrated or executed on restore. | R, B, `tests/storage/test_studio_input_backup.py`; V3-06 |

## All requested input formats

The 28 named V3 document families are retained below, plus common raster images.
**Direct** means library import with original retention. **Derivative** means
Convert documents produces a new UTF-8 text file and a provenance receipt; it
does not register its text as an independently verified source. Detailed bounds
are in [direct import formats](DOCUMENT_FORMATS_V3.md) and
[conversion formats](document_formats.md).

| ID | Requested input | Status / available route | Fidelity or missing boundary |
| --- | --- | --- | --- |
| F01 | TXT | **Supported:** direct and derivative | Direct attestation requires exact UTF-8. Conversion supports BOM/explicit supported encodings without replacement decoding. |
| F02 | PDF | **Limited:** direct and derivative | Real physical indexes; native text and installed local OCR remain subject to source review. Scanned PDF OCR uses the library path, not a fabricated plain-text extraction. |
| F03 | DOCX | **Limited:** direct and derivative | Reviewed ordered paragraphs/simple tables; unsupported/hidden/complex structures require review. No invented physical pagination. |
| F04 | EPUB | **Limited:** direct and derivative | Supported static spine/XHTML content, bounded package parsing, logical locations; no DRM removal or complete visual-layout claim. |
| F05 | HTML | **Limited:** direct and derivative; HTM/XHTML aliases | Static text; no script, remote-resource or browser execution. CSS visibility/layout is not reproduced. |
| F06 | MD | **Supported:** direct and derivative | Literal Markdown retains notation. Optional Pandoc plain-text conversion has a separate installed-runtime boundary. |
| F07 | EML | **Limited:** direct and derivative | Supported readable body/alternative selection; attachments are not executed or silently incorporated. |
| F08 | FB2 | **Limited:** direct and derivative | Bounded ordered XML body text; images and visual layout are outside the text representation. |
| F09 | FB3 | **Limited:** direct and derivative | Declared supported package body, not arbitrary archive members; no layout reconstruction. |
| F10 | ODT | **Limited:** direct and derivative | Supported document text; no macros/active objects. Unsupported revision/visibility structures require review. |
| F11 | ODS, XLSX | **Limited:** direct and derivative | Stored text/value subsets in declared sheet order; no formula evaluation or spreadsheet rendering. Direct import rejects ambiguous formula/visibility cases. |
| F12 | ODP, PPTX | **Limited:** direct and derivative | Supported slide text and declared order; animation/media/layout and visually inferred reading order are not certified. |
| F13 | RTF | **Limited:** optional derivative only | Installed Pandoc with fixed `--sandbox` conversion. No direct library adapter; an absent runtime reports unavailable. |
| F14 | PNG, JPEG, BMP, TIFF, WebP | **Limited:** optional derivative OCR only | Installed Tesseract and explicit language selection, isolated pixel/frame budgets. No direct image-source catalog adapter; OCR accuracy needs review. |
| F15 | AZW, AZW3, MOBI, PDB, PRC, LIT | **Unsupported** | No admitted converter for these six families. calibre is a candidate, not an integrated or qualified engine; no DRM/password bypass. |
| F16 | CHM, DjVu, DOC, PPT, XLS, TCR, WPD | **Unsupported** | No admitted reader for these seven families. A reviewed optional converter and actual offline/fidelity tests are needed; installed LibreOffice or another binary alone is insufficient. |

D and C provide actual extraction/integrity test targets for the enabled rows.
`tests/conversion/test_conversion.py::test_unverified_legacy_formats_are_not_falsely_supported`
covers refusal of unsupported families. Its Pandoc/Tesseract cases distinguish
actual execution from explicit unavailable behavior; a passing unavailable
branch is not evidence that an optional engine ran. DRM/password circumvention,
active-content execution and inferred quotation approval remain unsupported by
design, independently of future format adapters.

## Concrete work still required

These are source gaps, not merely missing private models or manual acceptance:

1. **W07/W08:** qualify the implemented Voice-reader/Studio pronunciation and optional Hunspell review with exact native Windows engines and human accessibility checks. The separate Hunspell adapter and dictionary selection now have actual POSIX native evidence; keep cancellation, source invariance, licensed dictionary provenance and saved-reading recovery as required gates.
2. **W10–W12:** the shared split/name preview, durable document batches, reviewed Studio admission and CLI are implemented; see [document batches](DOCUMENT_BATCHES.md). Remaining extensions are per-book preset mixtures, authored chapter/bookmark import where real positions exist, and reviewed audio artifact naming without changing canonical Studio receipts.
3. **W15/W16:** M4B title/scene chapters and measured line LRC use the existing verified audio path; see [actual chapter publication](AUDIOBOOK_CHAPTERS.md). The [reviewed tag/cover workflow](AUDIO_METADATA_COVER.md) adds explicit portable metadata and local artwork without changing source audio or Studio receipts. Qualify the native artifact, third-party players and human accessibility separately; do not infer word alignment.
4. **W06/W19/W20/W21:** complete tabbed editing/presentation settings, optional clipboard watching, tray/global keys and playback-device selection using the existing native UI. Bounded Find/Replace, explicit clipboard reading, keymap interchange, configurable skips/seek, A–B repeat and sleep are implemented; native acceptance remains separate. Include keyboard focus, cancellation and screen-reader coexistence checks.
5. **F15/F16:** review narrow existing converter integrations before implementation; prove offline behavior, bounded parsing, fidelity/provenance and no source modification for each admitted family. Keep unavailable formats visible until this is done.
6. **W03/W04/W14/W18:** per-language reader defaults/favorites and document batch CLI are implemented. Expose the remaining supported synthesis controls, voice installation management and audio concatenation/batch paths; retain explicit unavailable states for SAPI4, unqualified voice/model pairs and absent codecs.

Completion requires both the relevant source change and its exact-build
acceptance. Native keyboard/device evidence, human NVDA/listening evidence and
engine/model/voice/output rights are recorded separately. This checklist closes
the traceability requirement; it does not close the unfinished workflows.

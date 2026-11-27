# Finite reading and synthesis workflow checklist

This inventory covers 28 requested workflows under V3-03 and V3-04. Its scope
comes from the [approved technical project](UNIVERSAL_SCOPE_V3_AUDIO_AGENT.md)
and [finalization decisions](UNIVERSAL_SCOPE_V3_FINALIZATION.md). Official
Balabolka documentation, listed below, supplies comparison workflows. This is
a finite acceptance list, not a promise of every feature of another application.

**Implemented** describes the named source capability. **Limited** means that
only the stated subset exists. **Pending** identifies a concrete missing route.
These labels do not establish actual execution on the final installed wheel,
model quality, language coverage, human listening, NVDA behavior, publisher
signing or redistribution permission. Each linked gate is a verification entry
point; its result must be bound to the exact source and installed artifact.

## Requested workflows

| ID | Workflow | Status | Existing producer and gate | Current boundary |
| --- | --- | --- | --- | --- |
| RW-01 | Open supported books/documents | Limited | [Importer](../src/carr_library/extract/document_formats.py), [explicit streamed TXT](LARGE_SOURCE_PROFILES.md); [workflow gate](../tests/e2e/test_extended_document_workflows.py), [large-source gate](../tests/e2e/test_streamed_source_import.py) | Ordered text subsets preserve original bytes. Ordinary intake stays at 64 MiB; an explicit UTF-8 TXT profile permits a selected limit up to 128 MiB and bounded logical fragments. This is not multi-gigabyte/PDF-scale or complete visual-format qualification. |
| RW-02 | Read a source range, selection or text from the cursor | Implemented | [Source range](../src/carr_library/speech/source.py), [reader](../src/carr_library/ui/speech_dialog.py), [navigation](../src/carr_library/ui/reader_navigation.py); [source gate](../tests/speech/test_source.py), [native navigation](../scripts/windows_reader_navigation_smoke.py) | Source authorization and retained-source anchors remain separate from reading edits. Explicit word/line/sentence/paragraph navigation uses the reading copy. Word highlighting and unattended whole-book navigation are separate requirements. |
| RW-03 | Read typed or pasted text | Implemented | [Native reader](../src/carr_library/ui/speech_dialog.py), [desktop/clipboard owner](../src/carr_library/ui/desktop_integration.py); [reading gate](../tests/speech/test_reading.py), [native desktop gate](../scripts/windows_desktop_integration_smoke.py) | Independent reading copies use explicit native text editing. Opt-in clipboard watching retains an exact in-memory snapshot; a separate explicit command opens it. Startup never enables watching, global keys, tray hiding or speech. Clipboard derivatives have no quotation authority. |
| RW-04 | Discover SAPI5 and registered local voices | Limited | [Speech registry](../src/carr_library/speech/runtime.py), [configured registry](../src/carr_library/runtime_settings.py), [voice preferences](VOICE_PREFERENCES.md); [registry gate](../tests/speech/test_registered_studio_voices.py), [preference gate](../tests/speech/test_voice_preferences.py) | Favorites, exact-language defaults and named rate/volume profiles retain exact voice IDs. Explicit Use checks fresh availability and the current reader registry; unavailable choices are reported without substitution or autoplay. Saved reader choices keep their priority. Bitness, installed assets and language/voice quality remain separate boundaries. |
| RW-05 | Use SAPI4 or a separate Speech Platform route | Pending | [Current registry](../src/carr_library/speech/runtime.py); [contract gate](../tests/speech/test_runtime.py) | No qualified SAPI4 helper or distinct Speech Platform adapter is delivered. SAPI5 success does not establish legacy-engine parity. |
| RW-06 | Change supported rate, volume and pitch | Limited | [Speech settings](../src/carr_library/speech/runtime.py), [native voice options](../src/carr_library/ui/reader_synthesis.py), [Studio facade](../src/carr_library/speech/studio_voice_adapter.py), [user controls](VOICE_PREFERENCES.md#native-voice-pitch-and-punctuation); [reader forwarding gate](../tests/ui/test_reader_synthesis.py), [passive recovery gate](../tests/storage/test_reader_synthesis_backup.py), [facade gate](../tests/speech/test_registered_studio_voices.py) | The reader retains rate/volume and adds opt-in native pitch (0–99 on the engine's scale) and punctuation reading through its existing synthesis worker. Unsupported retained requests block Read/Save WAV until an explicit compatible voice or reset; no substitution occurs. Apply confirms playback stop, and tabs/recovery retain settings passively. Named rate/volume profiles keep their existing contract. Playback pitch/time stretching remain separate. The new native-options slice is source-complete with focused execution pending; engine and voice quality are not established by these controls. |
| RW-07 | Play, pause, resume, stop and seek | Implemented | [Existing WavePlayer](../src/carr_library/speech/player.py); [transport gate](../tests/speech/test_player_controls.py), [OPEN ownership gate](../tests/speech/test_player_transport.py), [native playback and queue](../scripts/windows_reader_player_smoke.py) | MCI and the existing waveOut route report native Windows position; ffplay estimates position. The exact 68777f9f source and installed probes passed both the short-name MCI route and long retained queue paths through waveOut, including pause, seek and owned cleanup. Physical-device, listening and sealed-installation acceptance remain separate. |
| RW-08 | Repeat an A/B range and use a sleep timer | Implemented | [Reader controls](../src/carr_library/ui/reader_playback.py), [queue](../src/carr_library/speech/playlist.py); [boundary gate](../tests/speech/test_player_controls.py), [installed probe](../scripts/windows_reader_controls_smoke.py), [native playback and queue](../scripts/windows_reader_player_smoke.py) | Explicit repeat stops at B. Sleep counts monotonic elapsed time including pauses. Lifecycle boundaries disarm both; they are never restored automatically. The explicit playlist reuses the current player. Source and installed A/B, sleep and queue controls passed at the focused checkpoint below; sealed-installation and human listening acceptance remain separate. |
| RW-09 | Choose the reader output device | Implemented | [Output identity](../src/carr_library/speech/output_devices.py), [existing WavePlayer](../src/carr_library/speech/player.py), [native selector](../src/carr_library/ui/reader_output.py); [routing probe](../scripts/windows_reader_output_smoke.py) | Windows exposes an explicit per-reader output with an opaque endpoint ID, revalidation and paused-position retention. Selection disarms repeat/sleep and does not start playback or change system/NVDA output. Linux retains its declared default-device boundary; physical-device/listening acceptance is separate. |
| RW-10 | Retain position and book bookmarks | Limited | [ReaderResume](../src/carr_library/speech/session.py), [book state](../src/carr_library/reader/reading_state.py); [session gate](../tests/speech/test_session.py), [book-state gate](../tests/reader/test_reading_state.py) | Text/source identity and playback position are separate. Recovery never starts speech, repeat or a timer automatically. |
| RW-11 | Edit a separate copy with undo/redo and original comparison | Implemented | [ReadingDocument](../src/carr_library/speech/reading.py), [reader history controls](../src/carr_library/ui/speech_dialog.py); [reading gate](../tests/speech/test_reading.py), [history integration gate](../tests/ui/test_reader_preparation.py), [native preparation gate](../scripts/windows_reading_preparation_smoke.py) | Buttons and fixed Ctrl+Z/Ctrl+Y use the same reading history when the current reader editor has focus. Nested tool fields retain their own native editing. Source/busy checks and confirmed playback stop precede a history change. Original text and history survive save/reopen; legacy anchor import requires explicit Save As, and reading edits do not approve quotations. P3 source/installed native preparation confirms Ctrl+Z; sealed-installation evidence remains separate. |
| RW-12 | Find and replace text | Implemented | [Reading tools](../src/carr_library/speech/reading_tools.py), [native tools](../src/carr_library/ui/reading_tools.py); [native gate](../scripts/windows_reading_tools_smoke.py) | Native next/previous, case/whole-word, selection/all, literal or bounded isolated-regex preview, replace one/all and undo use the existing reading copy. Source and selection identity are rechecked before applying. Another application's undocumented special-code dialect is not promised. |
| RW-13 | Apply pronunciation substitutions | Implemented | [Shared reading tools](../src/carr_library/speech/reading_tools.py), [native dictionary](../src/carr_library/ui/reading_tools.py); [reading-tools gate](../tests/speech/test_reading_tools.py) | The reader exposes scoped, ordered literal/regex rules with preview and explicit acceptance. Studio retains its separate scoped literal dictionary and source-linked narration workflow. Existing carr.pronunciations.v1 import/export and original preservation remain. Voice/language scopes do not establish pronunciation quality or support for every external dictionary dialect. |
| RW-14 | Apply bounded regex pronunciation rules | Limited | [Isolated regex worker](../src/carr_library/speech/regex_worker.py), [reading service](../src/carr_library/speech/reading.py); [reading gate](../tests/speech/test_reading.py) | Reading rules have time/output/cancellation limits. Studio's distinct literal dictionary stays literal; no implicit phoneme or regex conversion occurs. |
| RW-15 | Spell checking | Implemented | [Windows spelling](../src/carr_library/speech/spelling.py), [owned COM worker](../src/carr_library/speech/spelling_worker.py), [native correction preview](../src/carr_library/ui/reader_spelling.py); [native gate](../scripts/windows_spelling_smoke.py) | The user explicitly chooses an installed Windows dictionary. Bounded isolated checks validate UTF-16 positions, preview and apply corrections through the existing document with undo. Missing dictionaries are reported; no dictionary is downloaded or substituted. Language quality and human NVDA acceptance are separate. |
| RW-16 | Normalize a derivative for reading | Limited | [ReadingDocument](../src/carr_library/speech/reading.py), [explicit preparation](../src/carr_library/ui/reader_preparation.py); [preparation gate](../tests/speech/test_preparation.py), [native gate](../scripts/windows_reading_preparation_smoke.py) | Whitespace/optional hyphenation cleanup and reviewed English/Ukrainian/Slovak digit, ISO-date-field and symbol naming change only the derivative. Preview and explicit Apply bind the exact source and selection. Natural grammar, stress, punctuation restoration and inferred pronunciation remain separate capabilities. |
| RW-17 | Assign voices to roles and passages | Implemented | [Typed projects](../src/carr_library/audio/projects.py), [director](../src/carr_library/audio/director.py); [director gate](../tests/audio/test_director.py) | Existing saved scenes retain [ProductionService](../src/carr_library/studio/render.py). Arbitrary document tags remain data, not executable instructions. |
| RW-18 | Insert silence and position scenes/voices | Implemented | [Timeline mixer](../src/carr_library/audio/render.py), [projects](../src/carr_library/audio/projects.py); [real render gate](../tests/audio/test_render.py) | Explicit pauses, offsets, pan, fades and typed effects use measured local audio. Engine prosody is a separate capability. |
| RW-19 | Long production with progress, cancellation and resume | Limited | [Director](../src/carr_library/audio/director.py), [scene service](../src/carr_library/studio/render.py); [recovery gate](../tests/audio/test_project_recovery_boundaries.py), [production gate](../tests/studio/test_production_hardening.py) | Content/runtime-bound chunks control reuse. Generated/repeated 1/3/10-hour fixtures show streaming assembly, not original multi-hour neural quality. |
| RW-20 | Export audio with chosen encoding parameters | Limited | [Renderer/converter](../src/carr_library/audio/render.py), [batch conversion](../src/carr_library/audio/conversion_batch.py), [metadata controls](../src/carr_library/ui/audio_metadata_dialog.py); [render gate](../tests/audio/test_render.py), [metadata probe](../scripts/windows_audio_metadata_smoke.py) | Encoders depend on the verified installed runtime. Reviewed batch settings and supported metadata changes publish new outputs; originals remain unchanged. Reader Save WAV stays explicit. Every external naming template, tag dialect or codec is not implied. |
| RW-21 | Produce synchronized text and chapter outputs | Limited | [Director](../src/carr_library/audio/director.py), [publication service](../src/carr_library/studio/publishing.py), [DAISY interchange](../src/carr_library/studio/daisy.py); [director gate](../tests/audio/test_director.py), [publication gate](../tests/studio/test_publishing.py), [DAISY gate](../tests/studio/test_daisy_publishing.py) | Book and scene publication use measured segment/chapter boundaries. EPUB/DAISY format validation is a required gate, independent of actual reader interoperability. Segment timing is not word/phoneme alignment, acoustic transcription or Braille qualification. |
| RW-22 | Controlled command-line production | Limited | [Audio CLI](../src/carr_library/audio/cli.py), [scene CLI](../src/carr_library/studio/cli.py) | Existing commands reuse these services and typed projects. Their flags are not another product's command-line contract. |
| RW-23 | Batch import or conversion | Limited | [Conversion service](../src/carr_library/conversion/service.py), [Import Center](../src/carr_library/ui/import_center.py), [audio batch review](../src/carr_library/ui/audio_conversion_dialog.py), [Book batch](../src/carr_library/studio/book_batch.py); [conversion gate](../tests/conversion/test_conversion.py), [audio batch gate](../tests/ui/test_audio_conversion_dialog.py) | Explicit captured inputs, settings, output folders and reviewed per-item plans use the existing owned workers. Completed outputs survive cancellation; resume requires matching identities. No unattended watcher or automatic external application is implied. |
| RW-24 | Customize keyboard commands | Limited | [Command registry](../src/carr_library/keybindings.py), [desktop integration](../src/carr_library/ui/desktop_integration.py); [keyboard gate](../tests/ui/test_keyboard_settings.py), [native desktop gate](../scripts/windows_desktop_integration_smoke.py) | One inventory supplies conflict checks, persistence and palette search. Ctrl+Z/Ctrl+Y remain fixed editor commands outside configurable bindings. Explicit global hotkeys, clipboard actions and tray/hide controls are implemented through the same desktop owner. Conflicts are visible; registration is not automatic after restart/restore. Human NVDA and actual window/device combinations remain separate acceptance. |
| RW-25 | Customize reader presentation | Limited | [Appearance controls](../src/carr_library/ui/reader_presentation.py), [reading tabs](../src/carr_library/ui/reader_tabs.py), [focus-reading controls](../src/carr_library/ui/reading_focus.py); [native appearance gate](../scripts/windows_reader_presentation_smoke.py), [tab gate](../tests/ui/test_reader_tabs.py), [focus gate](../tests/ui/test_reading_focus.py) | Font, foreground/background and 50–400% zoom have preview, explicit Apply/Cancel/Reset, durable preferences and backup. Tabs and bounded focus-reading have source implementations. Presentation preserves source identity; timers/playback are not restored automatically. Final-wheel/native/NVDA and per-engine acceptance remain independent. |
| RW-26 | Accessible and optional audible progress | Implemented | [Shared controller](../src/carr_library/ui/operation_progress.py); [progress gate](../tests/ui/test_operation_progress.py), [native probe](../scripts/windows_progress_smoke.py) | Notifications default to Off, preserve focus and respect actual playback/quiet holds. Ctrl+Alt+G queries progress. Manual NVDA/listening are independent checks. |
| RW-27 | Install, repair, uninstall and update | Limited | [Offline installer](../packaging/install_windows.py); [bundle gate](../tests/packaging/test_windows_bundle.py) | Verified installation and an optional unsigned launcher do not establish a signed standard installer, automatic repair/uninstall or signed updater. Transfer eligibility remains component-specific. |
| RW-28 | Translate with an explicitly selected service | Limited | [Translation service](../src/carr_library/assistant/translation.py), [workspace](../src/carr_library/ui/translation_dialog.py); [translation gate](../tests/assistant/test_translation.py) | Selected text uses existing provider/permission/budget controls. Translation is a derivative; live-provider, source-selection and language-quality acceptance remain separate. |


## Current integration checkpoint — 2026-11-27

The rows above describe source capabilities and retain their original finite scope.
The current technical authority is finalization blob
`db8adbdc483a03c262885928c7ed214ca70bbaa7` with audio/agent blob
`d0b2761dd22b33233b311d4800230fb47f5d3d8b`. Exact source checkpoints, fixture
corrections, gate selections and failures are recorded in the
[integration ledger](INTEGRATION_REUSE_2026-11-27_READING_AUDIO.md).

The focused native playback checkpoint is
`68777f9f36b142ed1ce847ee47d82e20d0bdd132`. The
[completed source and installed native run](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46075231974)
(job `140761298828`) passed the reader/player probe with **198 checks in each
mode**, and the recorded-EPUB/DAISY probe completed **65 checks in each mode**
and reached its final complete state. The reader probe exercised actual native A/B, sleep, skip, listening-copy
conversion and volume controls, plus retained-queue pause, save, passive reopen,
direct destruction and owned Close. It used synthetic PCM and the explicitly
reviewed output endpoint. Both the short-name MCI route and the existing waveOut
route for long retained paths were exercised. The EPUB/DAISY lanes exported,
imported and played recorded PCM and completed native Stop/Close.

These results apply to that exact source and installed wheel in the focused
job. They do not qualify the complete aggregate, a sealed installation, capture,
voice-model quality, audible listening or human NVDA acceptance.

Earlier P3 `10b9a4e36b6415cdf9a21fbc36bcd9ef2b72420a` remains the source identity
for its separately recorded preparation and preference checks. In the
[P3 preflight run](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46063639311),
Windows job `140723613596` completed source and installed reading preparation
(step 10) and voice preferences (step 11). Preparation exercised the actual
Ctrl+Z path; the preferences probe used native controls and synthetic voice
metadata. Those completed steps are historical evidence for their own source,
rather than an aggregate or sealed-installation result.

The actual P2 Ctrl+Z, queue MCI 304 and recorded-EPUB playback failures remain
failed predecessor receipts. The successful focused playback run above records
new execution of the corrected queue and EPUB paths. It does not relabel the
old runs or establish one shared cause for their failures. The complete Windows
gate at `94ecfdd4` also remains a failed predecessor, with no eligible wheel
from that run.

Successful appearance, output or editing checks cover only their recorded cases
and source identity. This inventory does not transfer a predecessor PASS to
another candidate, qualify a physical device/model, establish 135-language or
performance coverage, or close human Windows 11/NVDA acceptance.

## Requested input formats

The importer fidelity rules are in
[DOCUMENT_FORMATS_V3.md](DOCUMENT_FORMATS_V3.md). Import and optional conversion
must retain their own original/derivative identities.

| Input family | Current route | Boundary |
| --- | --- | --- |
| TXT, PDF | Existing library import; PDF native-text/OCR paths | Exact UTF-8 attestation and PDF physical-page/OCR review still apply. |
| DOCX, EPUB | Supported ordered-text import | Logical locations are not printed pages; unsupported package content is rejected. |
| EML, HTML, MD | Bounded text-oriented import | No attachment execution, browser scripting, remote resources or CSS-layout reconstruction. |
| FB2, FB3 | Supported XML book bodies | Images and full visual layout are not reconstructed. |
| ODT, ODS, ODP, PPTX, XLSX | Supported Office XML text subsets | No formula/macro execution; order, hidden-content rules and unsupported structures are checked. |
| RTF and external conversion | Explicitly registered local converter where the adapter advertises support | Actual executable/output/fidelity validation is required. Converter support does not establish native-import support. |
| AZW/AZW3, MOBI, PDB/PRC, LIT | No qualified native importer in this checklist | No DRM bypass or unrestricted encrypted-document access. |
| CHM, DjVu, TCR, WPD, legacy DOC/PPT/XLS | No qualified native importer in this checklist | No suffix-renaming workaround or automatic Office/LibreOffice execution. |
| Supported raster images/TIFF | Explicit OCR conversion route where available | The actual selected engine/model and image subset must qualify. OCR text remains unverified. |

The existing 64 MiB original-file boundary and component resource budgets remain.
This inventory does not establish multi-GB single-document or 50,000-book
performance. Those targets need a declared corpus and machine profile plus
measured memory, disk, latency, cancellation and recovery results.

## Repeat and sleep acceptance

These controls extend the existing speech player. They add no transport, job
store or document format. A/B points are validated against the opened PCM WAV's
frame duration; setting them alone does not enable repeat. MCI/ffplay interval
endpoints and the existing waveOut frame limit stop each interval at B. A delayed
GUI poll may delay the next iteration but must not extend the previous interval.

Sleep uses monotonic elapsed time from the explicit user action, including
pauses. The responsive GUI checks every 100 ms; this is not a hard-real-time
stop guarantee. Expiry clears timer/repeat, releases output while retaining a
paused position when possible, and requires explicit F7 resume. New audio,
Stop/F8, cancellation, changed library/source authorization and Close invalidate
automatic starts. Reading projects/recovery do not persist armed controls.

Portable tests read actual owned PCM headers and model both device protocols.
The installed Windows probe remains a separate gate:

~~~console
python -I -B scripts/windows_reader_controls_smoke.py --report ci-results/reader-controls.json
~~~

It verifies installed source hashes, real WinMM, native Tab/Space/Enter/F7/F8,
repeat boundaries, pause-inclusive sleep, explicit resume, scope/close cleanup,
shared progress registration and unchanged fixture PCM/reading text. The fixture
is a synthetic tone, not claimed voice inference. Human listening, NVDA and a
device matrix remain NOT_RUN unless independently recorded.

## Reader editing and presentation acceptance

The source routes above share the current reading document, undo history,
background executor, source authorization and process owner. Ctrl+Z/Ctrl+Y
dispatch to that same history only while the active reading editor has focus;
they do not replace editing in search, dictionary, spelling or preparation
fields. Find/replace and pronunciation reuse the existing isolated
regular-expression worker. Spelling
uses the installed Windows Spell Checker API in a bounded owned child. Output
selection extends the same Windows player. Appearance and voice preferences are
passive state and do not grant engine, device or external-file permission on
restore. Voice preferences retain favorites and exact choices; use requires a
fresh current-voice check, and opening or saving them never starts speech.

The required Windows aggregate exercises source and installed forms of
`windows_reading_tools_smoke.py`, `windows_spelling_smoke.py`,
`windows_reader_presentation_smoke.py` and `windows_reader_output_smoke.py`.
It also repeats these controls from the complete sealed installation. The source,
installed and sealed-bundle lanes include `windows_voice_preferences_smoke.py`
and retain `windows_reading_preparation_smoke.py`'s actual Ctrl+Z acceptance.
The preference probe uses synthetic voice metadata to test real
controls, settings and lifetime; it does not qualify a voice model or listening.

The output probe requires an explicitly selected actual enumerated endpoint;
no simulated endpoint can satisfy that native gate. Individual source
checkpoints or the supplementary reader workflow do not qualify a delivery
artifact by themselves.

## Official comparison references

Reviewed 2026-11-27. These references describe workflows only. Their binaries,
assets, voice packages, online utilities and compatibility workarounds were not
adopted.

- [Official product overview](https://www.cross-plus-a.com/balabolka.htm):
  document/clipboard input, voices, substitutions, synchronized text, spelling,
  presentation and keyboard families (RW-01, 03, 04, 06, 13–15, 21, 24–25).
- [Official FAQ](https://www.cross-plus-a.com/bfaq.htm):
  engine compatibility, role changes, pauses, output devices, replacement and
  installation topics (RW-05, 09, 12, 17–18, 27).
- [Official command-line reference](https://www.cross-plus-a.com/bconsole.htm):
  explicit input/output, voice controls, pause/resume, queueing and CLI
  workflows (RW-02, 06–07, 19–23).
- The owner-approved V3 scope supplies additional source integrity, recovery,
  A/B/sleep and progress requirements; these are not attributed to an
  undocumented comparison feature.

PR116 at f6a04859dab0fb1ac506de60bd6d881f267e6667 was reviewed before this
extension. Its finite-inventory and native-endpoint policy structure informed
this work. Its alternate player/store and execution claims were not adopted.

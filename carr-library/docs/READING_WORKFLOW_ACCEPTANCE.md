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
| RW-01 | Open supported books/documents | Limited | [Importer](../src/carr_library/extract/document_formats.py); [workflow gate](../tests/e2e/test_extended_document_workflows.py) | Ordered text subsets preserve original bytes. The format inventory below does not promise full visual or parser compatibility. |
| RW-02 | Read a source range, selection or text from the cursor | Implemented | [Source range](../src/carr_library/speech/source.py), [reader](../src/carr_library/ui/speech_dialog.py); [source gate](../tests/speech/test_source.py) | Source authorization and retained-source anchors remain separate from reading edits. Word highlighting and unattended whole-book navigation are separate requirements. |
| RW-03 | Read typed or pasted text | Implemented | [Native reader](../src/carr_library/ui/speech_dialog.py); [reading gate](../tests/speech/test_reading.py) | Independent reading copies use explicit native text editing. Automatic clipboard watching and tray-triggered speech remain pending. |
| RW-04 | Discover SAPI5 and registered local voices | Limited | [Speech registry](../src/carr_library/speech/runtime.py), [configured registry](../src/carr_library/runtime_settings.py); [registry gate](../tests/speech/test_registered_studio_voices.py) | Observed IDs and capabilities are preserved. Bitness and installed assets matter; discovery does not qualify every language or voice. |
| RW-05 | Use SAPI4 or a separate Speech Platform route | Pending | [Current registry](../src/carr_library/speech/runtime.py); [contract gate](../tests/speech/test_runtime.py) | No qualified SAPI4 helper or distinct Speech Platform adapter is delivered. SAPI5 success does not establish legacy-engine parity. |
| RW-06 | Change supported rate, volume and pitch | Limited | [Speech settings](../src/carr_library/speech/runtime.py), [Studio facade](../src/carr_library/speech/studio_voice_adapter.py); [facade gate](../tests/speech/test_registered_studio_voices.py) | The reader exposes rate and volume. Other controls depend on the selected engine; unsupported nondefault requests fail. Time stretching is identified separately. |
| RW-07 | Play, pause, resume, stop and seek | Implemented | [Existing WavePlayer](../src/carr_library/speech/player.py); [transport gate](../tests/speech/test_player_controls.py) | MCI supplies native Windows position; ffplay estimates position. Device and listening acceptance remain separate. |
| RW-08 | Repeat an A/B range and use a sleep timer | Implemented | [Reader controls](../src/carr_library/ui/reader_playback.py); [boundary gate](../tests/speech/test_player_controls.py), [installed probe](../scripts/windows_reader_controls_smoke.py) | Explicit repeat uses a native B endpoint. Sleep counts monotonic elapsed time including pauses. Lifecycle boundaries disarm both; they are never restored automatically. A general playlist is separate. |
| RW-09 | Choose the reader output device | Implemented | [Output identity](../src/carr_library/speech/output_devices.py), [existing WavePlayer](../src/carr_library/speech/player.py), [native selector](../src/carr_library/ui/reader_output.py); [routing probe](../scripts/windows_reader_output_smoke.py) | Windows exposes an explicit per-reader output with stable interface identity, revalidation and paused-position retention. Selection disarms repeat/sleep and does not start playback or change system/NVDA output. Linux retains its declared default-device boundary; physical-device/listening acceptance is separate. |
| RW-10 | Retain position and book bookmarks | Limited | [ReaderResume](../src/carr_library/speech/session.py), [book state](../src/carr_library/reader/reading_state.py); [session gate](../tests/speech/test_session.py), [book-state gate](../tests/reader/test_reading_state.py) | Text/source identity and playback position are separate. Recovery never starts speech, repeat or a timer automatically. |
| RW-11 | Edit a separate copy with undo/redo and original comparison | Implemented | [ReadingDocument](../src/carr_library/speech/reading.py); [reading gate](../tests/speech/test_reading.py) | Canonical source anchors, original text and edit history survive save/reopen. Legacy anchor import requires explicit Save As; reading edits do not approve quotations. |
| RW-12 | Find and replace text | Implemented | [Reading tools](../src/carr_library/speech/reading_tools.py), [native tools](../src/carr_library/ui/reading_tools.py); [native gate](../scripts/windows_reading_tools_smoke.py) | Native next/previous, case/whole-word, selection/all, literal or bounded isolated-regex preview, replace one/all and undo use the existing reading copy. Source and selection identity are rechecked before applying. Another application's undocumented special-code dialect is not promised. |
| RW-13 | Apply pronunciation substitutions | Implemented | [Shared reading tools](../src/carr_library/speech/reading_tools.py), [native dictionary](../src/carr_library/ui/reading_tools.py); [reading-tools gate](../tests/speech/test_reading_tools.py) | The reader exposes scoped, ordered literal/regex rules with preview and explicit acceptance. Studio retains its separate scoped literal dictionary and source-linked narration workflow. Existing carr.pronunciations.v1 import/export and original preservation remain. Voice/language scopes do not establish pronunciation quality or support for every external dictionary dialect. |
| RW-14 | Apply bounded regex pronunciation rules | Limited | [Isolated regex worker](../src/carr_library/speech/regex_worker.py), [reading service](../src/carr_library/speech/reading.py); [reading gate](../tests/speech/test_reading.py) | Reading rules have time/output/cancellation limits. Studio's distinct literal dictionary stays literal; no implicit phoneme or regex conversion occurs. |
| RW-15 | Spell checking | Implemented | [Windows spelling](../src/carr_library/speech/spelling.py), [owned COM worker](../src/carr_library/speech/spelling_worker.py), [native correction preview](../src/carr_library/ui/reader_spelling.py); [native gate](../scripts/windows_spelling_smoke.py) | The user explicitly chooses an installed Windows dictionary. Bounded isolated checks validate UTF-16 positions, preview and apply corrections through the existing document with undo. Missing dictionaries are reported; no dictionary is downloaded or substituted. Language quality and human NVDA acceptance are separate. |
| RW-16 | Normalize a derivative for reading | Limited | [ReadingDocument](../src/carr_library/speech/reading.py), [cleanup action](../src/carr_library/ui/speech_dialog.py); [reading gate](../tests/speech/test_reading.py) | Deterministic whitespace/optional hyphenation cleanup does not infer stress, intended pronunciation or missing source content. |
| RW-17 | Assign voices to roles and passages | Implemented | [Typed projects](../src/carr_library/audio/projects.py), [director](../src/carr_library/audio/director.py); [director gate](../tests/audio/test_director.py) | Existing saved scenes retain [ProductionService](../src/carr_library/studio/render.py). Arbitrary document tags remain data, not executable instructions. |
| RW-18 | Insert silence and position scenes/voices | Implemented | [Timeline mixer](../src/carr_library/audio/render.py), [projects](../src/carr_library/audio/projects.py); [real render gate](../tests/audio/test_render.py) | Explicit pauses, offsets, pan, fades and typed effects use measured local audio. Engine prosody is a separate capability. |
| RW-19 | Long production with progress, cancellation and resume | Limited | [Director](../src/carr_library/audio/director.py), [scene service](../src/carr_library/studio/render.py); [recovery gate](../tests/audio/test_project_recovery_boundaries.py), [production gate](../tests/studio/test_production_hardening.py) | Content/runtime-bound chunks control reuse. Generated/repeated 1/3/10-hour fixtures show streaming assembly, not original multi-hour neural quality. |
| RW-20 | Export audio with chosen encoding parameters | Limited | [Renderer/converter](../src/carr_library/audio/render.py); [render gate](../tests/audio/test_render.py) | Encoders depend on the verified installed runtime. Reader Save WAV stays explicit; every naming template or metadata field is not implied. |
| RW-21 | Produce synchronized text and chapter outputs | Limited | [Director](../src/carr_library/audio/director.py), [publication service](../src/carr_library/studio/publishing.py); [director gate](../tests/audio/test_director.py), [publication gate](../tests/studio/test_publishing.py) | Measured segment/cue timing differs from word/phoneme alignment. MP3 synchronized-text tags, every subtitle dialect and every external reader require separate support. |
| RW-22 | Controlled command-line production | Limited | [Audio CLI](../src/carr_library/audio/cli.py), [scene CLI](../src/carr_library/studio/cli.py) | Existing commands reuse these services and typed projects. Their flags are not another product's command-line contract. |
| RW-23 | Batch import or conversion | Limited | [Conversion service](../src/carr_library/conversion/service.py), [Import Center](../src/carr_library/ui/import_center.py); [conversion gate](../tests/conversion/test_conversion.py) | Explicit plans, reviewed converters and derivative provenance remain required. No unattended watcher or automatic external application is implied. |
| RW-24 | Customize keyboard commands | Limited | [Command registry](../src/carr_library/keybindings.py), [productivity](../src/carr_library/ui/productivity.py); [keyboard gate](../tests/ui/test_keyboard_settings.py) | One inventory supplies conflict checks, persistence and palette search. System-wide hotkeys and tray automation remain pending. |
| RW-25 | Customize reader presentation | Limited | [Appearance state](../src/carr_library/speech/presentation.py), [native appearance controls](../src/carr_library/ui/reader_presentation.py); [native gate](../scripts/windows_reader_presentation_smoke.py) | Font, foreground/background and 50–400% zoom have preview, explicit Apply/Cancel/Reset, durable typed preferences and backup. Applying appearance preserves text, selection and source identity. Tabs and speed-reading modes remain concrete missing routes. |
| RW-26 | Accessible and optional audible progress | Implemented | [Shared controller](../src/carr_library/ui/operation_progress.py); [progress gate](../tests/ui/test_operation_progress.py), [native probe](../scripts/windows_progress_smoke.py) | Notifications default to Off, preserve focus and respect actual playback/quiet holds. Ctrl+Alt+G queries progress. Manual NVDA/listening are independent checks. |
| RW-27 | Install, repair, uninstall and update | Limited | [Offline installer](../packaging/install_windows.py); [bundle gate](../tests/packaging/test_windows_bundle.py) | Verified installation and an optional unsigned launcher do not establish a signed standard installer, automatic repair/uninstall or signed updater. Transfer eligibility remains component-specific. |
| RW-28 | Translate with an explicitly selected service | Limited | [Translation service](../src/carr_library/assistant/translation.py), [workspace](../src/carr_library/ui/translation_dialog.py); [translation gate](../tests/assistant/test_translation.py) | Selected text uses existing provider/permission/budget controls. Translation is a derivative; live-provider, source-selection and language-quality acceptance remain separate. |

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
frame duration; setting them alone does not enable repeat. Native MCI/ffplay
endpoints stop each interval at B. A delayed GUI poll may delay the next
iteration but must not extend the previous interval.

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
background executor, source authorization and process owner. Find/replace and
pronunciation reuse the existing isolated regular-expression worker. Spelling
uses the installed Windows Spell Checker API in a bounded owned child. Output
selection extends the current MCI player. Appearance preferences are passive
state and do not grant engine, device or external-file permission on restore.

The required Windows aggregate exercises source and installed forms of
`windows_reading_tools_smoke.py`, `windows_spelling_smoke.py`,
`windows_reader_presentation_smoke.py` and `windows_reader_output_smoke.py`.
It also repeats these controls from the complete sealed installation. The output
probe requires an explicitly selected actual enumerated endpoint; no simulated
endpoint can satisfy that native gate. Individual source checkpoints or the
supplementary reader workflow do not qualify a delivery artifact by themselves.

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

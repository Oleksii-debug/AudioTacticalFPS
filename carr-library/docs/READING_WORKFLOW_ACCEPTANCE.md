# Bounded reading and synthesis workflow acceptance

Recorded 2026-11-27. Source reviewed: `6d8fdfd57bb54798ad3684e1005dafbf37fe08b4`;
diagnostic successor: `57790e45576b797d515446f73293ae0ab187bf11`.
The requirements remain [V3](UNIVERSAL_SCOPE_V3_AUDIO_AGENT.md) and
[the current finalization decisions](UNIVERSAL_SCOPE_V3_FINALIZATION.md).

This finite inventory makes the requested familiar reading/synthesis workflows
reviewable. It does not promise identical behavior to every current or future
feature of another product. The official references describe comparison
workflows only; their application code, installers, assets and branding are not
included.

## Evidence dimensions

Each row names the existing producer and its component gate. A linked test is a
verification entry point, not a statement that the test passed on the current
commit. Exact observed runs and failures are recorded in
[the integration checkpoint](INTEGRATION_CHECKPOINT_2026_11_27.md).

`REQUIREMENT`, `SOURCE_REVIEWED`, `ADAPTER_PRESENT` and `INTEGRATED`
describe source/integration readiness. `EXECUTED`, `QUALIFIED` and
`DISTRIBUTION_CLEARED` require independent, exact-artifact evidence and are not
implied by those source states. A qualified runtime also needs its engine/model,
language, hardware and rights identities. None of this table establishes manual
Windows 11/NVDA or listening acceptance.

## Requested workflows

| ID | Scenario | Scope | Source status | Existing producer and gate | Limit or remaining dependency | Reference |
| --- | --- | --- | --- | --- | --- | --- |
| RW-01 | Open readable documents | V3-01 | INTEGRATED, limited | [extract/document_formats.py](../src/carr_library/extract/document_formats.py); [component gate](../tests/e2e/test_extended_document_workflows.py) | The supported extraction subsets retain originals and logical locations; see the format table below. | R1 |
| RW-02 | Read selected text, the current page or from the cursor | V3-03 | INTEGRATED, limited | [reader/live_reader.py](../src/carr_library/reader/live_reader.py); [component gate](../tests/reader/test_live_reader.py) | Selection uses actual native prefix text so CRLF and UTF-16 positions cannot silently select different source bytes. Whole-book continuous navigation remains separate. | R3 |
| RW-03 | Read an independent pasted or typed text | V3-02/03 | INTEGRATED, limited | [ui/narration_editor.py](../src/carr_library/ui/narration_editor.py); [component gate](../tests/reader/test_narration.py) | Create an independent reading copy and use the existing production service. Clipboard watching and automatic clipboard speech are not implemented by pasting text. | R1 |
| RW-04 | Discover installed SAPI5 voices | V3-03 | INTEGRATED, limited | [audio/engines.py](../src/carr_library/audio/engines.py); [component gate](../tests/audio/test_engines.py) | The registry exposes observed voice capabilities. Earlier actual Windows SAPI evidence does not qualify every installed voice or language. | R2 |
| RW-05 | SAPI4 and Speech Platform compatibility | V3-03 | REQUIREMENT | [audio/engines.py](../src/carr_library/audio/engines.py); [component gate](../tests/audio/test_engines.py) | No qualified SAPI4 helper or Speech Platform route is present in this checkpoint. Do not substitute SAPI5 success for legacy compatibility. | R2 |
| RW-06 | Change supported rate, pitch and volume | V3-03 | INTEGRATED, limited | [reader/live_reader.py](../src/carr_library/reader/live_reader.py); [component gate](../tests/reader/test_live_reader.py) | The selected voice must advertise the requested control; unsupported nondefault controls are refused. Playback pitch/time stretching is a different requirement. | R3 |
| RW-07 | Play, pause, resume, stop and seek | V3-03/05 | INTEGRATED, limited | [audio/player.py](../src/carr_library/audio/player.py); [component gate](../tests/audio/test_player.py) | Shared PCM playback has guarded file identity and explicit position semantics. Actual device and listening acceptance remain separate. | R3 |
| RW-08 | Queue tracks, repeat a range and set a sleep timer | V3-03/05 | INTEGRATED, limited | [audio/player.py](../src/carr_library/audio/player.py); [component gate](../tests/audio/test_player.py) | Playlist, A–B loop and sleep controls reuse the same player. They do not prove continuous source-book scheduling. | Scope |
| RW-09 | Choose an audio output device | V3-03/05 | ADAPTER_PRESENT | [audio/playback.py](../src/carr_library/audio/playback.py); [component gate](../tests/audio/native_playback_probe.py) | Only actual backend-supported devices may be selected. A backend identifier or absent device is not device acceptance. | R2 |
| RW-10 | Preserve a reading position and bookmarks | V3-03/06 | INTEGRATED, limited | [reader/reading_state.py](../src/carr_library/reader/reading_state.py); [component gate](../tests/reader/test_reading_state.py) | Text locations and playback state are separately typed and bound to source identity; native and backup checks remain required. | Scope |
| RW-11 | Edit a separate reading copy with undo/redo | V3-02/03 | INTEGRATED | [reader/narration.py](../src/carr_library/reader/narration.py); [component gate](../tests/reader/test_narration.py) | Versioned edits, original comparison, explicit Save/Save As and recovery drafts do not change quotation authority. | Scope |
| RW-12 | Find and replace literal text | V3-02/03 | INTEGRATED | [ui/narration_editor.py](../src/carr_library/ui/narration_editor.py); [component gate](../tests/ui/test_narration_editor.py) | Preview and apply exact, case-sensitive replacements. Regex editing is a separate row. | R2 |
| RW-13 | Apply a literal pronunciation dictionary | V3-02/03 | INTEGRATED, limited | [reader/narration.py](../src/carr_library/reader/narration.py); [component gate](../tests/reader/test_narration.py) | Bounded longest-match replacement is non-cascading. The current language field is descriptive; it is not automatic language detection or a voice-scoped override. | R1 |
| RW-14 | Preview bounded regular-expression substitutions | V3-02/03 | INTEGRATED, limited | [reader/regex_preview.py](../src/carr_library/reader/regex_preview.py); [actual child-process gate](../tests/reader/test_regex_preview.py); [editor gate](../tests/ui/test_narration_regex_dialog.py) | Explicit preview and apply reuse the versioned reading-copy editor. The isolated worker has time, match and byte budgets; originals and quotation authority remain unchanged. Installed Windows and manual NVDA acceptance are separate. See the bounded behavior below. | R1 |
| RW-15 | Spell checking with reviewed dictionaries | V3-02/03 | REQUIREMENT | [reader/narration.py](../src/carr_library/reader/narration.py); [component gate](../tests/reader/test_narration.py) | Hunspell/OS spell checking, dictionary rights and correction-preview integration still require implementation and actual runtime evidence. | R1 |
| RW-16 | Text normalization and word-break cleanup | V3-02 | INTEGRATED, limited | [reader/narration.py](../src/carr_library/reader/narration.py); [component gate](../tests/reader/test_narration.py) | Only spacing and line-ending normalization are currently implemented here. Punctuation, stress, phonemes and linguistic restoration are not inferred. | R2 |
| RW-17 | Assign voices to roles and scripted passages | V3-04/13 | INTEGRATED, limited | [audio/production.py](../src/carr_library/audio/production.py); [component gate](../tests/audio/test_production.py) | Typed scenes and roles feed the existing renderer. Arbitrary third-party voice markup is not accepted as executable instructions. | Scope |
| RW-18 | Insert timed silence and control scene placement | V3-04/13 | INTEGRATED, limited | [audio/models.py](../src/carr_library/audio/models.py); [component gate](../tests/audio/test_production.py) | Explicit timeline values use validated production settings; unsupported engine prosody remains visible. | Scope |
| RW-19 | Render a project with progress and restart recovery | V3-04/06 | INTEGRATED, limited | [audio/production.py](../src/carr_library/audio/production.py); [component gate](../tests/audio/test_production_commit.py) | Content-bound chunks and completion receipts control reuse. Many-hour assembly fixtures are not many-hour original neural generation. | Scope |
| RW-20 | Export audio with selected formats and parameters | V3-04 | INTEGRATED, limited | [audio/ffmpeg.py](../src/carr_library/audio/ffmpeg.py); [component gate](../tests/audio/test_encoded_verification.py) | Only the verified local encoder's supported containers/codecs qualify. Full decoded duration and format checks remain required. | Scope |
| RW-21 | Produce subtitles and synchronized text | V3-04/20 | INTEGRATED, limited | [audio/production.py](../src/carr_library/audio/production.py); [component gate](../tests/audio/test_production.py) | Use generated segment timing and the actual advertised formats. Do not claim phoneme/word alignment, MP3 synchronized tags or all subtitle dialects without separate tests. | R1 |
| RW-22 | Use a controlled command-line workflow | V3-04/09 | INTEGRATED, limited | [__main__.py](../src/carr_library/__main__.py); [component gate](../tests/e2e/test_audio_cli.py) | Existing CLI commands call the same services. This is not command-line option compatibility with another product. | R3 |
| RW-23 | Batch extraction and document conversion | V3-01/04 | INTEGRATED, limited | [conversion/service.py](../src/carr_library/conversion/service.py); [component gate](../tests/conversion/test_conversion.py) | Explicit bounded conversion produces new derivatives with fidelity limits. It is not a general-purpose unattended folder watcher. | R1 |
| RW-24 | Change keyboard shortcuts | V3-03/10 | INTEGRATED, limited | [ui/command_registry.py](../src/carr_library/ui/command_registry.py); [component gate](../tests/ui/test_keyboard_manager.py) | The existing application/context registry handles conflicts and saved mappings. System-wide hotkeys and tray/clipboard automation remain pending. | R1 |
| RW-25 | Customize reader presentation | V3-03/10 | REQUIREMENT, partial | [ui/reading_desk.py](../src/carr_library/ui/reading_desk.py); [component gate](../tests/ui/test_reading_desk_actions.py) | Native readable text is available. Do not infer complete font/color/zoom, tabs or speed-reading modes from an accessible text control. | Scope |
| RW-26 | Show audible and accessible progress | V3-19 | INTEGRATED; native lifecycle retest pending | [ui/operation_progress.py](../src/carr_library/ui/operation_progress.py); [component gate](../tests/ui/probe_operation_progress_native.py) | Actual Windows source f6a0485 passes gauge 99 and verified 100 after the probe event-loop correction, then fails Studio destruction. Captured native IDs and owned-worker settlement now guard disposal; those later corrections require an exact-source native retest. Physical feedback and manual NVDA remain separate. | Scope |
| RW-27 | Install, repair, uninstall and update | V3-18 | INTEGRATED, partial | [packaging/install_windows.py](../packaging/install_windows.py); [component gate](../tests/packaging/test_windows_bundle.py) | Verified offline bundle installation exists. Standard signed installer/repair/uninstall and signed owner-authorized in-place updates are not completed by this installer. | R2 |
| RW-28 | Translate text through an explicitly chosen service | V3-11/20 | INTEGRATED; controlled-provider execution | [assistant/translation.py](../src/carr_library/assistant/translation.py); [translation gate](../tests/assistant/test_translation.py); [native gate](../tests/ui/native_translation_smoke.py) | Google Basic v2 uses the existing credential vault, gateway and budget ledger after explicit request review. Restart receipts, uncertain-send handling, bilingual export, reading copies and validated common backups are integrated. Live authorized API execution and exact-source Windows/NVDA acceptance remain separate; translated text never becomes an original quotation. | Scope |

## Requested input-format inventory

This table distinguishes library import from conversion to a new reading
derivative. The authoritative current parser behavior and fidelity boundaries
remain [DOCUMENT_FORMATS_V3.md](DOCUMENT_FORMATS_V3.md) and
[the existing converter](../src/carr_library/conversion/service.py).

| Requested input | This checkpoint | Qualification boundary |
| --- | --- | --- |
| TXT | Integrated UTF-8 source import and derivative conversion | Exact UTF-8 attestation and bounded original storage apply. |
| PDF | Integrated native-text/OCR paths | Physical-page identity remains distinct from printed labels; OCR does not approve a quote. |
| DOCX, EPUB | Integrated ordered-text subsets | Unsupported content is rejected or reported; logical positions are not printed pages. |
| EML, HTML, MD | Integrated bounded text subsets | Attachments, active content, remote resources and rendered CSS fidelity are not generally reproduced. |
| FB2, FB3 | Integrated supported book-body subsets | Images and visual layout are not reconstructed. |
| ODP, ODS, ODT | Integrated supported Office XML subsets | Import and derivative conversion have explicit differing fidelity reports; formulas/macros are not executed. |
| PPTX, XLSX | Integrated supported Office XML subsets | Relationship order and stored content have bounded validation; no general authoring-application fidelity claim. |
| RTF | Optional reviewed external conversion path | A selected, verified Pandoc installation and its actual output are required; not native library-import parity. |
| AZW, AZW3, MOBI, PDB, PRC, LIT | Pending reviewed adapter | No encrypted/DRM bypass and no extension-renaming workaround. |
| CHM, DjVu, TCR, WPD | Pending reviewed adapter | A file extension in the requested inventory does not establish a usable extractor. |
| Legacy DOC, PPT, XLS | Pending reviewed adapter | No automatic Office/LibreOffice execution. |
| Supported raster images/TIFF | Explicit managed OCR conversion path | Actual selected OCR engine/model and image subset must pass; recognized text remains unverified. |

The 64 MiB original-file limit remains in the existing import component.
Continuation removes selected result/file-count ceilings in named workflows;
it does not prove multi-GB import capacity or a 50,000-book performance target.
Capacity acceptance needs a declared corpus, hardware profile and measured
latency, memory, disk, cancellation and recovery results.

## RW-14: bounded regular-expression preview

The existing reading-copy editor now offers **Preview regular expression** and
**Cancel regex preview**. A request captures the current project, editing text
and physical data-root identity. The standard-library worker computes the
substitution in an isolated child process through the existing bounded process
supervisor; user expressions are never compiled on the UI thread. The user can
read the proposed text, apply it explicitly or cancel. Applying creates an
ordinary reading-copy revision with the existing undo, redo, Save and recovery
behavior. No source text, evidence, quotation approval, persisted dictionary
rule or storage schema is changed by the preview.

The bounded subset uses Python regular-expression and replacement syntax,
including numbered/named backreferences and an explicit case-sensitive or
case-insensitive choice. Its limits are:

| Boundary | Enforced limit |
| --- | --- |
| Native editor worker deadline | 2 seconds; the component API accepts 0.05–10 seconds. |
| Encoded UTF-8 request, including JSON overhead | 8,000,000 bytes. |
| Pattern and replacement | 1,024 and 4,096 Unicode characters respectively. |
| Returned matches and exact UTF-8 JSON reply | 10,000 matches and 1,000,000 bytes. |
| Resulting reading section | The existing 8 MiB text budget. |

Backreference expansion is checked conservatively before allocating each
replacement, so some large expressions are refused even when their actual
output might fit. Cancellation or deadline expiry terminates and reaps the
owned child and produces no accepted preview. Project, text, root or window
changes invalidate late results. The editor also rechecks those bindings after
the nested preview and close decisions before applying or saving anything.

The worker is adapted from existing project source at canonical commit
`184dd83d43f285ce27b6f351e82fee0bbaf19323`, original blob
`630f6d9a6b5a90355125b932cc469fe4d628f883`. The adapter adds capture-expansion
preflight and checks the exact encoded reply, while preserving that worker's
isolated standard-library approach. It adds no third-party regex dependency.

The component gates cover real child execution, Unicode, backreferences,
zero-width matches, catastrophic-pattern termination, cancellation and output
bounds. The [job gate](../tests/ui/test_narration_regex_jobs.py) exercises actual
source imports, immutable citations, explicit revisions and common backup
round trips. The [dialog gate](../tests/ui/test_narration_regex_dialog.py) uses
real workers with explicit native-control doubles for nested decisions,
forced destruction, stale roots and rejected executors. Those doubles are not
native Windows or screen-reader evidence. The extended
[installed Windows reading-copy probe](../scripts/windows_v3_reading_smoke.py)
requires actual named native fields, read-before-apply, undo/redo, Save and
backup restoration; its new regex phase remains **NOT_RUN** until executed on
the exact installed artifact. Manual NVDA remains **NOT_RUN**. This bounded
feature does not establish full editing or pronunciation parity with another
application.

## How a row advances

1. Keep its scenario and limitations stable; add an explicit subcase when a new
   requirement is approved.
2. Implement through the named producer and existing application/command/job
   boundary. Reconcile an upstream candidate before adding another implementation.
3. Exercise the meaningful failure and success paths, including immutable
   source bytes, external changes, cancellation, restart and late UI callbacks.
4. Verify the exact built wheel and installed Windows path. Record unavailable
   models/devices as `NOT_RUN`, not a passing replacement test.
5. Record human keyboard/NVDA and listening results independently, then the
   exact component/profile rights decision. Retain failed and predecessor
   evidence without transferring it to a later SHA.

A pass for literal replacements does not close regex or spell checking.
A pass for installed SAPI does not close SAPI4, private cloning or a language
matrix. A pass for rendering does not close microphone capture, monitoring,
real-time editing, acoustic accuracy or release signing.

## Official comparison references

Reviewed on 2026-11-27:

- **R1:** [Official product overview](https://www.cross-plus-a.com/balabolka.htm):
  document reading, installed voices, pronunciation substitutions, synchronized
  text, presentation and keyboard workflows.
- **R2:** [Official FAQ](https://www.cross-plus-a.com/bfaq.htm):
  engine compatibility, voice controls, output-device selection, editing and
  installation behavior. Product-specific compatibility advice is not adopted
  as Resonance acceptance evidence.
- **R3:** [Official command-line reference](https://www.cross-plus-a.com/bconsole.htm):
  explicit file/manual/clipboard input, voice and output controls, pauses,
  queueing, progress and subtitle workflows.
- **Scope:** owner-approved Resonance requirements above, where this product's
  requested workflow extends beyond those three comparison pages.

No proprietary binary was executed, reverse-engineered or redistributed for
this inventory. RW-14 reuses the project-owned source identified above;
its component execution, native acceptance and distribution evidence remain
separate.

# Bounded familiar reading and synthesis workflow checklist

Reviewed on 2026-11-27 against the primary Resonance implementation at
`2b951ff0a7c8bc1927c48f8817a1c7778ace548e` (tree
`3ec8fba55f3ac4c5b980e15307d979937f38fa3d`). This checklist records the requested
V3-01 through V3-04 workflows. It does not promise complete equivalence with
another application, copy its code, or add every future upstream feature to scope.

The public behavioral references are the official [overview](https://www.cross-plus-a.com/balabolka.htm),
[FAQ](https://www.cross-plus-a.com/bfaq.htm) and [command-line documentation](https://www.cross-plus-a.com/bconsole.htm),
read on that date. These references describe workflows only. Their commands,
assets, binaries, proprietary markup and interface are not included in Resonance.

## Status definitions

**Implemented** means the named path exists and has the referenced application
tests. **Partial** means a specific subset exists while the stated required
remainder is open. **Pending** means the requested workflow is not implemented
in the inspected path. Neither implementation nor a test name proves manual
NVDA, acoustic quality, every engine/language pair or distribution clearance.

## Finite acceptance inventory

| ID / scope | Requested workflow | Current implementation and remaining acceptance |
| --- | --- | --- |
| BW-01 / V3-02,03 | Editable manual text; selection and cursor reading | **Implemented:** `ui/speech_dialog.py` and `speech/reading.py` preserve original text, editable narration, selection/from-cursor reading and explicit execution. Native listening and NVDA remain separate. |
| BW-02 / V3-03 | Local voice discovery and choice | **Partial:** the shared `VoiceRegistry` exposes actual installed voice identities and capabilities. SAPI5 x64 rendered real PCM in the exact Windows V3 run. SAPI4, cross-bitness bridges and other optional engines need their own verified installation. |
| BW-03 / V3-03 | Synthesis rate, volume and voice controls | **Partial:** reader rate/volume controls follow the selected voice capability. Native pitch, timbre and expressive controls are not a universal reader capability; an audio pitch filter does not supply voice-native prosody. |
| BW-04 / V3-03 | Playback, pause/resume, stop and seek | **Partial:** `speech/player.py` and the reader expose playback and time seek. Device selection, A-B repetition, sleep timer and all requested semantic seek modes remain open; ffplay time is approximate. |
| BW-05 / V3-03 | Clipboard reading and monitoring | **Partial:** users can paste into the editable text control. A dedicated scoped clipboard-read/watch workflow is pending; watching must remain opt-in and off by default. |
| BW-06 / V3-03 | Tray controls and global shortcuts | **Pending:** current command registry and remapping cover application contexts. They do not establish a tray menu or operating-system-wide hotkeys. |
| BW-07 / V3-02 | Pronunciation substitutions and dictionaries | **Partial:** `speech/reading.py` supports literal and bounded-process regex rules, language/voice scoping, import/export and undo. The current voice-reader dialog has no pronunciation-rule or dictionary-management controls. Per-language listening remains required. |
| BW-08 / V3-02 | Spelling and language-aware normalization | **Partial:** the native reader exposes reversible whitespace cleanup; hyphen joining exists only in the `ReadingDocument.cleanup(join_hyphenated=True)` backend path. Hunspell integration, general punctuation repair, stress/phoneme annotations and robust number/formula pronunciation remain open. |
| BW-09 / V3-01 | Requested document formats | **Partial:** use the exact matrix below and `DOCUMENT_FORMATS_V3.md`. Recognized suffixes never substitute for actual parser or fidelity evidence. |
| BW-10 / V3-01,04 | Batch extraction and text split/merge | **Partial:** existing Import Center and typed assistant support scoped import batches and text extraction. A complete combined/split extraction UI, all requested boundary choices and naming-pattern workflow are pending. |
| BW-11 / V3-04 | Render selected text or chapters to audio | **Implemented subset:** reader PCM WAV export and Studio chapter/scene render use shared voice services and guarded publication. Real SAPI and three-role WAV/FLAC output were exercised on Windows. Other requested codec/build pairs need individual evidence. |
| BW-12 / V3-04 | Split-and-convert and output naming | **Partial:** Studio schedules scenes/chapters and verifies cached chunks. Bookmarks, arbitrary regex/size/duration split rules and complete editable naming templates are not all exposed as one native workflow. |
| BW-13 / V3-04 | Chaptered outputs and synchronized text | **Partial:** Studio exports chapter files, segment timing and SRT/VTT from measured render segments. LRC, embedded synchronized MP3 text and verified word-level highlighting remain open. Segment timing is not acoustic word alignment. |
| BW-14 / V3-03 | Bookmarks and saved reading position | **Implemented subset:** Reading Desk stores named source locations and validates revision/hash on reopening. It remembers a page/segment, not the NVDA review cursor or every spoken word. See `READING_DESK.md`. |
| BW-15 / V3-03 | Editor save, undo/redo and comparison | **Implemented subset:** reading projects preserve edit history and source mapping; Save As rechecks source access. Multiple tabs, full regex find/replace UI, encoding choices and all font/color/zoom presentation controls remain open. |
| BW-16 / V3-04 | Headless and reusable workflows | **Partial:** typed `Plan`/`Step` execution shares scoped application services; Python APIs and existing library CLI are present. This is not a command-line replacement for every upstream synthesis switch or stdin/stdout audio workflow. |
| BW-17 / V3-03,04 | Multiple voices and explicit pauses | **Partial:** Studio roles/scenes select voice identities and pauses, with actual three-role output. Reliable automatic language switching, every span-level override and all engine-specific controls still need implementation/qualification. |
| BW-18 / V3-03,10 | Accessible controls and progress | **Partial:** named native controls, registered keyboard commands and progress gauges/optional tones exist. Human Windows 11/NVDA listening, interference behavior, complete spoken-progress wiring and all device workflows remain required. |

## Required format inventory

| State in this candidate | Formats | Boundary |
| --- | --- | --- |
| Implemented reading/extraction subsets | TXT, PDF, DOCX, EPUB, EML, HTML, MD, FB2, FB3, ODT, ODS, ODP, PPTX, XLSX | Retained original bytes and constrained parsers; additional-format text is unverified and cannot become an exact quotation merely by importing it. PDF keeps separate page/OCR approval. See the per-format fidelity matrix. |
| Pending adapters and qualification | AZW, AZW3, CHM, DjVu, DOC, LIT, MOBI, PDB, PRC, PPT, RTF, TCR, WPD, XLS | Do not rename these to a supported suffix or silently invoke an unreviewed external converter. Raster/TIFF expansion remains separately tracked under V3-01. |

## Evidence and completion rule

Implementation references are `speech/`, `ui/speech_dialog.py`, `reader/`,
`studio/`, `conversion/`, `ui/command_registry.py` and their existing test suites.
The exact [Windows V3 run](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45923584569)
records real SAPI and Studio output. The exact
[keyboard/recovery run](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45923584608)
contains a successful keyboard job and a separate failed integrated-desktop job;
its overall failure is not a full-platform pass.

For each pending or partial row, extend the existing named module, record the
exact new source/engine identity, execute the real native workflow, retain
negative and recovery cases, and record manual NVDA/listening separately.
A complete checklist document closes the inventory task only. The open behaviors
above remain product requirements; they cannot be removed by relabelling the
candidate as a preview.

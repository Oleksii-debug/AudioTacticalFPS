# Familiar reading and synthesis workflows — finite acceptance inventory

Review date: 2026-11-27. Requirements: V3-03, V3-04, V3-05, V3-10 and V3-20.
This checklist fixes the requested workflow scope; changes to another product do
not silently create new Resonance requirements. The implementations are Resonance's
existing domain services and reviewed adapters. No Balabolka binary, proprietary
source, asset, configuration syntax or affiliation is included.

## How to read the status

**Integrated** means the named source path implements the bounded scenario.
**Limited** means only the stated subset is present.
**Pending** means requested code or integration is still needed.
Execution, acoustic qualification, native Windows/NVDA and distribution clearance
are independent. An Integrated row is not a PASS for any of those dimensions.

Source inputs for the current reconciliation are shared commit
`6a28a7ecdcfdf7dab2d5b948608c42803fe3e8a0`, reviewed selection
`042e44820bd3ec1ec0ab7c17236139ced58c9d25` and canonical
`4b6f8169986c4a58b43e4a23bc081fbefadbbdcb` (including the OCR inventory predecessor). Use the final PR143 head's installed
source hashes and reports to establish execution; results from these inputs
cannot be reassigned to the composed tree.

## Reading, editing and navigation

The public reference descriptions identify clipboard reading, native voices,
reading controls, substitutions, editor appearance, tray/global keys and
synchronized text. This table maps the requested scenarios to our own source,
rather than claiming compatibility with a proprietary implementation.
References: [official description](https://www.cross-plus-a.com/balabolka.htm),
[official voice information](https://www.cross-plus-a.com/voice.htm).

| ID | Requested scenario | Source status and implementation | Separate acceptance / remaining work |
| --- | --- | --- | --- |
| READ-01 | Select an actually installed local voice | Integrated: `speech/runtime.py`, `runtime_settings.py` and native reader controls | Exact installed voice/profile/engine identity; no invented voice-language pair |
| READ-02 | Rate, volume and pitch controls | Limited: capability-based SpeechSettings and provider mappings | Only controls declared by the selected engine are enabled; acoustic response still needs listening |
| READ-03 | Read all, selected text or from cursor | Integrated: `ui/speech_dialog.py`, `speech/live_adapter.py`, `reader/live_reading.py` | Exact source text and selection/cursor mapping, cancel and actual native playback |
| READ-04 | Pause, resume, stop and seek | Integrated through shared LiveReadingControls and native player | Actual device cleanup, resume position and Windows keyboard execution |
| READ-05 | Continue through source positions and save reading state | Integrated: Reading Desk, DocumentSequence and `reader/live_state.py` | No inferred printed page labels; restore exact source identity before resuming |
| READ-06 | Editable reading copy with original/diff and undo/redo | Integrated: `speech/reading.py`, reader dialog and draft store | Original retained bytes and quotation authority stay unchanged |
| READ-07 | Pronunciation substitutions and preview | Integrated: existing rule model and `ui/pronunciation_dialog.py` | Rule validation, source invariance, saved copy and native keyboard/lifetime cases |
| READ-08 | Third-party dictionary formats | Pending | Existing native rules do not claim BXD/REX/DIC file compatibility |
| READ-09 | Normalize reading text | Limited: existing reversible whitespace/reading edits | Not a universal language-specific cleanup or typography engine |
| READ-10 | Spell checking | Pending | Choose reviewed dictionary/runtime and language coverage before implementation |
| READ-11 | Clipboard reading | Limited: explicit paste into editable reading text | Dedicated clipboard capture/automatic monitoring is not implemented |
| READ-12 | Tray controls and system-wide hotkeys | Pending | Current configurable accelerators are application commands, not global hooks |
| READ-13 | Font, color and editor appearance customization | Pending beyond OS/native defaults | Must preserve accessibility and saved settings |
| READ-14 | SAPI5 | Integrated adapter and native acceptance path | Qualify actual installed token/architecture; a registered voice is not a cloning engine |
| READ-15 | SAPI4 and other legacy speech platforms | Pending verified compatibility | No claim that obsolete drivers work on Windows11 x64 |
| READ-16 | Explicit output-device selection | Limited | Recording selects a capture device; the current player does not expose the complete requested output-device workflow |
| READ-17 | Searchable commands and saved shortcuts | Integrated: existing command registry, keyboard settings and command palette | Saved explicit conflicts and stable action IDs remain authoritative; manual NVDA still required |

## Input formats

The reference application's format inventory is a comparison input, not proof
that Resonance supports those formats. Our authoritative current list is
`extract/document_formats.py`, used by the import pipeline and native picker.
References: [official format overview](https://www.cross-plus-a.com/balabolka.htm)
and [official text-extraction utility](https://www.cross-plus-a.com/btext.htm).

| ID | Formats | Resonance boundary | Required evidence |
| --- | --- | --- | --- |
| FORMAT-01 | TXT, PDF, DOCX, EPUB | Integrated bounded import and retained original bytes | Native/scanned PDF, exact TXT, ordered DOCX/EPUB logical text, malformed input and Unicode paths |
| FORMAT-02 | HTML, Markdown, EML | Integrated isolated text previews | Static text only; no scripts, remote resources or automatic attachment import |
| FORMAT-03 | FB2, FB3, ODT, ODP, ODS, PPTX, XLSX | Integrated isolated structured previews | Ordered supported text; no macro execution, formula calculation or invented page labels |
| FORMAT-04 | AZW, AZW3, CHM, DjVu, legacy DOC/PPT/XLS, LIT, MOBI, PDB, PRC, RTF, TCR, WPD | Pending native adapters | Current supported workflow is explicit conversion of a copy with a separately reviewed local tool; this does not establish source-format fidelity |
| FORMAT-05 | MHT, TXTZ, WRI and generic archive extraction | Pending | No blanket unknown-format/IFilter execution |
| FORMAT-06 | Password-protected/encrypted documents | Not supported by the current adapter | No DRM bypass; approved future decryption needs its own contract |
| FORMAT-07 | Very large originals and huge batches | Limited by current safety budgets | Current 64 MiB original bound and other declared limits require streaming/benchmark work before raising them |

## Synthesis, splitting and audio interchange

The official FAQ and console documentation describe chapter output, split
criteria, naming patterns, audio controls and subtitle sidecars. Only the
scenarios below are included in this fixed inventory.
References: [official FAQ](https://www.cross-plus-a.com/bfaq.htm),
[console documentation](https://www.cross-plus-a.com/bconsole.htm),
[text-extraction documentation](https://www.cross-plus-a.com/btext.htm).

| ID | Requested scenario | Source status and implementation | Separate acceptance / remaining work |
| --- | --- | --- | --- |
| AUDIO-01 | Save a reading as WAV | Integrated: canonical VoiceRegistry and create-only output guard | Actual selected voice PCM, cancellation, changed assets, exact frames and no overwrite |
| AUDIO-02 | Headless synthesis/production | Integrated: existing speech/Studio CLI and typed project services | CLI calls the same domain contracts as the desktop |
| AUDIO-03 | Multi-file batch with explicit choices | Limited: durable import and production jobs | Full per-item voice/preset/naming workflow is not established by the queue alone |
| AUDIO-04 | Chapter/scene splitting | Limited: typed Studio scenes and source positions | General split preview by arbitrary regex, marker, size/duration and bookmark criteria remains pending |
| AUDIO-05 | Naming templates | Limited: current validated output names | Arbitrary first-line/header/sequence pattern UI remains pending |
| AUDIO-06 | Multiple roles and per-line voice choice | Integrated typed narration/scene model | Capability-specific emotion/timbre controls are not inferred |
| AUDIO-07 | Pauses, gain, pan, stems and final mix | Integrated through current renderer and FFmpeg | Exact timeline/channel/ducking/level tests; audible quality assessed separately |
| AUDIO-08 | Chaptered M4B | Integrated selected `audio/audiobook.py` adapter and existing Studio cache | Actual ffprobe chapter/title/timeline, escaping, process-exit, resume and publication tests await exact-head execution; player interoperability remains separate |
| AUDIO-09 | SRT/VTT and measured text timing | Integrated at supported line/segment boundaries | Does not claim word-level speech alignment |
| AUDIO-10 | LRC | Integrated `script.lrc` from existing measured narration-line onsets and normal artifact receipts | Exact-head line/timing and old-complete-job preservation tests; no word timing or recorded-clip transcript is inferred |
| AUDIO-11 | Synchronized EPUB3 and DAISY2.02 | Integrated selected publisher/reader formats over current publication authority | EPUBCheck, pinned DTD checks, exact receipts and real reader interoperability are distinct gates |
| AUDIO-12 | Audio codecs and conversion | Integrated typed encodings: WAV, FLAC, MP3, Vorbis, Opus, AAC/M4A/M4B, AIFF and WMA | Actual codec availability, bit depth/quality controls and UI coverage are qualified individually; a generic M4B encoding is not chaptered-book acceptance |
| AUDIO-13 | Metadata, cover art and embedded synchronized tags | Limited to explicit fields in supported exporters | General cover/tag editor and MP3 embedded lyric parity remain pending |
| AUDIO-14 | Microphone capture, retakes and clip editing | Integrated explicit capture and schema2 clip workflow | Physical device, pause/resume capture, real-time monitoring and full edit/undo/loop workflow remain separately pending |
| AUDIO-15 | Repeatable workflow presets/macros | Limited: current typed plans/projects and command registry | Reusable named batch templates/macros need explicit UI and allowlisted service mapping |
| AUDIO-16 | Automatic online speech | Not enabled by the reader | Optional provider workflows require their separate explicit consent/profile; no scraped consumer endpoint |

## Evidence ownership and remaining decisions

The exact final head must run the source and installed reader, voice, clip,
recording, publication and command tests selected by the workflows. Native probes
exercise real wx events and owned-process completion. Synthetic PCM establishes
transport/control invariants only. Human NVDA, acoustic listening, private engine
and model/profile rights remain separate records.

This is the first finite checklist, not a declaration that every row is finished.
Pending requested code remains in scope. Unsupported inputs stay explicit rather
than receiving silent conversion, fallback voices or fabricated source authority.

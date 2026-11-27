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

The current source composition reconciles canonical
`e3cf74c243d4ef930b526b5648ca9003c0900057`, the retained PR143 feature
history and the independently reviewed reader, conversion and effects additions.
Use the final PR143 commit's installed source hashes and Actions reports to
establish execution; results from a component or predecessor do not qualify
a different composed tree.

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
| READ-03 | Read all, selected text, from cursor, or the current word, sentence or paragraph | Integrated: shared live reader and `reader/reading_units.py`; bounded unit controls use exact Unicode/CRLF source offsets and stop at the selected boundary | Deterministic punctuation/blank-line boundaries, not language-aware sentence analysis; actual native playback remains a separate gate |
| READ-04 | Pause, resume, stop and seek | Integrated through shared LiveReadingControls and native player | Actual device cleanup, resume position and Windows keyboard execution |
| READ-05 | Continue through source positions and save reading state | Integrated: Reading Desk, DocumentSequence and `reader/live_state.py` | No inferred printed page labels; restore exact source identity before resuming |
| READ-06 | Editable reading copy with original/diff and undo/redo | Integrated: `speech/reading.py`, reader dialog and draft store; literal or bounded isolated-regex Find/Replace with selection-bound preview and explicit apply | Original retained bytes and quotation authority stay unchanged; native line endings map back to the exact reading text before applying |
| READ-07 | Pronunciation substitutions and preview | Integrated: existing rule model, native dictionary import/export and `ui/pronunciation_dialog.py`; selected Studio spoken-line drafts save a new project revision with conflict-checked undo | Rule validation, source invariance, captured destination approval, committed-save warnings and native keyboard/lifetime cases; exported dictionaries use Resonance's own versioned schema |
| READ-08 | Third-party dictionary formats | Pending | Existing native rules do not claim BXD/REX/DIC file compatibility |
| READ-09 | Normalize reading text | Limited: existing reversible whitespace/reading edits | Not a universal language-specific cleanup or typography engine |
| READ-10 | Spell checking | Integrated bounded review of an explicitly selected local Hunspell pair with the reviewed code-only Spylls component; one suggestion is previewed and applied to the existing undoable reading copy | Dictionaries are supplied explicitly; no bundled language coverage, grammar claim or automatic download. Changed sources/dictionaries and cancelled or expired workers cannot apply a correction |
| READ-11 | Clipboard reading | Integrated explicit text snapshot opened as a separate reading copy | Snapshot is bounded, never monitors the clipboard, and checks native owner lifetime before reading or opening it; no automatic clipboard capture |
| READ-12 | Tray controls and system-wide hotkeys | Integrated explicit Windows session controls in `ui/reader_background.py` over the one existing live reader; show, pause/resume and stop | Off by default; owner-selected keys, actual OS collision checks and retained cleanup ownership. Native registration/message delivery, physical keys, audio listening and human NVDA are separate evidence dimensions. See [session controls](READ12_SESSION_CONTROLS.md) |
| READ-13 | Font, color and editor appearance customization | Integrated shared native appearance controls in Reading Desk and the editable reader: installed fonts, foreground/background, 50–400% zoom and system-default reset; existing state backup preserves preferences | Windows high contrast suspends explicit colors; text and selection stay unchanged. Native Windows and NVDA qualification remain separate |
| READ-14 | SAPI5 | Integrated adapter and native acceptance path | Qualify actual installed token/architecture; a registered voice is not a cloning engine |
| READ-15 | SAPI4 and other legacy speech platforms | Pending verified compatibility | No claim that obsolete drivers work on Windows11 x64 |
| READ-16 | Explicit output-device selection | Integrated for Windows: session-scoped reader and Studio controls over the existing MCI player, with exact endpoint/capability checks, explicit default and no automatic device substitution; see [output selection](OUTPUT_DEVICES.md) | Exact-head native keyboard, owner destruction and MCI checks; physical routing/listening remains a separate hardware gate |
| READ-17 | Searchable commands and saved shortcuts | Integrated: existing command registry, keyboard settings, command palette and reviewed keymap import/export | Saved explicit conflicts and stable action IDs remain authoritative; changed/imported mappings require review before apply; manual NVDA still required |

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
| FORMAT-04 | AZW, AZW3, CHM, DjVu, legacy DOC/PPT/XLS, LIT, MOBI, PDB, PRC, RTF, TCR, WPD | Limited: RTF uses explicitly installed Pandoc; the other listed native adapters remain pending | Explicit conversion of a copy with a separately reviewed local tool remains available; this does not establish original-format fidelity or permit DRM bypass |
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
| AUDIO-03 | Multi-file batch with explicit choices | Integrated retained document batches through `conversion/batches.py`, the native Convert menu and the `documents` CLI; prepare, review, approve, pause/cancel and resume | Exact retained inputs, completed-output revalidation and explicit destination/voice/format review. One selected voice per reviewed batch; per-item voice macros remain a separate requested extension |
| AUDIO-04 | Chapter/scene splitting | Integrated exact-text preview by whole text, paragraphs, blank lines, punctuation, characters, literal/isolated regex markers, supplied section/chapter/page/bookmark boundaries or explicitly estimated duration | Parts cover the exact stream without gaps; source positions must actually exist. Estimated duration is not measured audio or language-aware segmentation; Studio receives canonical scenes without interpreting imported prose as commands |
| AUDIO-05 | Naming templates | Integrated bounded title/source/heading/first-line/document/part/index/total fields with preview, portable collision checks and no-clobber publication | No arbitrary format expression or path interpolation. A changed source, review, destination or existing output needs a new explicit review |
| AUDIO-06 | Multiple roles and per-line voice choice | Integrated typed narration/scene model | Capability-specific emotion/timbre controls are not inferred |
| AUDIO-07 | Pauses, gain, pan, stems, effects and final mix | Integrated current renderer/FFmpeg plus a typed ordered offline effect-chain editor, bypass, undo/redo and named local presets | Actual installed filter capabilities, numeric and Nyquist bounds, combined tempo, exact publication and backup checks. Offline conversion does not establish real-time effect monitoring or audible quality. See [effect chains](ORDERED_AUDIO_EFFECTS.md) |
| AUDIO-08 | Chaptered M4B | Integrated selected `audio/audiobook.py` adapter and existing Studio cache | Actual ffprobe chapter/title/timeline, escaping, process-exit, resume and publication tests await exact-head execution; player interoperability remains separate |
| AUDIO-09 | SRT/VTT and measured text timing | Integrated at supported line/segment boundaries | Does not claim word-level speech alignment |
| AUDIO-10 | LRC | Integrated `script.lrc` from existing measured narration-line onsets and normal artifact receipts | Exact-head line/timing and old-complete-job preservation tests; no word timing or recorded-clip transcript is inferred |
| AUDIO-11 | Synchronized EPUB3 and DAISY2.02 | Integrated selected publisher/reader formats over current publication authority | EPUBCheck, pinned DTD checks, exact receipts and real reader interoperability are distinct gates |
| AUDIO-12 | Audio codecs and conversion | Integrated typed encodings: WAV, FLAC, MP3, Vorbis, Opus, AAC/M4A/M4B, AIFF and WMA; numeric native rate/channel/PCM-depth/bitrate/range/gain/tempo/loudness controls reuse current DSP and shared admission | Actual codec availability and format-specific constraints are checked; optional post-save measurement failure reports a saved output with a warning; a generic M4B encoding is not chaptered-book acceptance |
| AUDIO-13 | Metadata, cover art and embedded synchronized tags | Integrated bounded native metadata/cover review through `audio/metadata.py` and `ui/audio_metadata_dialog.py`; only declared format-specific fields | Actual encoded-file and cover validation remain required. This does not claim general MP3 embedded synchronized-lyric parity |
| AUDIO-14 | Microphone capture, retakes and clip editing | Integrated explicit capture and schema2 clip workflow | Physical device, pause/resume capture, real-time monitoring and full edit/undo/loop workflow remain separately pending |
| AUDIO-15 | Repeatable workflow presets/macros | Limited: typed projects/plans, retained batch reviews and named local DSP presets are integrated | General multi-operation macros and arbitrary per-item voice/preset templates still need an explicit allowlisted contract; stored settings never execute on restore |
| AUDIO-16 | Automatic online speech | Not enabled by the reader | Optional provider workflows require their separate explicit consent/profile; no scraped consumer endpoint |

The rendered-master preview in Audio Studio additionally supports pause/resume,
bounded skips, percentage seek, A–B repeat and a sleep timer over the existing
player. Its timer and queued actions are bound to one playback generation and
the actual source-file identity. These controls do not establish cross-chunk
A–B repeat in the live text reader. Windows transport, physical output devices
and audible precision require their respective native and listening evidence.

## Evidence ownership and remaining decisions

The exact final head must run the source and installed reader, voice, clip,
recording, publication and command tests selected by the workflows. Native probes
exercise real wx events and owned-process completion. Synthetic PCM establishes
transport/control invariants only. Human NVDA, acoustic listening, private engine
and model/profile rights remain separate records.

This is the first finite checklist, not a declaration that every row is finished.
Pending requested code remains in scope. Unsupported inputs stay explicit rather
than receiving silent conversion, fallback voices or fabricated source authority.

# Familiar reading and synthesis workflows — finite acceptance inventory

Review date: 2026-11-27. Requirements: V3-03, V3-04, V3-05, V3-10 and V3-20.
This checklist fixes the requested workflow scope; changes to another product do
not silently create new Resonance requirements. The implementations are Resonance's
existing domain services and reviewed adapters. No Balabolka binary, proprietary
source, asset or affiliation is included. Documented interchange grammars are
handled as explicitly imported data; they do not execute the upstream program.

## How to read the status

**Integrated** means the named source path implements the bounded scenario.
**Limited** means only the stated subset is present.
**Pending** means requested code or integration is still needed.
Execution, acoustic qualification, native Windows/NVDA and distribution clearance
are independent. An Integrated row is not a PASS for any of those dimensions.

The current source composition reconciles canonical
`e5c64a5245583f8f8b404a0f042f6ca438c24d7f`, the retained PR143 feature
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
| READ-02 | Synthesis rate/prosody and independent listening controls | Integrated: existing capability-based SpeechSettings plus `ui/playback_settings.py`, `speech/listening.py` and the existing reader/Studio worker | Playback tempo and audio pitch are independent of synthesis. Volume/mute/balance transform only the selected output's bounded PCM buffers; source audio and other players remain unchanged. Unsupported voice-native controls stay disabled. See [independent playback](INDEPENDENT_PLAYBACK.md) |
| READ-03 | Read all, selected text, from cursor, or the current word, sentence or paragraph | Integrated: shared live reader and `reader/reading_units.py`; bounded unit controls use exact Unicode/CRLF source offsets and stop at the selected boundary | Deterministic punctuation/blank-line boundaries, not language-aware sentence analysis; actual native playback remains a separate gate |
| READ-04 | Pause, resume, stop and seek | Integrated through shared LiveReadingControls and native player | Actual device cleanup, resume position and Windows keyboard execution |
| READ-05 | Continue through source positions and save reading state | Integrated: Reading Desk, DocumentSequence and `reader/live_state.py` | No inferred printed page labels; restore exact source identity before resuming |
| READ-06 | Editable reading copy with original/diff and undo/redo | Integrated: `speech/reading.py`, reader dialog and draft store; literal or bounded isolated-regex Find/Replace with selection-bound preview and explicit apply | Original retained bytes and quotation authority stay unchanged; native line endings map back to the exact reading text before applying |
| READ-07 | Pronunciation substitutions and preview | Integrated: existing rule model, native dictionary import/export and `ui/pronunciation_dialog.py`; selected Studio spoken-line drafts save a new project revision with conflict-checked undo | Rule validation, source invariance, captured destination approval, committed-save warnings and native keyboard/lifetime cases; exported dictionaries use Resonance's own versioned schema |
| READ-08 | Third-party dictionary formats | Integrated bounded import of text-only BXD, DIC and REX into the existing reviewed pronunciation rules, with author-tool observations for the accepted grammar; canonical JSON import/export remains available | Unsupported phoneme output modes and unmapped expression/replacement operators are refused explicitly. Every import previews the converted rules before saving; no second rule engine or dictionary store. See [dictionary formats](PRONUNCIATION_DICTIONARIES.md) |
| READ-09 | Normalize reading text | Limited: existing reversible whitespace/reading edits | Not a universal language-specific cleanup or typography engine |
| READ-10 | Spell checking | Integrated bounded review of an explicitly selected local Hunspell pair with the reviewed code-only Spylls component; one suggestion is previewed and applied to the existing undoable reading copy | Dictionaries are supplied explicitly; no bundled language coverage, grammar claim or automatic download. Changed sources/dictionaries and cancelled or expired workers cannot apply a correction |
| READ-11 | Clipboard reading | Integrated explicit text snapshot opened as a separate reading copy | Snapshot is bounded, never monitors the clipboard, and checks native owner lifetime before reading or opening it; no automatic clipboard capture |
| READ-12 | Tray controls and system-wide hotkeys | Integrated explicit Windows session controls in `ui/reader_background.py` over the one existing live reader; show, pause/resume and stop | Off by default; owner-selected keys, actual OS collision checks and retained cleanup ownership. Native registration/message delivery, physical keys, audio listening and human NVDA are separate evidence dimensions. See [session controls](READ12_SESSION_CONTROLS.md) |
| READ-13 | Font, color and editor appearance customization | Integrated shared native appearance controls in Reading Desk and the editable reader: installed fonts, foreground/background, 50–400% zoom and system-default reset; existing state backup preserves preferences | Windows high contrast suspends explicit colors; text and selection stay unchanged. Native Windows and NVDA qualification remain separate |
| READ-14 | SAPI5 | Integrated adapter and native acceptance path | Qualify actual installed token/architecture; a registered voice is not a cloning engine |
| READ-15 | SAPI4 and other legacy speech platforms | Pending verified compatibility | No claim that obsolete drivers work on Windows11 x64 |
| READ-16 | Explicit output-device selection | Integrated session-scoped reader and Studio controls; the default path keeps existing MCI playback, while explicit listening levels use the selected owned WinMM output with bounded PCM buffers | No implicit device substitution. Actual device inventory, native workflow controls, physical routing/listening and human NVDA remain separate evidence. See [output selection](OUTPUT_DEVICES.md) and [listening controls](INDEPENDENT_PLAYBACK.md) |
| READ-17 | Searchable commands and saved shortcuts | Integrated: existing command registry, keyboard settings, command palette and reviewed keymap import/export | Saved explicit conflicts and stable action IDs remain authoritative; changed/imported mappings require review before apply; manual NVDA still required |

## Input formats

The reference application's format inventory is a comparison input, not proof
that Resonance supports those formats. Direct catalog imports are defined by
`extract/document_formats.py`, used by the import pipeline and native picker.
Additional reading conversions are explicit choices in `conversion/formats.py`
and **Convert → Convert documents for reading**. They preserve the original and
publish a separate UTF-8 derivative with a source/text-hash receipt; they do not
add every legacy suffix as an unqualified raw catalog import.
References: [official format overview](https://www.cross-plus-a.com/balabolka.htm)
and [official text-extraction utility](https://www.cross-plus-a.com/btext.htm).

| ID | Formats | Resonance boundary | Required evidence |
| --- | --- | --- | --- |
| FORMAT-01 | TXT, PDF, DOCX, EPUB | Integrated bounded import and retained original bytes | Native/scanned PDF, exact TXT, ordered DOCX/EPUB logical text, malformed input and Unicode paths |
| FORMAT-02 | HTML, Markdown, EML | Integrated isolated text previews | Static text only; no scripts, remote resources or automatic attachment import |
| FORMAT-03 | FB2, FB3, ODT, ODP, ODS, PPTX, XLSX | Integrated isolated structured previews | Ordered supported text; no macro execution, formula calculation or invented page labels |
| FORMAT-04 | AZW, AZW3, CHM, DjVu, legacy DOC/PPT/XLS, LIT, MOBI, PDB, PRC, RTF, TCR, WPD | Integrated selected-tool routes: explicit Calibre for the eight book families; explicit LibreOffice for binary DOC/PPT/XLS, WordPerfect and PalmDoc PDB; existing explicit Pandoc for RTF | Actual finite native format gates remain independent of source integration. Unprotected supported containers only; DjVu needs existing embedded text, PDB/PRC require supported headers, and no route grants visual fidelity or quotation authority. See [legacy documents](LEGACY_DOCUMENT_READING.md) and [Calibre reading](CALIBRE_READING.md) |
| FORMAT-05 | MHT, TXTZ, WRI and generic archive extraction | Integrated: isolated MHT/MHTML root-text reading, guarded single-TXT TXTZ, bounded readable ZIP members, and explicit LibreOffice MS Write conversion | MIME roots must be unambiguous; no external resource retrieval. ZIP members are read without filesystem extraction; no nested/encrypted archives or arbitrary decoder. WRI requires the supported BE31 text-only header and actual selected-filter acceptance |
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
| AUDIO-03 | Multi-file batch with explicit choices | Integrated serial typed audio conversion through the reviewed Assistant and retained document batches through `audio/conversion_batch.py`, `conversion/batches.py`, the native Convert menu and the `documents` CLI; prepare, review, approve, pause/cancel and resume | Exact retained inputs, completed-output revalidation and explicit destination/voice/format review. One selected voice per reviewed batch; reusable workflow/project templates can explicitly parameterize per-item voice settings for a fresh review, without silently enqueueing batches |
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
| AUDIO-14 | Microphone capture, retakes and clip editing | Integrated Start/Pause/Resume/Stop/Cancel capture, exact PCM-segment receipts, explicit new retakes linked to retained prior bytes, and schema3 clip timelines: timecode ranges, copy/cut/paste/trim/split/join, silence, fades/crossfades, markers, history, loop and dry/wet preview | Original recordings and source files remain unchanged. Retakes never automatically replace a Studio asset. Existing schema1/2 fingerprints are preserved; schema3 effects/markers use the current renderer and passive backup. Optional dry monitoring stays off by default; actual device quality and latency need physical qualification. See [recording](MICROPHONE_RECORDING.md) and [clip editing](STUDIO_CLIP_EDITING.md) |
| AUDIO-15 | Repeatable workflow presets/macros | Integrated typed assistant, Studio-project and document-batch templates with explicit parameters, per-item overrides, import/export and conflict-checked revisions; existing DSP presets remain separate | Expansion uses the current allowlisted operations, budgets and execution host. Every new use requires current scope/profile/input/output review; saved templates and restored shortcuts carry no execution or network approval. See [workflow templates](WORKFLOW_TEMPLATES.md) |
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

This finite checklist records the current implemented routes and their explicit limits.
Its runtime and human qualification columns remain independent of source completion.
Pending requested code remains in scope. Unsupported inputs stay explicit rather
than receiving silent conversion, fallback voices or fabricated source authority.

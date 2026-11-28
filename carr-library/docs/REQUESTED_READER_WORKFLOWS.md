# Requested reading and synthesis workflows: finite acceptance checklist

This is the finite workflow inventory required by V3-01 through V3-04 and the
[current technical specification](UNIVERSAL_SCOPE_V3_FINALIZATION.md). It maps
the requested familiar reading/synthesis functions to Resonance's existing
implementation. It is not a promise to reproduce every present or future
function of another application.

The implementation snapshot for this inventory is
[`6042d1eb52ea32ded3769f9cb67459b119a4016c`](https://github.com/Oleksii-debug/AudioTacticalFPS/commit/6042d1eb52ea32ded3769f9cb67459b119a4016c),
tree `9d88b8a0475f88fc75cf20ffffe6c7b37bce2e57`. Source presence, actual execution,
engine/device quality and distribution approval remain separate. A later
candidate must supply its own exact-source acceptance.

## Behavioral references and scope

The official [product description](https://www.cross-plus-a.com/balabolka.htm)
documents local voices, text extraction, clipboard, display customization,
tray/global commands, pronunciation rules, synchronized text and spelling.
The official [FAQ](https://www.cross-plus-a.com/bfaq.htm) supplies the requested
split/naming/chapter and output-selection examples. The official
[console documentation](https://www.cross-plus-a.com/bconsole.htm) describes
voice/device enumeration, selected input, speech parameters and file output.
These public descriptions inform the finite rows below; implementation uses
Resonance's own services and reviewed dependencies. No third-party application
binary, source, assets or command syntax is adopted by this checklist.

The requested input suffix inventory remains in the existing
[document-format matrix](DOCUMENT_FORMATS_V3.md) and
[selected-converter matrix](DOCUMENT_CONVERSION.md). They distinguish direct
retained-source import from limited, explicitly selected local reading-copy
conversion. Aliases, unsupported structures, scanned content and native-source
quotation authority must keep those existing distinctions.

## Finite workflow map

**Present** means the source implements the stated bounded workflow.
**Limited** identifies an implemented subset with an explicit boundary.
Neither label means that all engines, codecs, languages, native devices or
the final integration have passed. The test references are required obligations,
not substituted execution receipts.

| ID | Requested workflow | Implementation and required check | Source status and precise boundary |
| --- | --- | --- | --- |
| R01 | Open documents for reading | [Formats](DOCUMENT_FORMATS_V3.md), [conversion](DOCUMENT_CONVERSION.md); [converter tests](../tests/conversion/) | Limited. Direct formats retain original bytes; selected legacy conversion retains its original, text and fidelity report externally. No DRM bypass or invented printed-page authority. |
| R02 | Enumerate and select local voices | [Voice runtime](speech_runtime.md), [preferences](VOICE_PREFERENCES.md), [Dordiai contract](DORDIAI_VOICES_CONTRACT.md); [speech tests](../tests/speech/) | Present through existing adapters. Actual SAPI5, SAPI4 compatibility, supplied private engines and each selected model/voice require their own execution. A voice name never establishes a language match. |
| R03 | Read a copy, selection, current text unit or clipboard snapshot | [Reader commands](READING_DESK.md), [clipboard](CLIPBOARD_READING.md); [navigation](../tests/speech/test_navigation.py), [native reader actions](../tests/ui/test_reader_navigation_ui.py) | Present. Unicode ranges and logical boundaries remain source-aware; deterministic text units are not linguistic or phoneme timing. Clipboard input is an explicit independent copy. |
| R04 | Play, pause, resume, stop, seek, repeat and sleep | [Reader controls](READING_DESK.md), [output](READER_PLAYBACK_OUTPUT.md); [player controls](../tests/speech/test_player_controls.py), [reader playback](../tests/ui/test_reader_playback.py) | Present. A–B repeat, skips and sleep retain existing lifetime/cancellation rules. Native playback qualification still requires actual output advancement. |
| R05 | Select synthesis rate, pitch, volume and available voice controls | [Audio engine](audio_engine.md), [voice runtime](speech_runtime.md); [speech tests](../tests/speech/) | Limited to declared controls of the selected adapter. Unsupported engine controls must remain unavailable or explicitly refused. Listening speed is a separate R06 operation. |
| R06 | Change listening speed and pitch independently | [Listening preparation](LISTENING_SPEED.md); [actual converter tests](../tests/speech/test_listening.py), [reader hooks](../tests/ui/test_reader_listening_hooks.py) | Present: 0.25–4 times speed and −12 to +12 semitones through a private verified derivative. Apply is silent; original frames remain the public timeline. Intermediate alignment is an estimate. |
| R07 | Pronunciation substitutions and reusable dictionaries | [Pronunciation workflow](READING_DESK.md), [native dictionary](../src/carr_library/ui/pronunciation_dialog.py); [speech tests](../tests/speech/) | Present for bounded literal/regex rules with explicit language/voice scope and import/export. Rules make one reversible reading edit and do not independently establish stress or accent quality. |
| R08 | Spelling suggestions and explicit corrections | [Hunspell review](HUNSPELL_REVIEW.md); [spelling](../tests/speech/test_spelling.py), [handlers](../tests/speech/test_spelling_handlers.py) | Limited to the explicitly selected reviewed runtime and dictionary pair. A suggestion is a proposal; no automatic dictionary download or original-source rewrite. |
| R09 | Separate tabs, saved copies, undo/redo, find and replace | [Tabs](READING_TABS.md), [reader editing](READING_DESK.md); [tabs](../tests/speech/test_reading_tabs.py), [find/replace](../tests/ui/test_find_replace.py) | Present. Editing and recovery belong to the derived reading copy. Reviewed replacement becomes invalid after text, selection, scope or lifetime changes. |
| R10 | Bookmarks, paragraph split/join and remembered positions | [Reading tools](READING_TOOLS.md), [source reading](READING_DESK.md); [reader tests](../tests/reader/), [speech tests](../tests/speech/) | Present. Source-page resume and reading-copy Unicode positions remain distinct; changed text cannot silently relocate a saved bookmark. |
| R11 | Font, colors and zoom | [Reading appearance](READING_PRESENTATION.md); [presentation](../tests/speech/test_reading_presentation.py), [handlers](../tests/speech/test_reading_presentation_handlers.py) | Present with native preview/apply/reset and guarded saved settings. Automated fixture selection does not qualify OS picker interaction, legibility or human NVDA use. |
| R12 | Split text into reviewed audio parts and choose names | [Book plans](BOOK_AUDIO.md); [book audio](../tests/studio/test_book_audio.py), [CLI](../tests/studio/test_book_cli.py) | Present for documented source/chapter/page/line/sentence/character/blank-line/regex splits and bounded index/book/part names. Measured-duration splitting is a separate completed-PCM operation. |
| R13 | Process multiple books with review, pause/cancel and resume | [Book batches](BOOK_BATCHES.md); [batch contracts](../tests/studio/test_book_batch.py) | Present, execution unresolved. The A3 30-case whole module timed out at its unchanged 600-second bound; no whole PASS or stuck-case cause is inferred. Every original ownership/recovery case remains required. |
| R14 | Export audio, chapters and synchronized text | [Studio and books](BOOK_AUDIO.md), [recorded EPUB](EPUB_RECORDED_AUDIO.md); [Studio tests](../tests/studio/) | Limited to the selected runtime codecs and documented measured timelines. WAV/FLAC/MP3/OGG/Opus/M4A/M4B/AIFF/WMA availability is verified for the actual runtime. Subtitle/overlay timing is not automatically word or viseme timing. |
| R15 | Convert audio in batches and preserve completed outputs | [Audio conversion](AUDIO_CONVERSION_BATCH.md); [conversion UI](../tests/ui/test_audio_conversion_batch.py), [CLI](../tests/studio/test_conversion_batch_cli.py) | Present through the same typed converter and durable job boundaries. Existing output review, cancellation and uncertain-publication recovery are retained. |
| R16 | Headless controlled book and conversion work | [Book CLI](BOOK_BATCHES.md), [conversion CLI](AUDIO_CONVERSION_BATCH.md); [book CLI](../src/carr_library/studio/book_cli.py), [conversion CLI](../src/carr_library/audio/conversion_batch_cli.py) | Present with Resonance's documented arguments, exact review tokens and existing services. This does not claim compatibility with another program's command-line flags. |
| R17 | Optional clipboard watching | [Desktop integration](DESKTOP_INTEGRATION.md); [integration tests](../tests/ui/test_desktop_integration.py) | Present, off by default. Observation retains only the latest bounded text in memory; it never starts speech or changes an opened source. Opening the snapshot is a separate explicit action. |
| R18 | Optional tray and global shortcuts | [Desktop integration](DESKTOP_INTEGRATION.md); [settings](../tests/ui/test_desktop_settings.py), [integration](../tests/ui/test_desktop_integration.py) | Present on supported Windows desktop sessions. Saving keys does not register them; collisions, partial acquisition, scope changes and unconfirmed native cleanup remain explicit. |
| R19 | Discover commands and use native keyboard actions | [Keyboard profiles](KEYBOARD_SHORTCUTS.md); [command registry](../tests/ui/test_command_registry.py), [dispatch](../tests/ui/test_reader_command_dispatch.py) | Present. Exact B keyboard/recovery execution passed its own scope; that result does not qualify tray, physical audio or manual screen-reader behavior. |
| R20 | Save, recover and move the reader's state | [Primary backup](PRIMARY_STATE_BACKUP.md), [tabs](READING_TABS.md), [voice preferences](VOICE_PREFERENCES.md); [typed storage tests](../tests/storage/) | Present with explicit typed-state and source validation. Restored preferences are passive; credentials, external engines and live native registrations do not become execution authority. |

## Evidence and remaining acceptance

The exact B keyboard/recovery receipt is retained in
[`actual_b_keyboard_recovery.json`](../../resonance-status/next67/actual_b_keyboard_recovery.json):
238 source cases and separate native report groups of 34, 23 and 14. It binds
B's source/tree and original artifact digest. No sum across those native groups
is claimed as a unique feature count.

The [coherent A3 source receipt](../../resonance-status/next68/actual_a3_source_138.json)
retains 137 passing whole modules and an explicit BookBatch timeout. Its selected source results include the actual
Windows SAPI production fixtures, Studio editing/markers/repeat and DAISY.
These are earlier-source, bounded software results. Full current source,
installed-wheel and native acceptance remains required by
[acceptance.md](acceptance.md).

On the [retained older Windows run](../../resonance-status/next68/actual_5b2_native_outcomes.json)
the audio services were running but the native inventory contained zero output devices. Reader playback and recording were
not qualified, and saved-audio advancement failed. A passive dialog or successful
cleanup cannot replace an actual output/device result. Exact original reports
remain separate from the later recorded-EPUB error-reporting correction.

Actual SAPI4/owner-engine combinations, supported device/driver profiles,
native-language quality, appropriate human NVDA/listening review and the complete
135-entry capability qualification remain open until their own evidence exists.
These inputs are not replaced by the checklist or by a passing source test.

When recording a completed row, retain the requirement ID, exact app source/tree,
installed artifact, selected runtime/model/pack, scenario and input/result
identities, actual outcome, independent review and applicable profile. Preserve
failure, partial and not-run states; do not silently delete a row to close the
inventory.

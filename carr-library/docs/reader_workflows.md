# Recorded EPUB audio, pronunciation review and command search

These views extend the existing source reader, editable reading copy, native
menu registry and local WavePlayer. They do not create another library store,
another speech engine, or quotation authority.

## Entry points

| Action | Existing entry point | User-controlled result |
| --- | --- | --- |
| Recorded EPUB audio | Reading Desk, **EPUB synchronized recorded audio…** | Read the current retained source section while its authored local recording plays. Opening the view does not start playback. |
| Pronunciation review | Voice reader, **Pronunciation rules** | Preview rules against the whole reading copy or its selected text, then explicitly apply one undoable edit. |
| Command search | Help, **Find a command…**, default Ctrl+Shift+P | Find an existing main-window menu action by name, path or its current shortcut. |
| Language evidence | Voice, **Languages and capability evidence…** | Inspect the unchanged canonical catalog and explicitly enumerate configured local voices separately. Enumeration is not acoustic qualification. |

## Recorded EPUB audio

The source is the library's retained EPUB and the exact current source reading
position. EPUB 3 local recorded-audio SMIL `seq`/`par` overlays are supported.
The existing EPUB parser supplies both text normalization and archive/XML
bounds. Exact start and end offsets are re-extracted for each authored fragment;
repeated words are never located by substring guesses.

Unsupported or ambiguous structures fail visibly. This includes remote
resources, encryption/obfuscation, missing or repeated fragment IDs, additional
SMIL timing/conditions, unsupported audio formats and references to another
source section. A section is bounded to 2,048 cues, and an individual prepared
clip to one hour. No text-to-speech fallback or word-level timing is inferred.

An isolated, bounded child reads the private source snapshot. Actual local
FFmpeg/ffprobe inspect and prepare the selected authored interval as PCM;
measured frame count must equal the declared interval. The existing WavePlayer
handles Play, Pause, Resume and Stop. Previous/next segment selection does not
automatically play. Normal completion advances within the current source
section. A stop from another owned audio operation invalidates pending playback
and preserves explicit replay of the selected segment.

Source identity, visibility, metadata and retained bytes are checked on the
worker. Immediately before native start/resume, the scope and observed file
identities are checked again without hashing large files on the GUI thread.
Changed or archived source scope stops/rejects playback. This boundary is not
an OS sandbox against another process that can forge metadata or race the
final native file open.

The isolated parser and audio conversion reserve the existing shared
`ResourceBroker.for_app` policy. The parser declares one CPU slot; the default
converter selects at most two configured slots. Admission includes bounded
memory/disk estimates and cancellation while waiting. Reservations do not
measure or enforce whole-system RAM, VRAM or arbitrary external processes.
The policy remains at `state/operations/resources.json`; live leases remain
under `runtime/resources`, outside restored persistent state.

## Pronunciation review

Rules use the existing `carr.pronunciations.v1` format and bounded substitution
engine. Literal or regular-expression rules may be scoped by language and
exact voice ID. General rules run before specific scopes, retaining list order
within each scope.

Import, preview, dictionary save/export and reading-copy apply are distinct
actions. Import never applies text changes. Apply verifies the captured
document identity/signature, exact text, selection and selected voice again;
a changed scope requires a fresh preview. One apply creates one undoable
reading edit. Original source text, source anchors, their history and quotation
authority remain unchanged.

The dictionary is stored at `state/speech/pronunciations.json`. Its existing
content digest is checked under the ordinary state lock before replacement,
so an independent edit requires reload. Export creates a new file and does not
overwrite an existing target. Existing full library backup includes the exact
dictionary bytes; the regression restores and reopens them after deleting the
original library. No separate backup format is introduced.

Reader integration uses the current shared `LiveReadingControls` and `DocumentSequence`
for reading-copy playback. Opening recorded EPUB audio explicitly stops live voice
reading; no second speech player or registry is installed by this component.
Native text ranges use the shared Unicode and line-ending mapping.

Reader integration retains all three accepted historical source formats,
source-family recovery/discovery, explicit historical detachment, authorization
before and after save choosers, and truthful post-save recovery warnings.
Pronunciation does not rewrite those formats or infer authority from equal text.
A recovery-list warning remains visible after a successfully applied/undone
reading edit.

## Command palette and shortcuts

The palette enumerates the existing keyboard/menu registry and dispatches the
original native menu event only after its modal window closes. The original
handler keeps its own scope checks. Current menu path, original item and ID,
ancestor/top-level enablement, library/root and registry identity are checked
again at dispatch. A moved, replaced, disabled or stale command cannot be
retargeted by a reused visible label.

The palette is installed before KeyboardSettings captures the final menu
inventory. A prior explicit saved shortcut keeps ownership when a newly added
default conflicts; the new default becomes unassigned and the native settings
view explains the conflict. Duplicate explicit assignments and unknown saved
actions still fail. Existing saved bytes change only on an explicit Apply.
User-defined shortcuts, editable-control keys and screen-reader keys retain
their existing policy.

## Native lifetime and evidence

Reused views cache their native owner ID while live. Child destruction does
not detach the owner. Direct Destroy requests detach callbacks immediately;
native parent deletion uses the filtered destruction event as a fallback.
Closed views do not access controls from queued completion, filter or refresh
callbacks. Owned work is cancelled and temporary files are released when their
worker/native device releases them. An unconfirmed playback close retains its
inputs and does not report confirmed silence.

The source/test composition requires fresh qualification. No donor run count
or prior installed-wheel result qualifies newly composed files.

Required Python suites include:

- `tests/reader/test_ebook_audio.py` (actual FFmpeg/ffprobe required)
- `tests/speech/test_pronunciation_review.py`
- `tests/speech/test_pronunciation_handlers.py`
- `tests/speech/test_pronunciation_reader_hook.py`
- `tests/speech/test_reader_native_lifetime.py`
- `tests/ui/test_command_palette.py`
- `tests/ui/test_keyboard_settings.py`
- `tests/ui/test_reader_dialog_lifetime.py`
- `tests/ui/test_progress_voice_actions.py`
- `tests/ui/test_reading_copy_save_guard.py`
- `tests/language_qualification/test_ui.py`

The existing source/reading/session, reading-desk and backup regressions remain
required. Installed qualification resolves the installed package under the
normal interpreter; it must not substitute checkout source files.

Actual Windows acceptance uses `python -I -B -X utf8` with:

- `scripts/windows_epub_reading_smoke.py --audio-evidence <external-test-audio-binding.json>`
- `scripts/windows_pronunciation_smoke.py`
- `scripts/windows_command_palette_smoke.py`
- `scripts/windows_reader_lifetime_smoke.py`

The probes activate the packaged native runtime before native imports and
verify exact current installed source. EPUB audio uses the same explicit
external executable path/hash evidence as publication acceptance. Its real
audio is a synthetic tone fixture, not a qualified voice. Pronunciation uses a
synthetic enumeration provider and makes no synthesis request. Automated native
keyboard and lifetime results do not establish physical keyboard, NVDA,
pronunciation quality or third-party reader interoperability.

## Reuse

This component reuses the reader, pronunciation and palette implementations and
their authored regressions from PR112 head
`6da794a2ef83c77d160e7f89a1b457a5f375cf6a`, composed on canonical PR143
`e07545a99db45bf46c7d985d33bd98e284d25833` and reconciled with its live-reader
successor `6a28a7ecdcfdf7dab2d5b948608c42803fe3e8a0`. Current source authority,
export receipts, shared-resource admission and native lifecycle corrections are
preserved. Unrelated reader formats, alternate transcription packages, active
document handlers and Studio schema changes are outside this component.

# Read source text aloud

The source reader can speak the current page or text segment, start at its
cursor, or speak an explicitly selected range. It uses the same installed local
voice discovery, speech renderer and PCM player as Audio Studio. Opening a book
or restoring a backup does not start speech.

Open the source reader, wait for local voice discovery, and choose a voice in
**Local reading voice**. The first voice is not selected automatically. Choose
the rate and volume for the next reading operation, then use one of the three
reading buttons. A missing voice or output device is reported; another voice or
engine is not substituted silently. Engine-specific limits remain enforced by
the shared renderer.

Discovery and synthesis run outside the native UI thread. **Stop voice reading**
also cancels an in-progress synthesis operation. A newly opened source page can
finish loading while discovery continues. Navigating to a different source
location stops active speech. Background status updates and an enabled automatic
next page do not move keyboard focus.

## Controls and keyboard access

The source reader uses the existing configurable command registry. Its reader
command palette and shortcut settings include these commands:

| Action | Default shortcut |
| --- | --- |
| Read current source text aloud | F6 |
| Read aloud from the cursor | F9 |
| Read selected text aloud | Ctrl+Shift+F6 |
| Pause or resume the active voice operation | F7 |
| Stop voice reading | F8 |
| Resume the saved voice position | Ctrl+Shift+F8 |
| Focus voice reading status | Ctrl+Alt+F8 |
| Discover local voices again | Unassigned; available as a button and command |

Tab and Shift+Tab move through the native controls. Voice, rate and volume are
named native choices. Escape closes the reader and cancels its worker. Existing
source navigation and the shared **Ctrl+Shift+Q** progress query remain available.
The actual installed bindings appear in the reader keyboard guide; customized
bindings take precedence over this default table.

**Continue automatically to the next source location** is off initially. Enable
it deliberately to continue through the same captured source edition. Reading a
selection always ends at that selection. Automatic continuation does not gather
the entire book into one synthesis request.

## Exact source text and saved positions

Cursor and selection ranges are mapped from native control prefixes to the
original Unicode source. Windows UTF-16 positions and native CRLF display rules
are not treated as Python text indexes. Repeated words, Ukrainian characters and
emoji therefore retain their original source positions. The selected text is
split into exact bounded chunks using the existing Studio chunker; the live
reader does not rewrite spacing or apply a pronunciation dictionary silently.

The status reports the current chunk's exact source character offsets and the
player's measured audio seconds separately. **These are not word timestamps.**
The application does not infer which individual word is audible from elapsed
time. Pause uses the player's measured position; it does not advance a guessed
source cursor while the audio is paused.

Before playback, the source's library, edition, active revision, retained
original and text hash are checked. Authorization is checked again during
synthesis and playback. Archiving the library, changing the active source
revision or losing the captured reader scope ends the operation. Source reading
does not approve extracted text as quotation evidence or establish printed page
numbers.

The last live position is saved separately from the existing page bookmark and
ordinary page-level resume. It records the exact source location, text hash,
text length, current chunk start and requested range end. **Resume saved voice
position** is explicit and requires an explicitly selected voice. It restarts
the current verified chunk, so the beginning of that chunk can be repeated after
Stop or an interruption. A changed source is rejected rather than mapped onto
different text. A failed state save reports a warning and preserves the earlier
state file.

Full library backup includes this state. Scoped backup includes only positions
for the selected libraries. Restore authenticates source bindings and offsets;
restoring state never opens a player automatically.

## Read an edited copy

The reading-copy editor has **Read aloud**, also available through
**Ctrl+Alt+V**. It captures a frozen candidate of the current editor text and
opens the same live controls. Unsaved edits can be heard without changing the
retained original or saving an Audio Studio project.

A source-backed reading copy retains its existing source authorization and
provenance checks. Independently authored copies remain explicitly authored
text. Edited text is not quotation evidence. Live positions for edited copies
are session-only; they are not written into the original source's resume file.
Saved reading copies and the existing **Save and open Audio Studio** handoff are
unchanged.

## Resource and lifecycle bounds

The defaults are 600 characters per synthesis chunk, 60 seconds to synthesize
one chunk, at most 180 seconds of audio and 32 MiB per chunk. One session is
bounded to 500,000 source characters, four hours of synthesized audio and six
hours of wall time. At least 64 MiB must remain free beyond the next chunk's
output allowance. These limits stop the operation with a readable message;
continuing requires another explicit reading action.

Generated WAVs stay in a private temporary directory under the library root.
Their digest and measured duration are checked before playback. The single
worker closes the player before removing each file, including during Stop,
window close and device failure. Cleanup failures are reported while the reader
remains open. Temporary speech is not saved as an Audio Studio production.

The shared progress sound preference is respected. When **Quiet during media**
is enabled, an existing progress cue is suspended and playback waits for it to
finish. This wait yields to the native event loop and is bounded. Closing the
window keeps media suppression active until the worker has closed its player;
its completion callback never touches a destroyed native control.

## Acceptance evidence and its limits

The source tests use actual retained documents, SQLite state and generated PCM
files with an explicitly synthetic controllable device. They cover exact
selection and chunk concatenation, explicit voice identity, cancellation during
synthesis, source revocation before and during playback, pause/resume timing,
ended-chunk reuse prevention, continuous reading limits, exact resume, corrupted
state preservation, output integrity and budgets, device loss and cleanup.
UI boundary tests cover native-style UTF-16 offsets, focus preservation, late
timer rejection, discovery during initial page acceptance and progress-cue
quiescence.

The actual Windows native probe is:

```console
python scripts/windows_live_reading_smoke.py --output ci-results/live-reading-native.json
```

It requires real wx controls and an installed SAPI5 voice. It renders actual
SAPI speech and verifies the passed library configuration root. Its controllable
PCM player is explicitly labelled in the report; it does not establish physical
audio playback. Until this command succeeds on the candidate Windows build,
native live-reader acceptance remains **NOT_RUN**.

Run the existing physical player gate separately on an audio-capable Windows
machine:

```console
python tests/speech/probe_windows_player.py --play
```

A missing backend or device is a failure of that gate. A hosted CI machine with
no audio device does not satisfy it. Audible listening, target Windows 11/NVDA
acceptance and voice-quality qualification require their own observed evidence.
These controls do not claim full Balabolka parity, word alignment, a new voice
engine, a bundled licensed voice catalogue or MusicGen support.

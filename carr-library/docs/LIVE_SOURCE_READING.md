# Live reading from an exact source or editable reading copy

Open **Read aloud from this source** in the Reading Desk (`Ctrl+Alt+R`), or
**Read aloud** in the editable reading-copy window. Refresh local voices, then
choose a voice explicitly. The current voice registry and its configured local
engines are reused; opening a reader never downloads a model or selects an
unavailable replacement voice.

## Reading and navigation

The range selector supports selected text, text from the cursor, the current
word, line, punctuation-delimited sentence, paragraph, current source location,
and the whole book from its first location. Whole-book reading loads one source
location at a time and skips empty locations. Sentence boundaries are a
punctuation navigation aid; they are not a claim about linguistic segmentation
for all languages.

**Prepare first excerpt** synthesizes a single bounded utterance without
starting an audio output device. **Read aloud** prepares and plays successive
utterances. The highlighted range identifies the exact source text for the
utterance. No word timing is inferred from elapsed time. Native control offsets
are verified against their actual text prefixes, including supplementary
Unicode characters and Windows line endings.

The existing shared player supplies pause/resume, seek and device selection.
Pause and precise seeking require its native Windows backend. **Where am I?**
shows the source location, utterance range, measured duration and the player's
reported position accuracy. Voice-native synthesis rate, pitch and volume are
enabled only when the chosen voice advertises those controls; they do not
silently substitute a different voice. Independent live playback speed/pitch
remain a separate player implementation task.

## Lifetime, cancellation and source integrity

One reader owns one background worker and at most one temporary PCM utterance.
Synthesis uses the existing resource broker, engine supervision and process
ownership. The source is checked before and after rendering. The selected
engine identity must remain unchanged for the session. The existing shared
media coordinator prevents conflicting narration/recording activity.

Before Play, a background worker revalidates the source and prepared PCM digest.
The existing immutable payload reader stays open across any media-coordinator
delay until the player releases its handle. Windows writers and renames are
excluded for this interval; changed POSIX payload metadata prevents both
handoff and a completed-cursor acknowledgement. A valid WAV header alone never
authorizes replacement audio to stand for the rendered source utterance.

The text cursor advances only after the player reports completion and releases
its audio handle. Stopping an unfinished utterance keeps its starting position
so resume can repeat it. Closing waits for pending synthesis and playback
owners before deleting temporary audio. A failed pause does not discard the
completion observer. A failed audio-handle release keeps the dialog and PCM
available for an explicit retry.

Original library text is read-only. Unsaved edits in a reading copy are spoken
as that independent copy and never borrow the original's verified source
location. Opening or speaking a source does not approve OCR or create quotation
evidence.

## Durable cursor and complete backup

`reading-speech.json` stores at most one resume row per library edition, with
the exact revision, source location, text digest, text length and completed
cursor. Independent reading copies are keyed by their text digest. The sidecar
contains no source text or PCM. Its bounded schema is validated before writes;
invalid or inaccessible existing state is not silently reset.

The existing reading-position store also remembers the current page. Restoring
a speech cursor requires that exact source text and location. A changed
revision never receives an old cursor automatically, and restoring a row does
not activate its historical voice or start playback.

Complete backup includes this typed sidecar, verifies each bound location
against the copied immutable database, and restores the bytes without retaining
links to the old library. Historical positions can be backed up without being
retargeted to an active revision. Temporary utterance PCM is not a backup input.

## Executed acceptance and remaining qualification

The final focused Linux/Python 3.12.14 run passed all 80 cases in
`tests/reader/test_live_speech.py`, `tests/ui/test_live_reading.py`, and
`tests/storage/test_live_reading_backup.py`, without skips. It exercised actual
resource reservations, files, workers, immutable source libraries, full
backup/restore and synthetic PCM, plus simulated character/UTF-16 native
boundaries. The prepared-audio handoff tests use actual changed PCM files and
verify that they cannot advance the completed cursor. An earlier nine-suite
regression run also passed all 269 cases across reading controls, keybindings,
draft saving and complete reader/Studio backup. These counts overlap. Those
tests do not qualify real local voices or an audio device.

`scripts/windows_live_reading_smoke.py` is a required installed/native probe in
the main, audio and full-bundle lanes. It uses actual wx controls and an
explicitly selected installed SAPI voice to prepare measured PCM, verifies
Unicode source highlights, focus, stop, durable resume and modal cleanup. A
probe is accepted only when the exact composed source executes successfully;
adding it to a workflow is not a passing result. Physical listening, manual
NVDA and language-specific voice quality remain separately reported evidence.
